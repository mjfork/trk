"""Leased claims: take, renew, release, the sweep of lapsed leases, and the live-claim reads.

`items` and `ready` are imported inside the functions that need them: `store` imports this module
while it is itself being imported, and `items` imports `store` back by name.
"""

import re
from datetime import datetime, timedelta

from . import clock
from .errors import conflict, refused, usage

_LEASE_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m?)?$")

_LIVE = "SELECT * FROM claims WHERE ended_at IS NULL"


def parse_lease(text):
    """Minutes from a duration: `45m`, `2h`, `1h30m`, or bare minutes `90`. At least one minute."""
    match = _LEASE_RE.match(text or "")
    if not match or not any(match.groups()):
        raise usage(f"a lease is a duration like 45m, 2h or 1h30m, got {text!r}")
    hours, minutes = (int(part or 0) for part in match.groups())
    total = hours * 60 + minutes
    if total < 1:
        raise usage(f"a lease is at least one minute, got {text!r}")
    return total


def _until(moment, minutes):
    return clock.stamp(moment + timedelta(minutes=minutes))


def _moment(stamp):
    return datetime.fromisoformat(stamp)


def live(conn, item_id):
    """The item's live claim row, or None."""
    return conn.execute(_LIVE + " AND item_id = ?", (item_id,)).fetchone()


def latest(conn, item_id):
    """The item's most recent claim row, live or ended, or None if it was never claimed."""
    return conn.execute("SELECT * FROM claims WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()


def is_live(claim, now):
    """Whether a claim row is live at `now`: not ended, and its lease not lapsed."""
    return claim["ended_at"] is None and claim["lease_until"] > clock.stamp(now)


def to_json(row):
    """The claim object on an item: who holds it and until when."""
    return {"session": row["session"], "actor": row["actor"], "lease_until": row["lease_until"],
            "taken_at": row["taken_at"], "scope": row["scope"]}


def listing(conn, session=None, item_id=None):
    """Live claims, oldest first, as `trk claims` prints them."""
    rows = conn.execute(
        _LIVE + " AND (:session IS NULL OR session = :session) AND (:item IS NULL OR item_id = :item) ORDER BY id",
        {"session": session, "item": item_id},
    )
    return [{"item": r["item_id"], **to_json(r)} for r in rows]


def _by_id(conn, claim_id):
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def _journal(tx, change, claim, *, data=None, at=None, actor=None):
    """One claim.* row: session is the claim's, data.claim_actor the claimant, actor whoever ran it."""
    from . import items

    tx.journal(change, items.feed_fields(tx.conn, claim["item_id"]), session=claim["session"], at=at, actor=actor,
               data={"claim_actor": claim["actor"], "lease_until": claim["lease_until"], "scope": claim["scope"],
                     **(data or {})})


def _lapsed(conn, now):
    return conn.execute(_LIVE + " AND lease_until <= ? ORDER BY lease_until, id", (clock.stamp(now),)).fetchall()


def expire(tx, actor=None):
    """End every lease with lease_until <= now inside `tx`, journaling `claim.expired` at the lease end.

    The row's actor is the claimant (no one acted), unless an explicit `actor` ran the sweep.
    Returns the items whose claims expired.
    """
    lapsed = _lapsed(tx.conn, tx.now)
    for claim in lapsed:
        tx.conn.execute("UPDATE claims SET ended_at = lease_until, \"end\" = 'expired' WHERE id = ?", (claim["id"],))
        _journal(tx, "claim.expired", claim, at=claim["lease_until"], actor=actor or claim["actor"])
    return [claim["item_id"] for claim in lapsed]


def sweep(store, actor=None):
    """End lapsed leases. A sweep that finds nothing takes no write lock. Returns the expired items."""
    if not _lapsed(store.conn, clock.now()):
        return []
    with store.transaction(actor) as tx:
        return expire(tx, actor)


SCOPES = ("spec", "build")

# What a spec-scope claim may claim past (UP-0011): a blocked ticket's spec can be written while its build waits.
SPEC_PAST = ("blocked-by:", "open-ask:")


def take(tx, raw_id, session, lease=None, force=False, scope="build"):
    """Claim an item for `session` on behalf of tx.actor; the same session claiming again renews.

    Refused, in this order: closed items and asks (rule `unclaimable`), another session's live claim
    (rule `claimed`, exit 4), programs unless `force` (`unclaimable`), and any other not-ready item (rule `not-ready`, naming its
    why_not rules), `force` or not. `force` exists only for a root's orchestrator holding its program, which `claim.taken`
    records as `data.forced`. Never touches the owner.

    `scope` (UP-0011): a `spec` claim is for writing the item's spec. It is granted past blocks edges and open asks,
    never counts its session as a builder, and refuses builder writes (rule `spec-scope`). The same session claiming
    again with scope `build` upgrades it, once the item is ready; a `spec` claim never downgrades a `build` one.
    """
    from . import items

    if scope not in SCOPES:
        raise usage(f"--scope is spec or build, got {scope!r}")
    expire(tx)
    row = items.get(tx.conn, raw_id)
    return _take(tx, row, session, lease, force, scope)


def take_next(tx, session, root=None, owner=None, lease=None):
    """Claim the first item `ready` returns; None when nothing is ready."""
    from . import ready

    expire(tx)
    rows = ready.frontier(tx.conn, root, owner, limit=1)
    return _take(tx, rows[0], session, lease, force=False, scope="build") if rows else None


def _refusal(tx, row, held, session, force, scope="build"):
    """Raise the refusal a claim by `session` would meet (`take`'s checks, in its order); return whether the
    item is a program claimed by force. Returns when the claim would be granted."""
    from . import ready

    item_id = row["id"]
    if row["status"] == "closed":
        raise refused("unclaimable", f"{item_id} is closed", item=item_id)
    if row["form"] == "ask":
        raise refused("unclaimable", f"{item_id} is an ask", item=item_id)
    if held is not None and held["session"] != session:
        raise conflict("claimed", f"{item_id} is claimed by session {held['session']} ({held['actor']})"
                       f" until {held['lease_until']}", item=item_id)
    if held is not None and (scope == "spec" or held["scope"] == "build"):
        return False
    forced = row["kind"] == "program"
    if forced and not force:
        raise refused("unclaimable", f"{item_id} is a program; only --force claims one", item=item_id)
    # An ask on the ticket keeps it off the frontier, not out of its builder's hands: the RED-read ask is open
    # while the builder's lease lapses, and a fresh session must still claim and write (tr-zn1dk).
    past = SPEC_PAST if scope == "spec" else ("open-ask:",)
    why_not = [rule for rule in ready.status(tx.conn, row)["why_not"]
               if not rule.startswith(past) and not (rule == "claimed" and held is not None)]
    if why_not and not forced:
        raise refused("not-ready", f"{item_id} is not ready: {', '.join(why_not)}", item=item_id)
    return forced


def require_held(tx, row, doing):
    """A builder's write needs the writer's claim on the ticket (tr-zn1dk). Passes for a writer with no session
    (a person's terminal) and for the session holding the live claim. Otherwise it raises what `claim` would
    raise for this writer (`unclaimable`, `claimed`, `not-ready`), and rule `not-claimed` when the claim would
    be granted: take it first. `doing` names the write in that message."""
    if not tx.session:
        return
    expire(tx)
    held = live(tx.conn, row["id"])
    if held is not None and held["session"] == tx.session:
        if held["scope"] == "spec":
            raise refused("spec-scope", f"{doing} is a builder's write; session {tx.session} holds a spec-scope claim"
                          f" on {row['id']}: `trk claim {row['id']} --scope build` once it is ready", item=row["id"])
        return
    _refusal(tx, row, held, tx.session, force=False)
    raise refused("not-claimed", f"{doing} needs your claim on {row['id']}, and session {tx.session} holds none: "
                  f"`trk claim {row['id']}` first", item=row["id"])


def _take(tx, row, session, lease, force, scope="build"):
    from . import items, ledger

    item_id = row["id"]
    held = live(tx.conn, item_id)
    forced = _refusal(tx, row, held, session, force, scope)
    if held is not None:
        upgrade = held["scope"] == "spec" and scope == "build"
        if upgrade:
            tx.conn.execute("UPDATE claims SET scope = 'build' WHERE id = ?", (held["id"],))
        _renew(tx, _by_id(tx.conn, held["id"]), lease)
        if upgrade and row["stage"] in ledger.PRE_BUILT:
            items.refresh_builder_asks(tx, item_id)
        return item_id
    minutes = lease or int(tx.store.meta("lease_minutes"))
    now = clock.stamp(tx.now)
    cursor = tx.conn.execute(
        "INSERT INTO claims (item_id, session, actor, lease_minutes, taken_at, lease_until, scope)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, session, tx.actor, minutes, now, _until(tx.now, minutes), scope),
    )
    _journal(tx, "claim.taken", _by_id(tx.conn, cursor.lastrowid), data={"forced": forced})
    if row["stage"] in ledger.PRE_BUILT and scope == "build":  # a claim before Built makes the claimant a builder
        items.refresh_builder_asks(tx, item_id)
    return item_id


def _renew(tx, claim, lease=None, actor=None):
    """Extend a live claim to now + its length (`lease`, if given, becomes the stored length)."""
    minutes = lease or claim["lease_minutes"]
    tx.conn.execute("UPDATE claims SET lease_minutes = ?, lease_until = ? WHERE id = ?",
                    (minutes, _until(tx.now, minutes), claim["id"]))
    _journal(tx, "claim.renewed", _by_id(tx.conn, claim["id"]), actor=actor)


def _due(conn, session, now):
    """The session's live claims with under half their stored lease left."""
    return [claim for claim in conn.execute(_LIVE + " AND session = ? ORDER BY id", (session,))
            if _moment(claim["lease_until"]) - now < timedelta(minutes=claim["lease_minutes"] / 2)]


def renew(store, actor, session, lease=None):
    """Renew `session`'s live claims and say what happened: `(renewed, not_due)`, each a list of
    `{"item", "lease_until", "lease_minutes"}`. Without `lease` only the claims past half their lease are
    renewed (and with none due no write lock is taken, which keeps the per-turn hook cheap); the others are
    `not_due`. With `lease` every live claim is renewed and stores that length. With no `actor` (no `--as`, no
    $TRK_ACTOR) each `claim.renewed` row is written by the claim's own actor, never the store's default (tr-82j8x)."""
    now = clock.now()
    if not (_due(store.conn, session, now) if lease is None else _mine(store.conn, session)):
        return [], _shown(_mine(store.conn, session))
    with store.transaction(actor or store.meta("default_actor"), session) as tx:
        expire(tx)
        renewing = _mine(tx.conn, session) if lease else _due(tx.conn, session, tx.now)
        for claim in renewing:
            _renew(tx, claim, lease, actor or claim["actor"])
        ids = {claim["id"] for claim in renewing}
        held = _mine(tx.conn, session)
    return (_shown([c for c in held if c["id"] in ids]), _shown([c for c in held if c["id"] not in ids]))


def _mine(conn, session):
    return conn.execute(_LIVE + " AND session = ? ORDER BY id", (session,)).fetchall()


def _shown(claims):
    return [{"item": c["item_id"], "lease_until": c["lease_until"], "lease_minutes": c["lease_minutes"]}
            for c in claims]


def release(tx, session, raw_id=None):
    """Release the session's live claims (or its claim on one item); any actor may. Returns the items."""
    from . import items

    query, params = _LIVE + " AND session = ?", [session]
    if raw_id is not None:
        query += " AND item_id = ?"
        params.append(items.get(tx.conn, raw_id)["id"])
    held = tx.conn.execute(query + " ORDER BY id", params).fetchall()
    for claim in held:
        _end(tx, claim)
    return [claim["item_id"] for claim in held]


def end_on_close(tx, item_id):
    """Closing an item ends its live claim: `claim.released` with `data.reason: closed`."""
    claim = live(tx.conn, item_id)
    if claim is not None:
        _end(tx, claim, reason="closed")


def _end(tx, claim, reason=None):
    tx.conn.execute("UPDATE claims SET ended_at = ?, \"end\" = 'released' WHERE id = ?",
                    (clock.stamp(tx.now), claim["id"]))
    _journal(tx, "claim.released", claim, data={"reason": reason} if reason else None)
