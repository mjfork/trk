"""Each item's worktree (tr-zwb4a): one recorded tree, `{path, branch, repo, session}`, and `trk where <path>`,
which names the items whose recorded tree holds a path."""

import os

from . import claims, clock, items  # gitrepo loads lazily: claim renew loads no gates module
from .errors import TrkError, REFUSED, usage


def to_json(row):
    """The item's recorded tree, `{path, branch, repo, session}`, or None when it records none."""
    if row["worktree_path"] is None:
        return None
    return {"path": row["worktree_path"], "branch": row["worktree_branch"], "repo": row["worktree_repo"],
            "session": row["worktree_session"]}


def record(tx, raw_id, path, branch=None, session=None):
    """Record the item's tree: `path` resolved with realpath (an existing directory, else exit 2), `branch`
    read from git when not given, `repo` the main checkout it belongs to, and `session` the writer's when not
    given. Replaces a tree recorded before; journals `item.updated` (field `worktree`, old and new trees)
    unless the tree is the same."""
    from . import gitrepo

    if not os.path.isdir(path):
        raise usage(f"--path {path!r} is not a directory")
    path = os.path.realpath(path)
    if branch is None:
        branch = gitrepo.branch_of(path)
    new = {"path": path, "branch": branch, "repo": gitrepo.repo_of(path), "session": session or tx.session}
    return _write(tx, raw_id, new)


def clear(tx, raw_id):
    """Remove the item's tree; journals `item.updated` unless it records none."""
    return _write(tx, raw_id, None)


def _write(tx, raw_id, new):
    row = items.get(tx.conn, raw_id)
    old = to_json(row)
    if old == new:
        return row["id"]
    values = (new["path"], new["branch"], new["repo"], new["session"], clock.stamp(tx.now)) if new else (None,) * 5
    tx.conn.execute("UPDATE items SET worktree_path = ?, worktree_branch = ?, worktree_repo = ?, worktree_session = ?,"
                    " worktree_at = ? WHERE id = ?", (*values, row["id"]))
    items.journal_updated(tx, row["id"], ["worktree"], {"worktree": old}, {"worktree": new})
    return row["id"]


def where(conn, path):
    """Who owns `path`: (tree, [entry]) for the innermost recorded tree that holds it, its items newest first.
    A path in no recorded tree is refused with rule `not-found` (exit 1), naming the git work tree that holds
    it, if any, in `worktree`."""
    path = os.path.realpath(path)
    rows = [row for row in conn.execute("SELECT * FROM items WHERE worktree_path IS NOT NULL")
            if path == row["worktree_path"] or path.startswith(row["worktree_path"].rstrip("/") + "/")]
    if not rows:
        from . import gitrepo

        found = gitrepo.toplevel(path)
        tail = f"; it is in git work tree {found}, which no item records" if found else ""
        raise TrkError(REFUSED, f"no item records a tree holding {path}{tail}", rule="not-found",
                       extra={"path": path, "worktree": found})
    tree = max((row["worktree_path"] for row in rows), key=len)
    owners = sorted((row for row in rows if row["worktree_path"] == tree), key=lambda r: r["worktree_at"],
                    reverse=True)
    return tree, [_entry(conn, row) for row in owners]


def _entry(conn, row):
    derived = items.hierarchy(conn, row["id"])
    root = items.get(conn, derived["root"])
    claim = claims.live(conn, row["id"])
    return {"id": row["id"], "title": row["title"], "kind": items.KIND_OUT[row["kind"]], "root": root["id"],
            "root_title": root["title"], "effective_owner": derived["effective_owner"], "status": row["status"],
            "stage": row["stage"], "claim": claims.to_json(claim) if claim is not None else None,
            "branch": row["worktree_branch"], "worktree": to_json(row), "archive": row["archive"],
            "effective_archive": items.effective_archive(conn, row["id"]), "recorded_at": row["worktree_at"]}
