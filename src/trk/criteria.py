"""Acceptance criteria (tickets, projects) and destination statements (programs): versioned rows with
stable `ac-<n>` handles, the generated section text, and the one-time import of written sections.

`items` is imported inside the functions that need it: items imports this module back lazily.
"""

import re

from . import clock, ranks
from .errors import not_found, refused, usage

TEXT_MAX = 500

# The generated section of each (kind, form) that takes criteria; decisions and asks take none.
GENERATED = {
    ("program", None): "destination",
    ("project", None): "acceptance-criteria",
    ("work_item", "spec_bound"): "acceptance-criteria",
    ("work_item", "self_contained"): "acceptance-criteria",
    ("work_item", "findings"): "acceptance-criteria",
}

_HANDLE = re.compile(r"ac-([1-9][0-9]*)", re.IGNORECASE)


def generated_handle(kind, form):
    """The section generated from the criteria of an item of this (internal) kind and form, or None
    when it takes none."""
    return GENERATED.get((kind, form))


def handle(n):
    return f"ac-{n}"


def _one_line(text, needed):
    """`text` stripped, or the usage error `needed` when it is empty or spans lines."""
    text = (text or "").strip()
    if not text or "\n" in text or "\r" in text:
        raise usage(needed)
    return text


def check_text(text):
    """One non-empty line of at most TEXT_MAX chars (usage error otherwise); returns it stripped."""
    text = _one_line(text, "a criterion is one non-empty line")
    if len(text) > TEXT_MAX:
        raise usage(f"a criterion is at most {TEXT_MAX} chars (got {len(text)})")
    return text


def check_takes(row):
    """Refuse an item that takes no criteria (rule `no-criteria`)."""
    if generated_handle(row["kind"], row["form"]) is None:
        raise refused("no-criteria", f"{row['id']} is a {row['form'] or row['kind']}; it takes no criteria",
                      item=row["id"])


def excerpt(text):
    """Criterion text in a feed row: Asks and owners' plain-text excerpt (240 chars)."""
    from . import asks  # asks imports items, which imports this module lazily

    return asks.excerpt(text)


def _cut(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _rows(conn, item_id):
    return conn.execute("SELECT * FROM criteria WHERE item_id = ? ORDER BY rank", (item_id,)).fetchall()


def live(conn, item_id):
    """An item's live (unretired) criterion rows in rank order."""
    return [row for row in _rows(conn, item_id) if not row["retired"]]


def live_count(conn, item_id):
    return conn.execute("SELECT COUNT(*) FROM criteria WHERE item_id = ? AND retired = 0", (item_id,)).fetchone()[0]


def _number(raw):
    match = _HANDLE.fullmatch((raw or "").strip())
    if not match:
        raise usage(f"a criterion handle looks like ac-<n>, got {raw!r}")
    return int(match.group(1))


def get(conn, item_id, raw_handle):
    """The criterion row an `ac-<n>` handle names; exit 3 when the item has none."""
    n = _number(raw_handle)
    row = conn.execute("SELECT * FROM criteria WHERE item_id = ? AND n = ?", (item_id, n)).fetchone()
    if row is None:
        raise not_found(f"{item_id} has no criterion {handle(n)}", item=item_id)
    return row


def _writable(conn, raw_id):
    """The item row for a criterion write: it must take criteria and be open."""
    from . import items

    row = items.get(conn, raw_id)
    check_takes(row)
    if row["status"] == "closed":
        raise refused("closed", f"{row['id']} is closed", item=row["id"])
    return row


def _rank(conn, item_id, where, anchor):
    """A rank among the item's criteria (retired ones included) for --first|--last|--before|--after."""
    ordered = [r["rank"] for r in _rows(conn, item_id)]
    if where == "first":
        slot = 0
    elif where == "last":
        slot = len(ordered)
    else:
        slot = ordered.index(get(conn, item_id, anchor)["rank"]) + (where == "after")
    lo = ordered[slot - 1] if slot > 0 else None
    hi = ordered[slot] if slot < len(ordered) else None
    return ranks.key_between(lo, hi)  # criteria are few; keys are never re-keyed, only allowed to grow


def insert(tx, item_id, text, where="last", anchor=None):
    """Store one new criterion (version 1) without journaling it; returns its row."""
    conn = tx.conn
    rank = _rank(conn, item_id, where, anchor)
    n = conn.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM criteria WHERE item_id = ?", (item_id,)).fetchone()[0]
    now = clock.stamp(tx.now)
    conn.execute(
        "INSERT INTO criteria (item_id, n, text, rank, version, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
        (item_id, n, text, rank, now, now),
    )
    _keep_version(tx, item_id, n, 1, text)
    return conn.execute("SELECT * FROM criteria WHERE item_id = ? AND n = ?", (item_id, n)).fetchone()


def _keep_version(tx, item_id, n, version, text):
    tx.conn.execute(
        "INSERT INTO criterion_versions (item_id, n, version, text, actor, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, n, version, text, tx.actor, clock.stamp(tx.now)),
    )


def journal_added(tx, item_id, row, via=None):
    """`criterion.added` (handle, text excerpt, version 1, rank)."""
    from . import items

    data = {"handle": handle(row["n"]), "text": excerpt(row["text"]), "version": 1, "rank": row["rank"]}
    if via is not None:
        data["via"] = via
    tx.journal("criterion.added", items.feed_fields(tx.conn, item_id), data=data)


def _touch(tx, item_id):
    tx.conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (clock.stamp(tx.now), item_id))


def add(tx, raw_id, text, where="last", anchor=None):
    """`criterion add`: a new live criterion at any stage of an open item; returns its handle."""
    text = check_text(text)
    item_id = _writable(tx.conn, raw_id)["id"]
    row = insert(tx, item_id, text, where, anchor)
    _touch(tx, item_id)
    journal_added(tx, item_id, row)
    return handle(row["n"])


def _live(conn, item_id, raw_handle):
    row = get(conn, item_id, raw_handle)
    if row["retired"]:
        raise usage(f"{handle(row['n'])} is retired; a retired criterion never changes (add a new one)")
    return row


def edit(tx, raw_id, raw_handle, text):
    """`criterion edit`: new text at the next version, every version's text kept; unchanged text changes nothing."""
    from . import items

    text = check_text(text)
    item_id = _writable(tx.conn, raw_id)["id"]
    row = _live(tx.conn, item_id, raw_handle)
    if row["text"] == text:
        return handle(row["n"])
    version = row["version"] + 1
    tx.conn.execute("UPDATE criteria SET text = ?, version = ?, updated_at = ? WHERE item_id = ? AND n = ?",
                    (text, version, clock.stamp(tx.now), item_id, row["n"]))
    _keep_version(tx, item_id, row["n"], version, text)
    _touch(tx, item_id)
    tx.journal("criterion.edited", items.feed_fields(tx.conn, item_id),
               data={"handle": handle(row["n"]), "old_version": row["version"], "version": version,
                     "text": excerpt(text)})
    return handle(row["n"])


def retire(tx, raw_id, raw_handle, reason):
    """`criterion retire`: kept, struck through, out of every gate; the last live one is refused."""
    from . import items

    reason = _one_line(reason, "retire needs --reason \"<one line>\"")
    item_id = _writable(tx.conn, raw_id)["id"]
    row = _live(tx.conn, item_id, raw_handle)
    if live_count(tx.conn, item_id) == 1:
        raise refused("last-criterion", f"{handle(row['n'])} is {item_id}'s last live criterion; add another first",
                      item=item_id)
    tx.conn.execute("UPDATE criteria SET retired = 1, retired_reason = ?, updated_at = ? WHERE item_id = ? AND n = ?",
                    (reason, clock.stamp(tx.now), item_id, row["n"]))
    _touch(tx, item_id)
    tx.journal("criterion.retired", items.feed_fields(tx.conn, item_id),
               data={"handle": handle(row["n"]), "reason": excerpt(reason)})
    return handle(row["n"])


def listing(conn, item_id):
    """Every criterion in rank order, retired ones included, with its evidence status from its latest
    counting row (`evidence.criterion_statuses`)."""
    from . import evidence, items

    statuses = evidence.criterion_statuses(conn, items.get(conn, item_id))
    return [{
        "handle": handle(row["n"]),
        "text": row["text"],
        "version": row["version"],
        "rank": row["rank"],
        "retired": bool(row["retired"]),
        "retired_reason": row["retired_reason"],
        **statuses[row["n"]],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    } for row in _rows(conn, item_id)]


def read(conn, raw_id):
    """`trk criteria`: (item id, the listing, the generated text) for an item that takes criteria."""
    from . import items

    row = items.get(conn, raw_id)
    check_takes(row)
    return row["id"], listing(conn, row["id"]), generated(conn, row["id"]) or ""


def _line(criterion):
    """`- [x] ac-2 (v2) text — pass (…)`, `- [ ] ac-3 text — no evidence`, or `- ~~ac-4 text~~ (retired: …)`."""
    if criterion["retired"]:
        return f"- ~~{criterion['handle']} {criterion['text']}~~ (retired: {criterion['retired_reason']})"
    version = f" (v{criterion['version']})" if criterion["version"] > 1 else ""
    box = "x" if criterion["status"] == "pass" else " "
    latest = criterion["latest"]
    if latest is None:
        weighed = "no evidence"
    else:
        sha = f", {latest['sha'][:7]}" if latest.get("sha") else ""
        weighed = f"{criterion['status']} ({latest['kind']} {latest['id']}{sha})"
    return f"- [{box}] {criterion['handle']}{version} {criterion['text']} — {weighed}"


def generated(conn, item_id):
    """The generated section body: one line per criterion in rank order; None when there are none."""
    listed = listing(conn, item_id)
    return "".join(_line(c) + "\n" for c in listed) if listed else None


# --- the schema 4 migration's one-time import of written criteria ---

_ITEM = re.compile(r"^(?:[-*]|[0-9]+[.)])\s+(?:\[[ xX]\]\s*)?(.*)$")


def parse_written(body):
    """The criteria a written section held: each top-level list item collapsed to one line, or the
    whole text as one when it has no list items; each cut to TEXT_MAX chars. Not a markdown parser."""
    found, current, after_blank = [], None, False
    for line in body.splitlines():
        match = _ITEM.match(line)  # top level: the marker starts the line
        if match:
            current = [match.group(1)]
            found.append(current)
        elif not line.strip():
            after_blank = True
            continue
        elif current is not None and (not after_blank or line[0].isspace()):
            current.append(line)  # a continuation: right below the item, or indented under it
        else:
            current = None  # a paragraph after the list belongs to no criterion
        after_blank = False
    texts = [" ".join(parts) for parts in found] if found else [body]
    return [_cut(" ".join(text.split()), TEXT_MAX) for text in texts if text.strip()]
