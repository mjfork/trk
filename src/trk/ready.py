"""The frontier: `ready` and `why_not` for one item, and the ordered walk `trk ready` returns.

`status` is the one function both `ready` and `show` read, so they agree by construction.
"""

from . import items  # items imports ready back; both bind modules only
from .errors import usage
from .store import require_actor

# Kinds and forms that never come from the frontier.
_NEVER = {("program", None), ("work_item", "ask")}


def _live_claim(conn, item_id):
    return conn.execute(
        "SELECT 1 FROM claims WHERE item_id = ? AND ended_at IS NULL", (item_id,)
    ).fetchone() is not None


def _open_blockers(conn, item_id):
    """Open items that block this one, in link order."""
    return [r["src"] for r in conn.execute(
        "SELECT edges.src FROM edges JOIN items ON items.id = edges.src"
        " WHERE edges.dst = ? AND edges.type = 'blocks' AND items.status = 'open' ORDER BY edges.rowid",
        (item_id,),
    )]


def _open_asks(conn, item_id):
    """Open child asks, in rank order."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM items WHERE parent_id = ? AND form = 'ask' AND status = 'open' ORDER BY rank", (item_id,)
    )]


def status(conn, row):
    """{"ready", "why_not": [rule…], "blocked", "in_progress"} for an item row.

    why_not lists every rule that applies, baseline first. Blocked and in progress are derived:
    an open blocker or open child ask, and a live claim.
    """
    item_id = row["id"]
    rules = []
    if row["status"] == "closed":
        rules.append("closed")
    in_progress = _live_claim(conn, item_id)
    if in_progress:
        rules.append("claimed")
    blocking = [f"blocked-by:{src}" for src in _open_blockers(conn, item_id)]
    blocking += [f"open-ask:{ask}" for ask in _open_asks(conn, item_id)]
    rules += blocking
    if (row["kind"], row["form"]) in _NEVER:
        rules.append("kind-never")
    elif row["kind"] == "project" and row["spec_state"] != "draft":
        rules.append("spec-not-draft")
    elif row["form"] == "spec_bound":
        project = items.nearest_project(conn, item_id)
        if project is None:
            rules.append("no-project")
        elif project["spec_state"] != "accepted":
            rules.append("spec-not-accepted")
    return {"ready": not rules, "why_not": rules, "blocked": bool(blocking), "in_progress": in_progress}


def marker(item):
    """The tree marker for an item's status() facts: ready, else claimed, else blocked; none when closed."""
    if item["status"] == "closed":
        return None
    if item["ready"]:
        return "ready"
    if item["in_progress"]:
        return "claimed"
    return "blocked" if item["blocked"] else None


def frontier(conn, root=None, owner=None, limit=None, stage=None, client=None):
    """Ready item rows: a depth-first walk in rank order (all of it, or the subtree at `root`),
    urgent ones first, each group in walk order; `owner` keeps one effective owner's items, `stage`
    tickets at that stage, `client` items whose effective client is that name."""
    if root is not None:
        root = items.get(conn, root)["id"]
    if owner is not None:
        require_actor(conn, owner)
    walked = [row for row, _ in items.outline(conn, root) if status(conn, row)["ready"]]
    if stage is not None:
        from .stages import STAGES  # the gates modules load only for the verbs that need them

        if stage not in STAGES:
            raise usage(f"--stage is one of {', '.join(STAGES)}, got {stage!r}")
        walked = [row for row in walked if row["stage"] == stage]
    if owner is not None:
        walked = [row for row in walked if items.hierarchy(conn, row["id"])["effective_owner"] == owner]
    if client is not None:
        walked = [row for row in walked if items.hierarchy(conn, row["id"])["effective_client"] == client]
    ordered = [row for row in walked if row["urgent"]] + [row for row in walked if not row["urgent"]]
    return ordered[:limit] if limit is not None else ordered
