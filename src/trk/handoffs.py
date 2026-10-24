"""Handoffs: checkpoint records kept with an item, appended one per checkpoint, never edited.

A handoff is not a section and Core never requires one; a later session reads the latest to restart.
"""

from . import clock, items
from .errors import not_found, refused, usage

MAX_BYTES = 256 * 1024
FIRST_LINE_MAX = 200


def first_line(body):
    """The body's first non-blank line, stripped and cut to FIRST_LINE_MAX chars; None for a blank body."""
    line = next((line.strip() for line in body.splitlines() if line.strip()), None)
    return None if line is None else line[:FIRST_LINE_MAX]


def add(tx, raw_id, body):
    """Append one handoff as the transaction's actor and session; journals `handoff.added`. Returns its id."""
    item_id = items.get(tx.conn, raw_id)["id"]
    line = first_line(body)
    if line is None:
        raise usage("a handoff body has no text")
    size = len(body.encode("utf-8"))
    if size > MAX_BYTES:
        raise refused("handoff-too-large", f"the handoff is {size} bytes; a handoff holds at most {MAX_BYTES}",
                      item=item_id)
    handoff_id = tx.conn.execute(
        "INSERT INTO handoffs (item_id, at, actor, session, first_line, body) VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, clock.stamp(tx.now), tx.actor, tx.session, line, body),
    ).lastrowid
    tx.journal("handoff.added", items.feed_fields(tx.conn, item_id), data={"id": handoff_id, "first_line": line})
    return handoff_id


def get(conn, handoff_id):
    """One handoff record by its id."""
    return to_json(conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone())


def latest(conn, raw_id):
    """The item's latest handoff record; exit 3 for an unknown item or one with no handoff."""
    item_id = items.get(conn, raw_id)["id"]
    row = _latest_row(conn, item_id)
    if row is None:
        raise not_found(f"{item_id} has no handoff", item=item_id)
    return to_json(row)


def listing(conn, raw_id):
    """(item id, every handoff record oldest first); exit 3 for an unknown item."""
    item_id = items.get(conn, raw_id)["id"]
    rows = conn.execute("SELECT * FROM handoffs WHERE item_id = ? ORDER BY id", (item_id,)).fetchall()
    return item_id, [to_json(row) for row in rows]


def summary(conn, item_id):
    """The latest handoff without its body, for the item JSON; None when there is none."""
    row = _latest_row(conn, item_id, "id, at, actor, session, first_line")  # every item read runs this: no body
    return None if row is None else dict(row)


def _latest_row(conn, item_id, columns="*"):
    return conn.execute(
        f"SELECT {columns} FROM handoffs WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,)
    ).fetchone()


def to_json(row):
    """A handoff row as `handoff add` and `handoff show` print it."""
    return {"id": row["id"], "item": row["item_id"], "at": row["at"], "actor": row["actor"],
            "session": row["session"], "first_line": row["first_line"], "body": row["body"]}
