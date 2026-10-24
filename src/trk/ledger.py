"""The decision ledger: a ticket's append-only fork, contradiction and reward-hacking rows, the verdicts,
settlements and promotions they gather, asks tied to a row, and who counts as independent of the build.

Rows are never edited; a correction is a new row that refers to the old one. Independence is by session:
a session that built the ticket (a `builder` row, `pr` or `test-run` evidence, a claim held while the
ticket was before Built) never judges it, nor does a named agent that built it. `items` is imported
inside the functions that need it.
"""

import json
import re

from . import actors, claims, clock, config, criteria, evidence, stages
from .errors import gate_unmet, not_found, refused, usage

KINDS = ("fork", "contradiction", "reward-hack")
ORIGINS = ("builder", "auditor")
SETTLEMENTS = ("fixed", "deferred", "accepted", "overruled")

# The verdicts each row kind takes.
VERDICTS = {"fork": ("sound", "risky", "wrong"), "contradiction": ("sound", "risky", "wrong"),
            "reward-hack": ("confirmed", "refuted")}

TEXT_MAX = 500

# The title a repeated guess files its template ticket under.
TEMPLATE_TITLE = "Ticket template: add a field for {topic}"

# Stages a ticket may still take ledger rows at, and the stages before Built (a claim held then builds).
OPEN_STAGES = ("framed", "specd", "built")
PRE_BUILT = (None, "framed", "specd")

_HANDLE = re.compile(r"(?:ledger-)?([1-9][0-9]*)")


def handle(n):
    return f"ledger-{n}"


def number(raw, flag="the row"):
    """A row number from `ledger-<n>` or `<n>` (usage error otherwise)."""
    match = _HANDLE.fullmatch((raw or "").strip())
    if not match:
        raise usage(f"{flag} is ledger-<n> or <n>, got {raw!r}")
    return int(match.group(1))


def _line(value, flag, required=True):
    """One line of at most TEXT_MAX chars, stripped; None when absent and not required."""
    if value is None and not required:
        return None
    value = (value or "").strip()
    if not value:
        raise usage(f"{flag} needs \"<one line>\"")
    if "\n" in value or "\r" in value:
        raise usage(f"{flag} is one line, no newline")
    if len(value) > TEXT_MAX:
        raise usage(f"{flag} is at most {TEXT_MAX} chars (got {len(value)})")
    return value


def shared_agents(conn):
    """The actors many sessions write as, for whom only the session check applies (setting `shared-agent`)."""
    found = conn.execute("SELECT value FROM meta WHERE key = 'shared_agent'").fetchone()
    return set((found[0] if found else config.DEFAULTS["shared_agent"]).split(","))


# --- the ticket and its rows ---

def ticket(conn, raw_id):
    """The item row for a ledger verb: tickets only (rule `ledger-tickets-only`)."""
    from . import items

    row = items.get(conn, raw_id)
    if not stages.is_ticket(row):
        what = items.FORM_OUT.get(row["form"]) or items.KIND_OUT[row["kind"]]
        raise refused("ledger-tickets-only", f"{row['id']} is a {what}; ledger rows belong to tickets",
                      item=row["id"])
    return row


def _open_ticket(conn, raw_id):
    row = ticket(conn, raw_id)
    if row["status"] == "closed":
        raise refused("closed", f"{row['id']} is closed", item=row["id"])
    return row


def get(conn, item_id, raw_n, live=False):
    """The ledger row `raw_n` names on a ticket; exit 3 when it has none. `live` also refuses a withdrawn
    row (rule `ledger-withdrawn`): nothing is judged, settled or promoted once it is withdrawn."""
    n = number(raw_n)
    found = conn.execute("SELECT * FROM ledger WHERE item_id = ? AND n = ?", (item_id, n)).fetchone()
    if found is None:
        raise not_found(f"{item_id} has no {handle(n)}", item=item_id)
    if live and withdrawal(conn, item_id, n) is not None:
        raise refused("ledger-withdrawn", f"{handle(n)} of {item_id} is withdrawn", item=item_id)
    return found


def withdrawal(conn, item_id, n):
    """The row that withdrew ledger row `n`, or None."""
    return conn.execute("SELECT * FROM ledger_withdrawals WHERE item_id = ? AND n = ?", (item_id, n)).fetchone()


def _audit_begun(conn, item_id):
    """Whether any audit has begun on a ticket: an `audit` evidence row, an auditor's ledger row or a verdict."""
    return any(conn.execute(query, (item_id,)).fetchone() for query in (
        "SELECT 1 FROM evidence WHERE item_id = ? AND kind = 'audit'",
        "SELECT 1 FROM ledger WHERE item_id = ? AND origin = 'auditor'",
        "SELECT 1 FROM ledger_verdicts WHERE item_id = ?"))


def _audited_by(conn, item_id, session):
    """Whether `session` audited the ticket: wrote an auditor row, a verdict or an `audit` row on it."""
    return session is not None and any(conn.execute(query, (item_id, session)).fetchone() for query in (
        "SELECT 1 FROM evidence WHERE item_id = ? AND kind = 'audit' AND session = ?",
        "SELECT 1 FROM ledger WHERE item_id = ? AND origin = 'auditor' AND session = ?",
        "SELECT 1 FROM ledger_verdicts WHERE item_id = ? AND session = ?"))


def _claimed_by(conn, item_id, session):
    """Whether `session` ever claimed the ticket, at any stage, live or ended."""
    return session is not None and conn.execute("SELECT 1 FROM claims WHERE item_id = ? AND session = ?",
                                                (item_id, session)).fetchone() is not None


def _cited(conn, item_id, n):
    """Whether another row of the ledger corrects ledger row `n` (`refers_to`) or repeats it (`repeat_of`)."""
    return conn.execute("SELECT 1 FROM ledger WHERE (item_id = ? AND refers_to = ?) OR repeat_of = ?",
                        (item_id, n, f"{item_id}:{n}")).fetchone() is not None


def withdraw(tx, raw_id, raw_n, reason):
    """`ledger withdraw`: take a row out of the ledger with a one-line reason, as `evidence retract` does for
    evidence; the row stays listed, marked withdrawn, and no gate counts it. Ledger rows are added before Merged,
    so they are withdrawn before it (`ledger-closed`). Refused: a row that has a verdict, settlement, promotion
    or tied ask (`ledger-judged`: it has been looked at, and withdrawing it would dodge that), a row another
    row corrects or repeats (`ledger-cited`) and a row already withdrawn (`ledger-withdrawn`). By writer:
    the builder withdraws only rows its own session wrote (`not-own-row`) and only before any audit has begun
    (`ledger-audited`; an auditor's row is `not-independent`); a session that holds or ever held a claim on
    the ticket counts as the builder, at any stage; an auditor's row (origin `auditor`, or a reward-hacking flag) is withdrawn only by the
    session that wrote it, or by a person (`not-own-row`); an auditor withdraws only its own rows
    (`not-own-row`); any other session that never built the ticket (the driver) withdraws a builder's row, and
    a person withdraws any row. The builder's session still counts as the builder after its row is withdrawn.
    Returns the row's handle."""
    reason = _line(reason, "--reason")
    conn = tx.conn
    row = _open_ticket(conn, raw_id)
    if row["stage"] not in OPEN_STAGES:
        raise refused("ledger-closed", f"{row['id']} is at {row['stage']}; ledger rows are withdrawn before Merged",
                      item=row["id"])
    entry = get(conn, row["id"], raw_n, live=True)
    n = entry["n"]
    if any(conn.execute(f"SELECT 1 FROM {table} WHERE item_id = ? AND n = ?", (row["id"], n)).fetchone()
           for table in ("ledger_verdicts", "ledger_settlements", "ledger_promotions", "ledger_asks")):
        raise refused("ledger-judged", f"{handle(n)} of {row['id']} has a verdict, settlement, promotion or tied "
                      "ask; it cannot be withdrawn", item=row["id"])
    if _cited(conn, row["id"], n):
        raise refused("ledger-cited", f"{handle(n)} of {row['id']} is corrected or repeated by another row; it "
                      "cannot be withdrawn", item=row["id"])
    if is_builder(conn, row["id"], tx.session, tx.actor) or _claimed_by(conn, row["id"], tx.session):
        if entry["origin"] == "auditor":
            raise refused("not-independent", f"{tx.actor} built {row['id']}; withdrawing an auditor row needs an "
                          "independent session", item=row["id"])
        if _audit_begun(conn, row["id"]):
            raise refused("ledger-audited", f"{row['id']} has been audited; the builder can no longer withdraw "
                          f"{handle(n)}: ask the driver", item=row["id"])
        if entry["session"] != tx.session:
            raise refused("not-own-row", f"session {tx.session} built {row['id']}; the builder withdraws only rows "
                          f"its own session wrote, and {handle(n)} is not its own", item=row["id"])
    elif tx.session is None:
        if actors.kind(conn, tx.actor) != "human":
            raise refused("session-required", "withdrawing a ledger row needs a session (--session or "
                          "$TRK_SESSION) unless a person writes", item=row["id"])
    elif entry["session"] != tx.session and (entry["origin"] == "auditor" or entry["kind"] == "reward-hack"):
        raise refused("not-own-row", f"{handle(n)} of {row['id']} is an auditor's finding; only the session that "
                      "wrote it, or a person, withdraws it", item=row["id"])
    elif _audited_by(conn, row["id"], tx.session) and entry["session"] != tx.session:
        raise refused("not-own-row", f"session {tx.session} audited {row['id']}; an auditor withdraws only its "
                      f"own rows, and {handle(n)} is not its own", item=row["id"])
    seq = _journal(tx, "ledger.withdrawn", row, {"ledger": handle(n), "n": n, "kind": entry["kind"],
                                                "reason": _excerpt(reason)})
    conn.execute("INSERT INTO ledger_withdrawals (item_id, n, reason, actor, session, seq, created_at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?)", (row["id"], n, reason, tx.actor, tx.session, seq,
                                                  clock.stamp(tx.now)))
    return handle(n)


def _exists(conn, item_id, n):
    return conn.execute("SELECT 1 FROM ledger WHERE item_id = ? AND n = ?", (item_id, n)).fetchone() is not None


def tie_target(conn, parent, raw_n):
    """`ask --ledger <n>`: the row number when the ask's parent is a ticket with that row; usage error otherwise."""
    n = number(raw_n, "--ledger")
    if not stages.is_ticket(parent):
        raise usage(f"--ledger ties an ask to a ledger row of a ticket; {parent['id']} is not a ticket")
    if not _exists(conn, parent["id"], n):
        raise usage(f"--ledger: {parent['id']} has no {handle(n)}")
    return n


def tie(conn, parent_id, n, ask_id):
    conn.execute("INSERT INTO ledger_asks (item_id, n, ask_id) VALUES (?, ?, ?)", (parent_id, n, ask_id))


def tied_to(conn, ask_id):
    """The `ledger-<n>` an ask is tied to, or None."""
    found = conn.execute("SELECT n FROM ledger_asks WHERE ask_id = ?", (ask_id,)).fetchone()
    return handle(found["n"]) if found else None


# --- independence ---

def builders(conn, item_id):
    """(sessions, actors) that built a ticket: wrote a `builder` row, recorded `pr` or `test-run` evidence,
    or held a claim while it was before Built. Actors are every one of them; `builder_actors` filters."""
    acts = set(conn.execute("SELECT session, actor FROM ledger WHERE item_id = ? AND origin = 'builder'", (item_id,)))
    acts |= set(conn.execute("SELECT session, actor FROM evidence WHERE item_id = ? AND kind IN ('pr', 'test-run')",
                             (item_id,)))
    acts |= _pre_built_claims(conn, item_id)
    return {session for session, _ in acts if session}, {actor for _, actor in acts}


def _pre_built_claims(conn, item_id):
    """(session, claimant) of every claim held during a period when the ticket's stage was before Built,
    replaying its stage moves and claim rows in seq order. A claim is held until its row ending it, or
    its lease end when that comes first (a lapse is journaled only when a later write sweeps it)."""
    stage, held, found = None, {}, set()
    for change, at, session, data in conn.execute(
            "SELECT change, at, session, data FROM journal WHERE item = ? AND change IN ('stage.changed',"
            " 'claim.taken', 'claim.renewed', 'claim.released', 'claim.expired') ORDER BY seq", (item_id,)):
        data = json.loads(data)
        if change == "stage.changed":
            stage = data["new"]
            held = {s: claim for s, claim in held.items() if claim[1] > at}
            if stage in PRE_BUILT:
                found |= {(s, claimant) for s, (claimant, _) in held.items()}
        elif change in ("claim.taken", "claim.renewed"):
            held[session] = (data["claim_actor"], data["lease_until"])
            if stage in PRE_BUILT:
                found.add((session, data["claim_actor"]))
        else:
            held.pop(session, None)
    return found


def builder_actors(conn, acting):
    """The builder actors among `acting`: agents other than the shared agents."""
    shared = shared_agents(conn)
    return {actor for actor in acting if actor not in shared and actors.kind(conn, actor) == "agent"}


def is_builder(conn, item_id, session, actor):
    return built_by(conn, item_id)(session, actor)


def built_by(conn, item_id):
    """A predicate (session, actor) -> whether they built the ticket: a builder session or builder actor."""
    sessions, acting = builders(conn, item_id)
    barred = builder_actors(conn, acting)
    return lambda session, actor: (session is not None and session in sessions) or actor in barred


def check_independent(tx, row, doing):
    """Refuse a verdict, an auditor row or an audit without a session (rule `session-required`) or from
    a builder session or builder actor (rule `not-independent`)."""
    if not tx.session:
        raise refused("session-required", f"{doing} needs a session (--session or $TRK_SESSION): "
                      "independence is checked by session", item=row["id"])
    sessions, acting = builders(tx.conn, row["id"])
    if tx.session in sessions:
        raise refused("not-independent", f"session {tx.session} built {row['id']}; {doing} needs an independent "
                      "session", item=row["id"])
    if tx.actor in builder_actors(tx.conn, acting):
        raise refused("not-independent", f"{tx.actor} built {row['id']}; {doing} needs an independent actor",
                      item=row["id"])


# --- writes ---

def _journal(tx, change, row, data):
    from . import items

    seq = tx.journal(change, items.feed_fields(tx.conn, row["id"]), data=data)
    tx.conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (clock.stamp(tx.now), row["id"]))
    return seq


def _excerpt(text):
    return criteria.excerpt(text) if text is not None else None


def add(tx, raw_id, kind, *, decided, alternatives=(), why, would_have_asked=None, conflicts_with=None, files=(),
        sha=None, origin="builder", noise=False, repeat_of=None, topic=None, refers_to=None):
    """`ledger add`: one row, checked; returns its handle. Usage errors name the flag; then the ticket
    must be a ticket (`ledger-tickets-only`), open (`closed`) and before Merged (`ledger-closed`), and an
    auditor row needs an independent session."""
    if kind not in KINDS:
        raise usage(f"--kind is one of {', '.join(KINDS)}, got {kind!r}")
    if origin not in ORIGINS:
        raise usage(f"--origin is one of {', '.join(ORIGINS)}, got {origin!r}")
    decided = _line(decided, "--decided")
    why = _line(why, "--why")
    alternatives = [_line(alt, "--alternative") for alt in alternatives]
    if kind == "fork" and not alternatives:
        raise usage("a fork needs at least one --alternative \"<what else was weighed>\"")
    would_have_asked = _line(would_have_asked, "--would-have-asked", required=False)
    conflicts_with = _line(conflicts_with, "--conflicts-with", required=False)
    if kind == "contradiction" and conflicts_with is None:
        raise usage("a contradiction needs --conflicts-with <ADR path or spec:<section>>")
    files = [_line(path, "--file") for path in files]
    sha = evidence.check_sha(sha) if sha is not None else None
    if origin != "auditor" and (noise or repeat_of is not None):
        raise usage("--noise and --repeat-of are the auditor's: they need --origin auditor")
    if (repeat_of is None) != (topic is None):
        raise usage("--repeat-of <ticket>:<n> and --topic \"<topic>\" go together")
    topic = _line(topic, "--topic", required=False)
    refers = number(refers_to, "--refers-to") if refers_to is not None else None
    conn = tx.conn
    row = _open_ticket(conn, raw_id)
    if row["stage"] not in OPEN_STAGES:
        raise refused("ledger-closed", f"{row['id']} is at {row['stage']}; ledger rows are added before Merged",
                      item=row["id"])
    if refers is not None and not _exists(conn, row["id"], refers):
        raise usage(f"--refers-to: {row['id']} has no {handle(refers)}")
    repeated = None
    if repeat_of is not None:
        repeated = _repeated(conn, repeat_of)
        repeat_of = f"{repeated['item_id']}:{repeated['n']}"
    if origin == "auditor":
        check_independent(tx, row, "an auditor row")
    else:
        claims.require_held(tx, row, "a builder ledger row")
    n = conn.execute("SELECT coalesce(max(n), 0) + 1 FROM ledger WHERE item_id = ?", (row["id"],)).fetchone()[0]
    conn.execute(
        "INSERT INTO ledger (item_id, n, kind, decided, alternatives, why, would_have_asked, conflicts_with, files,"
        " origin, noise, repeat_of, topic, refers_to, actor, session, sha, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], n, kind, decided, json.dumps(alternatives), why, would_have_asked, conflicts_with,
         json.dumps(files), origin, int(noise), repeat_of, topic, refers, tx.actor, tx.session, sha,
         clock.stamp(tx.now)),
    )
    _journal(tx, "ledger.added", row, {
        "ledger": handle(n), "n": n, "kind": kind, "decided": _excerpt(decided),
        "alternatives": [_excerpt(alt) for alt in alternatives], "why": _excerpt(why),
        "would_have_asked": _excerpt(would_have_asked), "conflicts_with": conflicts_with, "files": files,
        "origin": origin, "noise": bool(noise), "repeat_of": repeat_of, "topic": topic,
        "refers_to": handle(refers) if refers else None, "sha": sha})
    if repeat_of is not None:
        _file_template(tx, row, n, decided, repeated, topic)
    if origin == "builder":  # a new builder session: an open RED-read ask's byline may change
        from . import items

        items.refresh_builder_asks(tx, row["id"])
    return handle(n)



def _template_parent(conn, row):
    """Where template tickets are filed: setting `template-parent`, or else the reviewed ticket's root."""
    from . import items

    found = conn.execute("SELECT value FROM meta WHERE key = 'template_parent'").fetchone()
    return found[0] if found else items.hierarchy(conn, row["id"])["root"]


def _file_template(tx, row, n, decided, repeated, topic):
    """A repeated guess is a template fix: relate both tickets to the open self-contained ticket titled for
    the topic under the template parent, filing it (born Framed) when there is none."""
    from . import edges, items

    conn = tx.conn
    target = repeated["item_id"]
    parent = _template_parent(conn, row)
    title = TEMPLATE_TITLE.format(topic=topic)
    found = conn.execute("SELECT id FROM items WHERE parent_id = ? AND title = ? AND form = 'self_contained'"
                         " AND status = 'open' ORDER BY rank LIMIT 1", (parent, title)).fetchone()
    if found is not None:
        template = found["id"]
    else:
        current = (f"The ticket template has no field for {topic}, so builders guess it, and an auditor found the "
                   f"same guess in two tickets:\n\n- {target} {handle(repeated['n'])}: {repeated['decided']}\n"
                   f"- {row['id']} {handle(n)}: {decided}\n")
        desired = (f"The ticket template has a field for {topic}, so a ticket states it and its builder no longer "
                   "guesses.\n")
        template = items.create(tx, "work-item", title, form="self-contained", parent=parent,
                                section_bodies=[("current-behaviour", current), ("desired-behaviour", desired)],
                                criterion_texts=[f"The ticket template has a field for {topic}."])
    edges.add(tx, template, "relates", row["id"])
    edges.add(tx, template, "relates", target)


def _repeated(conn, raw):
    """The ledger row `--repeat-of <ticket>:<n>` names; usage error when it has none."""
    target, sep, raw_n = raw.rpartition(":")
    if not sep or not target:
        raise usage(f"--repeat-of is <ticket>:<n>, got {raw!r}")
    found = ticket(conn, target)
    n = number(raw_n, "--repeat-of")
    row = conn.execute("SELECT * FROM ledger WHERE item_id = ? AND n = ?", (found["id"], n)).fetchone()
    if row is None:
        raise usage(f"--repeat-of: {found['id']} has no {handle(n)}")
    return row


def verdict(tx, raw_id, raw_n, value, note, sha=None):
    """`ledger verdict`: sound|risky|wrong on a fork or contradiction, confirmed|refuted on a reward-hack
    flag, from an independent session, at --sha or the head sha. Returns the row's handle."""
    every = sorted({v for values in VERDICTS.values() for v in values})
    if value not in every:
        raise usage(f"the verdict is one of {', '.join(every)}, got {value!r}")
    note = _line(note, "--note")
    sha = evidence.check_sha(sha) if sha is not None else None
    conn = tx.conn
    row = _open_ticket(conn, raw_id)
    entry = get(conn, row["id"], raw_n, live=True)
    if value not in VERDICTS[entry["kind"]]:
        raise usage(f"a {entry['kind']} row takes {' or '.join(VERDICTS[entry['kind']])}, not {value}")
    check_independent(tx, row, "a verdict")
    if sha is None:
        sha = evidence.weigh(conn, row).head
    seq = _journal(tx, "ledger.verdict", row, {"ledger": handle(entry["n"]), "n": entry["n"], "kind": entry["kind"],
                                              "verdict": value, "note": _excerpt(note), "sha": sha})
    conn.execute("INSERT INTO ledger_verdicts (item_id, n, verdict, note, sha, actor, session, seq, created_at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (row["id"], entry["n"], value, note, sha, tx.actor, tx.session, seq, clock.stamp(tx.now)))
    return handle(entry["n"])


def audit(tx, raw_id, note):
    """`ledger audit`: the `audit` evidence row for gate `merged` at the head sha, from an independent
    session; refused `gate-unmet` (need `pr`) while there is no head sha, and `brief-required` while the ticket's
    latest counting rating is RED and its `review-brief` is missing or lacks a heading (`blast.check_brief`).
    Returns the evidence id."""
    from . import blast

    note = evidence.check_note(note)
    conn = tx.conn
    row = _open_ticket(conn, raw_id)
    check_independent(tx, row, "an audit")
    head = evidence.weigh(conn, row).head
    if head is None:
        raise gate_unmet(row["id"], "merged", [{"need": "pr"}], "an audit is recorded at the head sha: "
                         "record the ticket's pr evidence first")
    blast.check_brief(conn, row)
    return evidence.record(tx, row, "audit", "merged", sha=head, note=note)


def settle(tx, raw_id, raw_n, settlement, *, sha=None, ask=None, adr=None, note=None):
    """`ledger settle`: `fixed --sha S` records the fix sha (a contradiction's, and any naming one, needs an
    answered `engineering` ask tied to the row); `accepted --ask A [--adr PATH]` and `overruled --ask A` need
    that ask and a writer who did not build the ticket (rule `settle-needs-answer`), except that an auditor
    settles a contradiction it raised with no ask (`_auditor_signoff`); `deferred` files a
    self-contained ticket for the row in the same transaction, and the builder never writes it (rule
    `not-independent`). Each but `fixed` must fit the row's latest verdict (usage error otherwise). A
    reward-hacking flag whose latest verdict is CONFIRMED is only fixed (rule `reward-hack-final`).
    Returns the row's handle."""
    if settlement not in SETTLEMENTS:
        raise usage(f"the settlement is one of {', '.join(SETTLEMENTS)}, got {settlement!r}")
    if settlement == "fixed" and sha is None:
        raise usage("fixed needs --sha <the fix commit>")
    if settlement != "fixed" and sha is not None:
        raise usage(f"--sha goes with fixed, not {settlement}")
    if settlement == "deferred" and ask is not None:
        raise usage("deferred takes no --ask: it files a ticket for the row")
    if adr is not None and settlement != "accepted":
        raise usage(f"--adr goes with accepted, not {settlement}")
    sha = evidence.check_sha(sha) if sha is not None else None
    adr = _line(adr, "--adr", required=False)
    note = _line(note, "--note", required=False)
    conn = tx.conn
    row = _open_ticket(conn, raw_id)
    entry = get(conn, row["id"], raw_n, live=True)
    latest = _settling_verdict(conn, row, entry)
    if settlement != "fixed" and latest is not None and latest["verdict"] == "confirmed":
        raise refused("reward-hack-final", f"{handle(entry['n'])} of {row['id']} is a confirmed reward-hacking flag; "
                      "it is only fixed (settle … fixed --sha <the fix>, then a REFUTED re-check)", item=row["id"])
    if settlement != "fixed":
        _check_fits(row, entry, settlement, latest)
    if settlement == "deferred" and is_builder(conn, row["id"], tx.session, tx.actor):
        raise refused("not-independent", f"{tx.actor} built {row['id']}; the builder never defers its own row",
                      item=row["id"])
    signed = None
    if settlement in ("accepted", "overruled"):
        ask = _answered_ask(tx, row, entry, ask, builder_barred=True)
    elif settlement == "fixed" and entry["kind"] == "contradiction" and ask is None:
        signed = _auditor_signoff(tx, row, entry, sha)
    elif settlement == "fixed" and (entry["kind"] == "contradiction" or ask is not None):
        ask = _answered_ask(tx, row, entry, ask, builder_barred=entry["kind"] == "contradiction")
    filed = _file_deferred(tx, row, entry, latest["note"]) if settlement == "deferred" else None
    seq = _journal(tx, "ledger.settled", row, {
        "ledger": handle(entry["n"]), "n": entry["n"], "kind": entry["kind"], "settlement": settlement, "sha": sha,
        "ask": ask, "ticket": filed, "adr": adr, "note": _excerpt(note),
        "verdict_seq": signed["seq"] if signed else None, "verdict_sha": signed["sha"] if signed else None})
    conn.execute("INSERT INTO ledger_settlements (item_id, n, settlement, sha, ask, ticket, adr, note, actor, session,"
                 " seq, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (row["id"], entry["n"], settlement, sha, ask, filed, adr, note, tx.actor, tx.session, seq,
                  clock.stamp(tx.now)))
    return handle(entry["n"])


def _settling_verdict(conn, row, entry):
    """The verdict `settle` works from: the Merged gate's choice, the latest independent verdict at or below
    the ticket head (`verdicts_at_head`). With no head yet, the latest by time. When there is none at the
    head but another lies outside it, a usage error names that verdict's sha and the head."""
    from . import gitrepo

    head = evidence.weigh(conn, row).head
    if head is None:
        return _latest(conn, "ledger_verdicts", row["id"], entry["n"], "verdict, note")
    built = built_by(conn, row["id"])
    counting, _stuck, outside = verdicts_at_head(
        conn, row["id"], entry["n"], gitrepo.repos_of(conn, row["id"]), head,
        lambda record: not built(record["session"], record["actor"]))
    if counting:
        found = conn.execute("SELECT verdict, note FROM ledger_verdicts WHERE item_id = ? AND n = ? AND seq = ?",
                             (row["id"], entry["n"], counting[-1]["seq"])).fetchone()
        return dict(found)
    if outside:
        raise usage(f"{handle(entry['n'])} of {row['id']} has no verdict at or below the head {head}; its "
                    f"latest ({outside[-1]['verdict']}) is at {outside[-1]['sha']}, outside the head")
    return None


def _check_fits(row, entry, settlement, latest):
    """A usage error unless the row's latest verdict is one this settlement settles: `deferred` and
    `accepted` a RISKY row, `overruled` a WRONG one; a contradiction is also `accepted` when judged SOUND."""
    label = handle(entry["n"])
    if latest is None:
        raise usage(f"{label} of {row['id']} has no verdict yet; a row is {settlement} once judged")
    fits = [judged for judged in ("risky", "wrong") if settlement in settles(entry["kind"], judged)]
    if entry["kind"] == "contradiction" and settlement == "accepted":
        fits.append("sound")
    if latest["verdict"] not in fits:
        raise usage(f"{label} of {row['id']} is judged {latest['verdict']}; {settlement} settles a row judged "
                    f"{' or '.join(fits) or 'otherwise'}")


def _file_deferred(tx, row, entry, verdict_note):
    """A deferred row becomes a self-contained ticket, born Framed, under the reviewed ticket's nearest
    project (else its root), related to it: current behaviour is the row, desired behaviour the latest
    verdict's note, and one drafted criterion. Returns its id."""
    from . import edges, items

    conn = tx.conn
    label = handle(entry["n"])
    project = items.nearest_project(conn, row["id"])
    parent = project["id"] if project is not None else items.hierarchy(conn, row["id"])["root"]
    files = json.loads(entry["files"])
    current = (f"{label} of {row['id']} ({entry['kind']}): {entry['decided']}\n\nWhy: {entry['why']}\n"
               + (f"\nFiles: {', '.join(files)}\n" if files else ""))
    filed = items.create(tx, "work-item", f"Deferred: {_excerpt(entry['decided'])}", form="self-contained",
                         parent=parent, section_bodies=[("current-behaviour", current),
                                                        ("desired-behaviour", f"{verdict_note}\n")],
                         criterion_texts=[f"{label} of {row['id']} resolved as the reviewer describes"])
    edges.add(tx, filed, "relates", row["id"])
    return filed


def _auditor_signoff(tx, row, entry, fix):
    """The verdict an auditor's own settle of a contradiction relies on, when `fixed --sha fix` needs no ask:
    the row's origin is `auditor`, the writer is the actor `auditor` from a session that did not build the
    ticket, and the row's latest independent verdict is SOUND, by the actor `auditor`, at a sha that is `fix`
    or contains it. Rule `settle-needs-answer` names the first condition missing and the `--ask` route;
    `sha-unverifiable` when git cannot tell whether the verdict holds the fix."""
    from . import gitrepo

    label = handle(entry["n"])

    def needs(why):
        return refused("settle-needs-answer", f"settling {label} of {row['id']} without --ask needs an auditor's "
                       f"sign-off: {why}; or settle with --ask <an answered engineering ask tied to it> "
                       f"(trk ask {row['id']} --to engineering --ledger {entry['n']})", item=row["id"])

    if entry["origin"] != "auditor":
        raise needs(f"{label} was logged by a builder, not an auditor")
    if tx.actor != "auditor":
        raise needs(f"{tx.actor} is not the actor auditor")
    check_independent(tx, row, "settling a contradiction without an ask")
    conn = tx.conn
    built = built_by(conn, row["id"])
    verdicts = [v for v in conn.execute("SELECT verdict, sha, actor, session, seq FROM ledger_verdicts"
                                        " WHERE item_id = ? AND n = ? ORDER BY seq", (row["id"], entry["n"]))
                if not built(v["session"], v["actor"])]
    if not verdicts:
        raise needs(f"{label} has no verdict from an independent session")
    latest = verdicts[-1]
    if latest["verdict"] != "sound" or latest["actor"] != "auditor":
        raise needs(f"the latest independent verdict on {label} is {latest['verdict']} by {latest['actor']}; the "
                    f"actor auditor must record a sound one at the fix {fix}")
    repos = gitrepo.repos_of(conn, row["id"])
    held = gitrepo.contains(gitrepo.place(repos, fix, latest["sha"]), fix, latest["sha"])
    if held is None:
        raise refused("sha-unverifiable", f"git cannot tell whether the sound verdict at {latest['sha']} holds the "
                      f"fix {fix} ({gitrepo.tried_in(repos, fix).get('hint', 'checkout tried: none')})",
                      item=row["id"])
    if not held:
        raise needs(f"the sound verdict at {latest['sha']} does not contain the fix {fix}")
    return {"seq": latest["seq"], "sha": latest["sha"]}


def _answered_ask(tx, row, entry, raw_ask, builder_barred):
    """The ask id, when it is a child of the ticket tied to this row, addressed to `engineering`, answered
    by a human, and (when `builder_barred`) the writer did not build the ticket; rule
    `settle-needs-answer` otherwise."""
    from . import items

    label = handle(entry["n"])

    def needs(why):
        return refused("settle-needs-answer", f"settling {label} of {row['id']} needs an answered engineering ask "
                       f"tied to it (trk ask {row['id']} --to engineering --ledger {entry['n']}): {why}", item=row["id"])

    if raw_ask is None:
        raise needs("no --ask given")
    ask = items.get(tx.conn, raw_ask)
    if ask["form"] != "ask" or ask["parent_id"] != row["id"]:
        raise needs(f"{ask['id']} is not an ask under {row['id']}")
    if not tx.conn.execute("SELECT 1 FROM ledger_asks WHERE item_id = ? AND n = ? AND ask_id = ?",
                           (row["id"], entry["n"], ask["id"])).fetchone():
        raise needs(f"{ask['id']} is not tied to {label}")
    if ask["addressee_role"] != "engineering":
        raise needs(f"{ask['id']} is addressed to {ask['addressee_role']}")
    if ask["status"] != "closed" or ask["resolution"] != "done":
        raise needs(f"{ask['id']} is not answered")
    answered = tx.conn.execute("SELECT actor FROM journal WHERE item = ? AND change = 'ask.answered'"
                               " ORDER BY seq DESC LIMIT 1", (ask["id"],)).fetchone()
    if answered is None or actors.kind(tx.conn, answered["actor"]) != "human":
        raise needs(f"{ask['id']} was not answered by a person")
    if builder_barred and is_builder(tx.conn, row["id"], tx.session, tx.actor):
        raise needs(f"{tx.actor} built {row['id']}; the builder never settles through an answer")
    return ask["id"]


def promote(tx, raw_id, raw_n, adr):
    """`ledger promote`: record the ADR a row was promoted to. A person's call only (rule `human-only`)."""
    adr = _line(adr, "--adr")
    conn = tx.conn
    row = ticket(conn, raw_id)
    entry = get(conn, row["id"], raw_n, live=True)
    if actors.kind(conn, tx.actor) != "human":
        raise refused("human-only", f"{tx.actor} is an agent; only a person promotes a ledger row to an ADR",
                      item=row["id"])
    seq = _journal(tx, "ledger.promoted", row, {"ledger": handle(entry["n"]), "n": entry["n"], "kind": entry["kind"],
                                               "adr": adr})
    conn.execute("INSERT INTO ledger_promotions (item_id, n, adr, actor, session, seq, created_at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?)", (row["id"], entry["n"], adr, tx.actor, tx.session, seq,
                                                  clock.stamp(tx.now)))
    return handle(entry["n"])


# --- reads ---

def _latest(conn, table, item_id, n, columns):
    found = conn.execute(f"SELECT {columns} FROM {table} WHERE item_id = ? AND n = ? ORDER BY seq DESC LIMIT 1",
                         (item_id, n)).fetchone()
    return dict(found) if found else None


def rows_json(conn, item_id):
    """Every ledger row of an item in order, each with its latest verdict, settlement and promotion and
    the asks tied to it; empty for items that are not tickets."""
    from . import items

    result = []
    for entry in conn.execute("SELECT * FROM ledger WHERE item_id = ? ORDER BY n", (item_id,)):
        n = entry["n"]
        asks = conn.execute(
            "SELECT i.id, i.title, i.status, i.resolution FROM ledger_asks a JOIN items i ON i.id = a.ask_id"
            " WHERE a.item_id = ? AND a.n = ? ORDER BY i.created_at, i.id", (item_id, n)).fetchall()
        result.append({
            "handle": handle(n), "n": n, "kind": entry["kind"], "decided": entry["decided"],
            "alternatives": json.loads(entry["alternatives"]), "why": entry["why"],
            "would_have_asked": entry["would_have_asked"], "conflicts_with": entry["conflicts_with"],
            "files": json.loads(entry["files"]), "origin": entry["origin"], "noise": bool(entry["noise"]),
            "repeat_of": entry["repeat_of"], "topic": entry["topic"], "refers_to": entry["refers_to"],
            "actor": entry["actor"], "session": entry["session"], "sha": entry["sha"],
            "created_at": entry["created_at"],
            "verdict": _latest(conn, "ledger_verdicts", item_id, n,
                               "verdict, note, sha, actor, session, seq, created_at"),
            "settlement": _latest(conn, "ledger_settlements", item_id, n,
                                  "settlement, sha, ask, ticket, adr, note, actor, session, seq, created_at"),
            "promotion": _latest(conn, "ledger_promotions", item_id, n, "adr, actor, session, seq, created_at"),
            "asks": [{"id": a["id"], "title": a["title"], "status": a["status"],
                      "resolution": items.RESOLUTION_OUT.get(a["resolution"])} for a in asks],
            "withdrawn": _latest(conn, "ledger_withdrawals", item_id, n, "reason, actor, session, seq, created_at"),
        })
    return result


def listing(conn, raw_id):
    """`trk ledger list`: (ticket id, its rows)."""
    row = ticket(conn, raw_id)
    return row["id"], rows_json(conn, row["id"])


def line(entry):
    """One row as text: handle, kind, origin, what was decided, then its latest verdict, settlement,
    promotion and tied asks."""
    marks = [entry["handle"], entry["kind"], entry["origin"]]
    if entry["noise"]:
        marks.append("noise")
    text = "  ".join(marks) + f"  {entry['decided']}"
    after = []
    if entry["withdrawn"]:
        after.append(f"withdrawn: {entry['withdrawn']['reason']}")
    if entry["verdict"]:
        after.append(f"verdict {entry['verdict']['verdict']}")
    if entry["settlement"]:
        after.append(f"settled {entry['settlement']['settlement']}")
    if entry["promotion"]:
        after.append(f"adr {entry['promotion']['adr']}")
    if entry["asks"]:
        after.append("asks " + " ".join(a["id"] for a in entry["asks"]))
    return text + (f"  — {', '.join(after)}" if after else "")


# --- the Merged gate additions ---


def verdicts_at_head(conn, item_id, n, repos, head, independent=lambda record: True):
    """A row's independent verdicts sorted by where their sha sits against the ticket head, oldest first:
    (counting, stuck, outside). `counting` are at the head or an ancestor of it, the ones the Merged gate
    counts and `settle` settles with; `stuck` could not be placed by git (`None`); `outside` are placed and
    not at or below the head. Both the gate and `settle` choose by this one rule."""
    from . import gitrepo

    placed = [(v, gitrepo.contains(gitrepo.place(repos, v["sha"], head), v["sha"], head)
               if v["sha"] and head else False)
              for v in conn.execute("SELECT verdict, sha, actor, session, seq FROM ledger_verdicts"
                                    " WHERE item_id = ? AND n = ? ORDER BY seq", (item_id, n))
              if independent(v)]
    return ([v for v, inside in placed if inside is True], [v for v, inside in placed if inside is None],
            [v for v, inside in placed if inside is False])


def review_needs(conn, row, weighing):
    """What a YELLOW or RED ticket's review still lacks for Merged: a counting `audit` from an independent
    session, then per ledger row (as `ledger: "ledger-<n>"`) a latest independent verdict at the head sha
    or an ancestor of it (`ledger-verdict`, or `sha-unverifiable` when only git's answer keeps the latest
    from counting), its settlement (`ledger-settled`, or `reward-hack-fixed` for a flag), and a
    contradiction's settlement through its ask. Independence is judged now, so an audit or a verdict
    stops counting once its session or actor builds the ticket."""
    from . import gitrepo

    built = built_by(conn, row["id"])

    def independent(record):
        return not built(record["session"], record["actor"])

    audits = [r for r in weighing.rows if r["kind"] == "audit" and r["gate"] == "merged" and r["counts"]]
    missing = [] if any(independent(r) for r in audits) else [{"need": "audit"}]
    repos, head = gitrepo.repos_of(conn, row["id"]), weighing.head
    for entry in conn.execute("SELECT n, kind FROM ledger WHERE item_id = ? AND NOT EXISTS (SELECT 1 FROM"
                              " ledger_withdrawals w WHERE w.item_id = ledger.item_id AND w.n = ledger.n) ORDER BY n",
                              (row["id"],)).fetchall():
        label = handle(entry["n"])
        verdicts, stuck, outside = verdicts_at_head(conn, row["id"], entry["n"], repos, head, independent)
        if stuck and (not verdicts or stuck[-1]["seq"] > verdicts[-1]["seq"]):
            missing.append({"need": "sha-unverifiable", "ledger": label, "sha": stuck[-1]["sha"],
                            **gitrepo.tried_in(repos, stuck[-1]["sha"])})
            continue
        if not verdicts:
            need = {"need": "ledger-verdict", "ledger": label}
            if outside:  # a verdict exists, but at a sha newer than the head: say so, and how to move the head
                above = outside[-1]["sha"]
                need.update(sha=above, hint=f"the latest verdict is at {above}, above the head {head}: if {above} is "
                                            f"the real head, record pr and a passing test-run at {above}")
            missing.append(need)
            continue
        settlements = _settlements(conn, row["id"], entry["n"])
        settled = _settled(repos, entry["kind"], verdicts, settlements)
        if isinstance(settled, dict) and "stranded" in settled:  # the fix is not in the head: say why
            missing.append({"need": "ledger-settled", "ledger": label, "sha": settled["stranded"],
                            "hint": f"fix {settled['stranded']} is not in the head (rebased?): "
                                    "re-settle at its rebased copy and re-verdict"})
            continue
        if isinstance(settled, dict):  # the fix's ancestry cannot be told, so the row cannot be judged settled
            need = {"need": "sha-unverifiable", "ledger": label, "sha": settled["sha"],
                    **gitrepo.tried_in(repos, settled["sha"])}
            if not repos:  # name the ticket in the hint: `trk show` points the reader at the command to run
                need["hint"] = (f"{row['id']} has no local git checkout: "
                                f"run `trk edit {row['id']} --add-repo <absolute path>`")
            missing.append(need)
            continue
        if not settled:
            need = {"need": "reward-hack-fixed" if entry["kind"] == "reward-hack" else "ledger-settled",
                    "ledger": label}
            if settlements and settlements[-1]["seq"] > verdicts[-1]["seq"] and settlements[-1]["settlement"] == "fixed":
                fix = settlements[-1]["sha"]  # settled again after the latest verdict: that verdict does not cover it
                need.update(sha=fix, hint=f"fix {fix} was settled after the latest verdict: the auditor must "
                                          "re-verdict after the new settlement")
            missing.append(need)
    return missing


def _settlements(conn, item_id, n):
    """A row's settlements, oldest first, as (settlement, sha, seq). One that cites the verdict it relied on
    (an auditor's settle with no ask: the `ledger.settled` feed row's `verdict_seq`) is read as made just
    before that verdict, so the cited SOUND is its re-check rather than a verdict still to come."""
    found = []
    for settled in conn.execute(
            "SELECT s.settlement, s.sha, s.seq, j.data FROM ledger_settlements s LEFT JOIN journal j ON j.seq = s.seq"
            " WHERE s.item_id = ? AND s.n = ? ORDER BY s.seq", (item_id, n)):
        cited = json.loads(settled["data"]).get("verdict_seq") if settled["data"] else None
        found.append({"settlement": settled["settlement"], "sha": settled["sha"],
                      "seq": cited - 0.5 if cited is not None else settled["seq"]})
    return found


def _settled(repos, kind, verdicts, settlements):
    """Whether a row is settled, given its counting verdicts (the last decides) and its settlements:
    True, False, `{"sha": <fix sha>}` when only git's answer keeps it unsettled (the fix's ancestry cannot
    be told: no checkout, or an unknown commit), which the gate reports as `sha-unverifiable`, or
    `{"stranded": <fix sha>}` when neither the verdict's sha nor a commit with the fix's patch holds the fix.

    RISKY and WRONG need a later settlement that settles them (`settles`). A CONFIRMED flag is never
    settled; only its REFUTED re-check is. SOUND and REFUTED stand, unless a `fixed` was recorded since
    the latest RISKY, WRONG or CONFIRMED verdict before them and their sha holds neither its fix
    sha nor a commit with the same patch (a rebase keeps it; else it is not that fix's re-check); a REFUTED after a CONFIRMED needs such a fix. A contradiction
    judged SOUND also needs Michael's call: `accepted` or `overruled`, or a `fixed` so re-checked."""
    from . import gitrepo

    verdict = verdicts[-1]
    judged = verdict["verdict"]
    if judged == "confirmed":
        return False
    if settlements and settlements[-1]["seq"] > verdict["seq"] and settlements[-1]["settlement"] == "fixed":
        return False  # a fix settled after the latest verdict waits for its re-check, whatever that verdict was
    if judged in ("risky", "wrong"):
        after = [s for s in settlements if s["seq"] > verdict["seq"]]
        return bool(after) and after[-1]["settlement"] in settles(kind, judged)
    adverse = [v for v in verdicts[:-1] if v["verdict"] in ("risky", "wrong", "confirmed")]
    since = adverse[-1]["seq"] if adverse else 0
    fixes = [s for s in settlements if since < s["seq"] < verdict["seq"] and s["settlement"] == "fixed"]
    if fixes:
        contained = gitrepo.contains(gitrepo.place(repos, fixes[-1]["sha"], verdict["sha"]), fixes[-1]["sha"],
                                    verdict["sha"])
        if contained is None:
            return {"sha": fixes[-1]["sha"]}
        if not contained:
            same = gitrepo.same_patch_in(gitrepo.place(repos, fixes[-1]["sha"], verdict["sha"]),
                                         fixes[-1]["sha"], verdict["sha"])
            if same is None:
                return {"sha": fixes[-1]["sha"]}  # git failed on the patches: only its answer keeps it unsettled
            if not same:
                return {"stranded": fixes[-1]["sha"]}
    if adverse and adverse[-1]["verdict"] == "confirmed" and not fixes:
        return False
    if kind == "contradiction":
        return bool(fixes) or any(s["settlement"] in ("accepted", "overruled") for s in settlements)
    return True


def settles(kind, judged):
    """The settlements that settle a row of `kind` whose latest verdict is `judged` (RISKY or WRONG): a
    `fixed` one waits for its re-check instead. A contradiction is settled only through its ask."""
    if judged == "wrong":
        return ("overruled",)
    return ("accepted",) if kind == "contradiction" else ("deferred", "accepted")
