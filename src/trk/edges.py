"""Edges: blocks, relates, supersedes.

`A blocks B`: B waits on A. `A supersedes B`: A replaces B. `relates` is symmetric and stored
once, src first by id. Only `blocks` edges may not form a cycle.
"""

from . import clock, items
from .errors import refused

TYPES = ("blocks", "relates", "supersedes")


def _oriented(src, edge_type, dst):
    """The (src, type, dst) an edge is stored as: relates puts the lower id first."""
    if edge_type == "relates" and dst < src:
        src, dst = dst, src
    return src, edge_type, dst


_MATCH = "src = ? AND type = ? AND dst = ?"


def _exists(conn, edge):
    return conn.execute(f"SELECT 1 FROM edges WHERE {_MATCH}", edge).fetchone() is not None


def _reaches(conn, start, goal):
    """Whether `goal` is reachable from `start` over blocks edges."""
    row = conn.execute(
        """
        WITH RECURSIVE downstream (id) AS (
            SELECT ? UNION SELECT edges.dst FROM edges JOIN downstream ON edges.src = downstream.id
            WHERE edges.type = 'blocks'
        )
        SELECT 1 FROM downstream WHERE id = ?
        """,
        (start, goal),
    ).fetchone()
    return row is not None


def _refuse_bad_link(conn, src, edge_type, dst):
    """Refuse a self-link (rule `self-link`) or a blocks edge that closes a cycle (rule `cycle`)."""
    if src == dst:
        raise refused("self-link", f"{src} cannot {edge_type} itself", item=src)
    if edge_type == "blocks" and _reaches(conn, dst, src):
        raise refused("cycle", f"{src} blocks {dst} would close a cycle: {dst} already blocks {src}, directly or through others",
                      item=src)


def _journal(tx, change, edge):
    src, edge_type, dst = edge
    tx.journal(change, items.feed_fields(tx.conn, src), data={"type": edge_type, "src": src, "dst": dst})


def _remove(tx, edge):
    if tx.conn.execute(f"DELETE FROM edges WHERE {_MATCH}", edge).rowcount:
        _journal(tx, "edge.removed", edge)


def add(tx, src, edge_type, dst):
    """Add an edge between two known ids if missing; journals `edge.added`. Returns it as stored."""
    edge = _oriented(src, edge_type, dst)
    if not _exists(tx.conn, edge):
        _refuse_bad_link(tx.conn, *edge)
        tx.conn.execute("INSERT INTO edges (src, type, dst, created_at) VALUES (?, ?, ?, ?)",
                        (*edge, clock.stamp(tx.now)))
        _journal(tx, "edge.added", edge)
    return edge


def _ids(conn, raw_src, raw_dst, edge_type):
    """(src, type, dst) for two input ids; unknown ids exit 3."""
    return items.get(conn, raw_src)["id"], edge_type, items.get(conn, raw_dst)["id"]


def link(tx, raw_src, edge_type, raw_dst):
    """`trk link`: add an edge if missing. Returns (src, type, dst) as stored."""
    return add(tx, *_ids(tx.conn, raw_src, raw_dst, edge_type))


def unlink(tx, raw_src, edge_type, raw_dst):
    """`trk unlink`: remove an edge if present; journals `edge.removed`. Returns (src, type, dst)."""
    edge = _oriented(*_ids(tx.conn, raw_src, raw_dst, edge_type))
    _remove(tx, edge)
    return edge


def supersede(tx, item_id, replacement):
    """Add `replacement supersedes item` if missing, and move every outgoing blocks edge of the
    item onto the replacement: a removed and an added row each, skipping ones it already has.
    A move that would close a cycle refuses (rule `cycle`); the caller's transaction rolls back.
    """
    add(tx, replacement, "supersedes", item_id)
    blocked = tx.conn.execute(
        "SELECT dst FROM edges WHERE src = ? AND type = 'blocks' ORDER BY rowid", (item_id,)
    ).fetchall()
    for row in blocked:
        if row["dst"] == replacement:
            raise refused("cycle", f"{item_id} blocks {replacement}, so {replacement} cannot take that edge over",
                          item=item_id)
        _remove(tx, (item_id, "blocks", row["dst"]))
        add(tx, replacement, "blocks", row["dst"])


def listing(conn, item_id):
    """Edges in and out of an item, each with the other end's id, title and status.

    `dir` is `out` when this item is the src, `in` when it is the dst; relates is `out` at both ends.
    """
    rows = conn.execute(
        """
        SELECT 'out' AS dir, edges.type, items.id, items.title, items.status, edges.rowid AS seq
        FROM edges JOIN items ON items.id = edges.dst WHERE edges.src = ?
        UNION ALL
        SELECT CASE edges.type WHEN 'relates' THEN 'out' ELSE 'in' END, edges.type, items.id, items.title,
               items.status, edges.rowid
        FROM edges JOIN items ON items.id = edges.src WHERE edges.dst = ?
        ORDER BY 1 DESC, 2, 6
        """,
        (item_id, item_id),
    ).fetchall()
    return [{"dir": r["dir"], "type": r["type"], "id": r["id"], "title": r["title"], "status": r["status"]}
            for r in rows]
