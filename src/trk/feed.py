"""The change feed. Rows are appended by the write path (store.Transaction.journal) and read
here by `trk changes`; contract/changes.schema.json describes the response."""

import json

from . import items

DEFAULT_LIMIT = 1000

# --root R: rows rooted at R, plus item.moved rows that left R (their old root).
_ROOT_FILTER = """
    AND (root = :root OR (change = 'item.moved' AND json_extract(data, '$.old.root') = :root))
"""


def changes(conn, since, root=None, limit=DEFAULT_LIMIT):
    """The feed response: rows with seq > since in ascending seq, at most `limit` (None: all).

    `epoch` names the journal the seqs belong to: `trk restore` rotates it, so a consumer keeps
    (epoch, seq) and rebuilds from --since 0 when the epoch changes.
    `next` is the cursor for the following call: the seq of the last row scanned, or
    `head` once the scan reached the end (so rows --root filtered out are never rescanned).
    Head and rows are read in one transaction, so they come from the same snapshot.
    """
    if root is not None:
        root = items.get(conn, root)["id"]
    sql = "SELECT * FROM journal WHERE seq > :since" + (_ROOT_FILTER if root else "") + " ORDER BY seq"
    if limit is not None:
        sql += " LIMIT :limit"
    conn.execute("BEGIN")
    try:
        epoch = conn.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()[0]
        head = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM journal").fetchone()[0]
        rows = conn.execute(sql, {"since": since, "root": root, "limit": limit}).fetchall()
    finally:
        conn.execute("COMMIT")
    full_page = limit is not None and len(rows) == limit
    return {
        "schema": 1,
        "epoch": epoch,
        "head": head,
        "next": rows[-1]["seq"] if full_page else head,
        "changes": [_row_json(row) for row in rows],
    }


def _row_json(row):
    """A journal row as the feed prints it: every column in table order, data as an object."""
    out = dict(row)
    out["data"] = json.loads(out["data"])
    return out
