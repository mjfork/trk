"""Items: creation, lookup, and the derived hierarchy facts (root, depth, effective owner)."""

import os
import re

from . import asks, claims, clock, comments, edges, handoffs, ids, ranks, ready, sections  # asks, edges, handoffs and ready import items back; all bind modules only
from .errors import gate_unmet, not_found, refused, usage
from .store import ACTOR_RE, require_actor

# Internal spelling -> CLI/JSON spelling.
KIND_OUT = {"program": "program", "project": "project", "work_item": "work-item"}
FORM_OUT = {"spec_bound": "spec-bound", "self_contained": "self-contained", "decision": "decision", "ask": "ask"}
RESOLUTION_OUT = {
    "done": "done",
    "wont_do": "wont-do",
    "superseded": "superseded",
    "out_of_scope": "out-of-scope",
    "duplicate": "duplicate",
}

KIND_IN = {out: internal for internal, out in KIND_OUT.items()}
RESOLUTION_IN = {out: internal for internal, out in RESOLUTION_OUT.items()}
FORM_IN = {out: internal for internal, out in FORM_OUT.items()}
SUBTYPES = ("research", "prototype", "grilling", "task")

# A child's kind never ranks above its parent's.
KIND_RANK = {"program": 3, "project": 2, "work_item": 1}


_LINEAGE = """
WITH RECURSIVE lineage (id, parent_id, owner, client, depth) AS (
    SELECT id, parent_id, owner, client, 0 FROM items WHERE id = ?
    UNION ALL
    SELECT items.id, items.parent_id, items.owner, items.client, lineage.depth + 1
    FROM items JOIN lineage ON items.id = lineage.parent_id
)
SELECT id, owner, client, depth FROM lineage ORDER BY depth
"""


def get(conn, raw_id):
    """The item row for an input id (Crockford-normalized); unknown ids exit 3."""
    item_id = ids.normalize(raw_id)
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise not_found(f"no item {raw_id!r}", item=raw_id)
    return row


def lineage(conn, item_id):
    """The item and its ancestors, nearest first: [(id, owner, distance)]."""
    return conn.execute(_LINEAGE, (item_id,)).fetchall()


def hierarchy(conn, item_id):
    """root, depth, effective owner and effective client of an item."""
    chain = lineage(conn, item_id)
    owner = next((row["owner"] for row in chain if row["owner"] is not None), None)
    client = next((row["client"] for row in chain if row["client"] is not None), None)
    return {"root": chain[-1]["id"], "depth": len(chain) - 1, "effective_owner": owner, "effective_client": client}


def effective_archive(conn, item_id):
    """The item's own archive location, else the nearest ancestor's; None when nothing above it sets one.
    Kept out of `hierarchy`, which feed rows read during migrations from before the column."""
    row = conn.execute(
        """
        WITH RECURSIVE up (id, parent_id, archive, depth) AS (
            SELECT id, parent_id, archive, 0 FROM items WHERE id = ?
            UNION ALL
            SELECT items.id, items.parent_id, items.archive, up.depth + 1
            FROM items JOIN up ON items.id = up.parent_id
        )
        SELECT archive FROM up WHERE archive IS NOT NULL ORDER BY depth LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    return row["archive"] if row is not None else None


def nearest_project(conn, item_id):
    """The nearest project above the item (id, spec_state), or None if there is none."""
    return conn.execute(
        """
        WITH RECURSIVE up (id, parent_id, kind, spec_state, depth) AS (
            SELECT id, parent_id, kind, spec_state, 0 FROM items WHERE id = ?
            UNION ALL
            SELECT items.id, items.parent_id, items.kind, items.spec_state, up.depth + 1
            FROM items JOIN up ON items.id = up.parent_id
        )
        SELECT id, spec_state FROM up WHERE depth > 0 AND kind = 'project' ORDER BY depth LIMIT 1
        """,
        (item_id,),
    ).fetchone()


def feed_fields(conn, item_id):
    """The item-describing columns of a feed row, after the change."""
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    derived = hierarchy(conn, item_id)
    return {
        "item": row["id"],
        "kind": KIND_OUT[row["kind"]],
        "form": FORM_OUT.get(row["form"]),
        "title": row["title"],
        "parent": row["parent_id"],
        "root": derived["root"],
        "depth": derived["depth"],
        "owner": derived["effective_owner"],
        "client": derived["effective_client"],
    }


def check_client(client):
    """A client name under the actor-name rule (lowercase letters, digits and `-`); anything else exits 2."""
    if not ACTOR_RE.match(client) or client == "none":
        raise usage(f"a client name is lowercase letters, digits and '-' ('none' clears it), got {client!r}")
    return client


def check_archive(archive):
    """An archive location: a one-line path template, `<tree>` standing for the worktree's directory name.
    Stored as written (no `~` expansion: whoever archives expands it); blank or multi-line exits 2."""
    if not archive.strip() or "\n" in archive or "\r" in archive:
        raise usage(f"an archive location is one non-empty line ('none' clears it), got {archive!r}")
    return archive.strip()


def check_title(title):
    title = title.strip()
    if not title or "\n" in title:
        raise usage("a title is one non-empty line")
    return title


def _check_values(jira_url, repos):
    if jira_url is not None and not jira_url.strip():
        raise usage("--jira needs a URL (use --no-jira to clear one)")
    if any(not repo.strip() for repo in repos):
        raise usage("a repo is a non-empty path or URL")


def _check_form(kind, form, subtype):
    """Work items need a form, decisions a subtype; nothing else takes either."""
    if form == "ask":
        raise usage("asks are opened with `trk ask <parent> --to <role> \"<title>\" --question @file|-`")
    if kind == "work_item" and form is None:
        raise usage("a work item needs --form spec-bound|self-contained|decision")
    if kind != "work_item" and form is not None:
        raise usage(f"--form applies to work items, not a {KIND_OUT[kind]}")
    if form == "decision" and subtype is None:
        raise usage("a decision needs --subtype research|prototype|grilling|task")
    if form != "decision" and subtype is not None:
        raise usage("--subtype applies to decisions only")


def check_nesting(parent, kind):
    """Refuse a child kind that ranks above its parent's kind (rule `nesting`)."""
    if parent is not None and KIND_RANK[kind] > KIND_RANK[parent["kind"]]:
        raise refused(
            "nesting",
            f"a {KIND_OUT[kind]} cannot sit under {KIND_OUT[parent['kind']]} {parent['id']}",
            item=parent["id"],
        )


def check_parent_open(parent):
    """Refuse an open item under a closed parent (rule `parent-closed`)."""
    if parent is not None and parent["status"] == "closed":
        raise refused("parent-closed", f"{parent['id']} is closed", item=parent["id"])


def check_not_ask(parent):
    """Refuse a child under an ask: asks are leaves (rule `ask-leaf`)."""
    if parent is not None and parent["form"] == "ask":
        raise refused("ask-leaf", f"{parent['id']} is an ask; asks are leaves", item=parent["id"])


def check_cycle(conn, item_id, parent):
    """Refuse moving an item under itself or one of its descendants (rule `cycle`)."""
    if parent is not None and any(row["id"] == item_id for row in lineage(conn, parent["id"])):
        raise refused("cycle", f"{parent['id']} is {item_id} or sits under it", item=item_id)


def place(tx, parent_id, where="last", anchor=None, moving=None):
    """The rank for an item placed under `parent_id` (None: top level).

    `where` is first, last, before or after; before/after name an `anchor` sibling. `moving` is
    the item being placed, left out of its own siblings. If the key would pass ranks.MAX_LENGTH,
    the siblings are re-keyed evenly here, each journaled as `item.moved` with `rekey: true`.
    """
    siblings = _siblings(tx.conn, parent_id, moving)
    slot = _slot(tx.conn, siblings, parent_id, where, anchor, moving)
    lo = siblings[slot - 1]["rank"] if slot > 0 else None
    hi = siblings[slot]["rank"] if slot < len(siblings) else None
    key = ranks.key_between(lo, hi)
    if len(key) <= ranks.MAX_LENGTH:
        return key
    keys = ranks.spread(len(siblings) + 1)
    key = keys.pop(slot)
    for row, new_rank in zip(siblings, keys):
        if new_rank != row["rank"]:
            _rekey(tx, row["id"], row["rank"], new_rank)
    return key


def _rekey(tx, item_id, old_rank, new_rank):
    conn = tx.conn
    conn.execute("UPDATE items SET rank = ? WHERE id = ?", (new_rank, item_id))
    fields = feed_fields(conn, item_id)
    spot = {"parent": fields["parent"], "root": fields["root"], "depth": fields["depth"]}
    tx.journal(
        "item.moved",
        fields,
        data={"old": {**spot, "rank": old_rank}, "new": {**spot, "rank": new_rank}, "rekey": True},
    )


def _insert_repos(conn, item_id, repos):
    conn.executemany("INSERT INTO item_repos (item_id, repo) VALUES (?, ?)", [(item_id, r) for r in repos])


def _unique(values):
    return list(dict.fromkeys(values))


REMOTE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://|^[\w.-]+@[\w.-]+:")  # a URL or scp-style remote, not a path


def is_relative_repo(repo):
    """Whether a stored repo is a path that resolves against the caller's directory: not a remote, not
    absolute and not `~`-led."""
    return not (REMOTE.match(repo) or repo.startswith(("/", "~")))


def absolute_repos(repos):
    """The repos as written to the store: remotes and absolute paths as given, `~` expanded, a relative
    path made absolute against the current directory, which must hold a git checkout (usage error)."""
    from . import gitrepo

    result = []
    for repo in repos:
        if REMOTE.match(repo):
            result.append(repo)
            continue
        path = os.path.expanduser(repo)
        if not os.path.isabs(path):
            path = os.path.abspath(path)
            if not gitrepo.is_checkout(path):
                raise usage(f"repo {repo!r} is relative and {path} is not a git checkout; give an absolute path"
                            " or run from inside the checkout")
        result.append(path)
    return result


def plan_absolutize(conn, root):
    """What `repos absolutize` would do: ({id, old, new} per item with a relative repo it can place,
    [{item, repo}] per relative repo it cannot). A relative repo is placed at `<root>/<repo>` when that
    is a git checkout; the rest of an item's repos keep their order and stay as they are."""
    from . import gitrepo

    changes, unresolved = [], []
    for row in conn.execute("SELECT DISTINCT item_id FROM item_repos ORDER BY rowid"):
        old = repos(conn, row["item_id"])
        new = []
        for repo in old:
            if is_relative_repo(repo):
                candidate = os.path.abspath(os.path.join(root, repo))
                if gitrepo.is_checkout(candidate):
                    repo = candidate
                else:
                    unresolved.append({"item": row["item_id"], "repo": repo})
            new.append(repo)
        new = _unique(new)
        if new != old:
            changes.append({"id": row["item_id"], "old": old, "new": new})
    return changes, unresolved


def apply_absolutize(tx, changes):
    """Write a plan: each item's repos replaced, one `item.updated` row per item (fields repos, old, new)."""
    conn = tx.conn
    for change in changes:
        conn.execute("DELETE FROM item_repos WHERE item_id = ?", (change["id"],))
        _insert_repos(conn, change["id"], change["new"])
        conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (clock.stamp(tx.now), change["id"]))
        tx.journal("item.updated", feed_fields(conn, change["id"]),
                   data={"fields": ["repos"], "old": {"repos": change["old"]}, "new": {"repos": change["new"]}})


def _check_criteria(kind, form, handles, criterion_texts):
    """Criteria go with the items that take them, never as a written generated section; a program
    needs at least one (its destination)."""
    from . import criteria

    generated = criteria.generated_handle(kind, form)
    if generated is not None and generated in handles:
        raise refused("generated-section", f"{generated} is generated from criterion rows; give each with "
                                           "--criterion \"<text>\" (later: trk criterion add)")
    if criterion_texts and generated is None:
        raise refused("no-criteria", f"a {FORM_OUT.get(form) or KIND_OUT[kind]} takes no criteria")
    if kind == "program" and not criterion_texts:
        raise usage("a program needs at least one --criterion \"<destination statement>\" (repeatable), e.g. "
                    "trk new program \"<title>\" --criterion \"<where it ends up>\"")
    return [criteria.check_text(text) for text in criterion_texts]


def create(tx, kind, title, *, form=None, subtype=None, parent=None, where="last", anchor=None,
           urgent=False, jira_url=None, repos=(), owner=None, section_bodies=(), criterion_texts=(), client=None):
    """Create an item of any kind (CLI spellings in). Returns its id.

    Checks run in the spec's order: unknown parent (exit 3), the nesting rule, an open item under a
    closed parent (rule `parent-closed`), then an ask as the parent (rule `ask-leaf`). An owner must
    be a registered actor (rule `unknown-actor`). A top-level item created without one gets the
    store's default owner, so every top-level item has its own. A `client` is a name under the actor-name
    rule (exit 2 otherwise); it is stored as the item's own, and its feed rows carry the effective one.
    `section_bodies` is [(handle, body)]; their handles are listed on `item.created`, with no row each.
    `criterion_texts` become ac-1, ac-2… in order, one `criterion.added` row each after `item.created`.
    A ticket must then hold the `framed` gate (rule `gate-unmet`, nothing written) and is born at the
    last stage it reaches from data, one `stage.changed` row per stage entered.
    """
    from . import criteria, stages

    conn = tx.conn
    kind = KIND_IN[kind]
    form = FORM_IN[form] if form is not None else None
    title = check_title(title)
    _check_form(kind, form, subtype)
    _check_values(jira_url, repos)
    repos = absolute_repos(repos)
    handles = sections.check_all(section_bodies)
    criterion_texts = _check_criteria(kind, form, handles, criterion_texts)
    parent_row = get(conn, parent) if parent is not None else None
    check_nesting(parent_row, kind)
    check_parent_open(parent_row)
    check_not_ask(parent_row)
    parent_id = parent_row["id"] if parent_row is not None else None
    if owner is not None:
        require_actor(conn, owner)
    elif parent_id is None:
        owner = tx.store.meta("default_owner")
    if client is not None:
        client = check_client(client)
    repos = _unique(repos)
    item_id, rank = insert(tx, kind, title, form=form, subtype=subtype, parent_id=parent_id, where=where,
                           anchor=anchor, urgent=urgent, jira_url=jira_url, repos=repos, owner=owner,
                           section_bodies=section_bodies, client=client)
    added = [criteria.insert(tx, item_id, text) for text in criterion_texts]
    path = stages.birth(tx, item_id) if stages.is_ticket(get(conn, item_id)) else []
    data = created_data(subtype=subtype, rank=rank, urgent=urgent, jira_url=jira_url, repos=repos, sections=handles,
                        owner=owner)
    data.update(stage=path[-1] if path else None, criteria=[criteria.handle(row["n"]) for row in added])
    tx.journal("item.created", feed_fields(conn, item_id), status_new="open", data=data)
    for row in added:
        criteria.journal_added(tx, item_id, row)
    stages.journal_moves(tx, item_id, None, path, "create")
    return item_id


def created_data(*, rank, urgent, sections, subtype=None, jira_url=None, repos=(), owner=None):
    """The data of an item's creation row: `item.created`, which `ask.opened` extends."""
    return {"subtype": subtype, "rank": rank, "urgent": bool(urgent), "jira_url": jira_url, "repos": list(repos),
            "sections": sections, "owner": owner}


def insert(tx, kind, title, *, form=None, subtype=None, parent_id=None, where="last", anchor=None, urgent=False,
           jira_url=None, repos=(), owner=None, section_bodies=(), addressee_role=None, asker=None, client=None):
    """Write a checked new item's rows (internal spellings in), without journaling. Returns (id, rank)."""
    conn = tx.conn
    rank = place(tx, parent_id, where, anchor)
    item_id = ids.new_id(
        tx.store.meta("id_prefix"),
        lambda candidate: conn.execute("SELECT 1 FROM items WHERE id = ?", (candidate,)).fetchone(),
    )
    now = clock.stamp(tx.now)
    conn.execute(
        "INSERT INTO items (id, kind, form, subtype, title, parent_id, rank, urgent, owner, spec_state, jira_url,"
        " created_at, updated_at, addressee_role, asker, client) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, kind, form, subtype, title, parent_id, rank, int(urgent), owner,
         "draft" if kind == "project" else None, jira_url, now, now, addressee_role, asker, client),
    )
    _insert_repos(conn, item_id, repos)
    from . import release

    for handle, body in section_bodies:
        sections.put(conn, item_id, handle, body, tx.now)
        release.attest(tx, item_id, handle, None, body)
    return item_id, rank


def _position(conn, item_id):
    fields = feed_fields(conn, item_id)
    rank = conn.execute("SELECT rank FROM items WHERE id = ?", (item_id,)).fetchone()["rank"]
    return {"parent": fields["parent"], "root": fields["root"], "depth": fields["depth"], "rank": rank}


def move(tx, raw_id, *, parent=None, top=False, where="last", anchor=None):
    """Reparent (`parent`, or `top` for the top level) and/or reorder an item among its siblings.

    Without `parent` or `top` it stays under its parent. An ask never changes parent (rule
    `ask-parent-fixed`, checked first). Then checks run in the spec's order: unknown parent (exit 3),
    `cycle`, `nesting`, `parent-closed` for an open item, and `ask-leaf` for an ask as the parent. Only the moved
    item's rank is rewritten, unless the new siblings need re-keying. One `item.moved` row (old
    and new position); descendants get no rows. Moving to where it already is changes nothing.
    Lifting an item with no own owner to the top level first stores its effective owner as its
    own (`owner.changed`, reason `moved-top`), so every top-level item keeps one. Open asks whose
    addressee the move changes get `ask.readdressed` rows (cause `moved`) after `item.moved`.
    """
    conn = tx.conn
    row = get(conn, raw_id)
    item_id = row["id"]
    if row["form"] == "ask" and (top or (parent is not None and ids.normalize(parent) != row["parent_id"])):
        raise refused("ask-parent-fixed", f"{item_id} is an ask; an ask never changes parent, open a new one instead",
                      item=item_id)
    if top:
        parent_row = None
    elif parent is not None:
        parent_row = get(conn, parent)
    else:
        parent_row = get(conn, row["parent_id"]) if row["parent_id"] is not None else None
    check_cycle(conn, item_id, parent_row)
    check_nesting(parent_row, row["kind"])
    if row["status"] == "open":
        check_parent_open(parent_row)
    check_not_ask(parent_row)
    parent_id = parent_row["id"] if parent_row is not None else None
    same_parent = parent_id == row["parent_id"]
    if same_parent and _slot(conn, _siblings(conn, parent_id, item_id), parent_id, where, anchor, item_id) == _index(conn, row):
        return item_id
    routed = asks.snapshot(conn) if not same_parent else {}  # a reorder changes no addressee
    if parent_id is None and row["owner"] is None:
        set_own_owner(tx, item_id, hierarchy(conn, item_id)["effective_owner"], reason="moved-top")
    old = _position(conn, item_id)
    rank = place(tx, parent_id, where, anchor, moving=item_id)
    conn.execute(
        "UPDATE items SET parent_id = ?, rank = ?, updated_at = ? WHERE id = ?",
        (parent_id, rank, clock.stamp(tx.now), item_id),
    )
    tx.journal("item.moved", feed_fields(conn, item_id), data={"old": old, "new": _position(conn, item_id)})
    asks.reroute(tx, routed, "moved")
    return item_id


def _index(conn, row):
    """The item's current index among its siblings (how many rank before it)."""
    return conn.execute(
        "SELECT COUNT(*) FROM items WHERE parent_id IS ? AND rank < ?", (row["parent_id"], row["rank"])
    ).fetchone()[0]


def _siblings(conn, parent_id, moving=None):
    """The children of `parent_id` (None: top level) other than `moving`, in rank order."""
    return conn.execute(
        "SELECT id, rank FROM items WHERE parent_id IS ? AND id IS NOT ? ORDER BY rank", (parent_id, moving)
    ).fetchall()


def _slot(conn, siblings, parent_id, where, anchor, moving):
    """The index among `siblings` that `where` names (rule `not-sibling` for a stray anchor)."""
    if where == "first":
        return 0
    if where == "last":
        return len(siblings)
    anchor_row = get(conn, anchor)
    if anchor_row["parent_id"] != parent_id or anchor_row["id"] == moving:
        where_to = f"under {parent_id}" if parent_id else "at the top level"
        raise refused("not-sibling", f"{anchor_row['id']} is not a sibling {where_to}", item=anchor_row["id"])
    return [row["id"] for row in siblings].index(anchor_row["id"]) + (where == "after")


def children(conn, item_id, open_only=False):
    """Child rows of an item (None: top-level items), in rank order."""
    status = " AND status = 'open'" if open_only else ""
    return conn.execute(
        f"SELECT * FROM items WHERE parent_id IS ?{status} ORDER BY rank", (item_id,)
    ).fetchall()


def outline(conn, item_id=None, include_closed=False):
    """Open items (all items with include_closed) in rank order, depth-first: [(row, level)].

    With an id, start at that item.
    """
    result = []

    def walk(parent_id, level):
        for row in children(conn, parent_id, open_only=not include_closed):
            result.append((row, level))
            walk(row["id"], level + 1)

    if item_id is None:
        walk(None, 0)
    else:
        row = get(conn, item_id)
        result.append((row, 0))
        walk(row["id"], 1)
    return result


def repos(conn, item_id):
    """The item's repos in the order added."""
    return [r["repo"] for r in conn.execute("SELECT repo FROM item_repos WHERE item_id = ? ORDER BY rowid", (item_id,))]


def edit(tx, raw_id, *, title=None, urgent=None, jira_url=None, clear_jira=False, add_repos=(), remove_repos=(),
         owner=None, clear_owner=False, kind=None, form=None, client=None, clear_client=False, archive=None,
         clear_archive=False):
    """Change scalar fields; journals `item.updated` (fields, old, new) only if something changed.
    `client` sets the item's own client name (exit 2 unless it follows the actor-name rule) and
    `clear_client` clears it so it inherits again; old and new are the own values, the row's client the
    effective one after the change. Descendants that inherit it get no rows. `archive` and `clear_archive` do
    the same for the item's own archive location (check_archive), which descendants inherit as
    `effective_archive`.

    The item's own owner is not one of those fields: changing it journals `owner.changed` (old,
    new) instead, after any `item.updated`, and both rows carry the new effective owner.
    Descendants whose effective owner follows get no rows. A top-level item must keep its own
    owner, so `clear_owner` on one is refused (rule `owner-required`). Kind and form are
    immutable, so passing either is refused. Adding a repo already there, or removing one that
    is not, changes nothing. An ask never has its own owner (rule `ask-no-owner`). Open asks whose
    addressee the owner change changes get `ask.readdressed` rows (cause `owner-changed`) after it.
    """
    if kind is not None or form is not None:
        raise usage("kind and form are immutable; close the item as superseded by a new one")
    _check_values(jira_url, add_repos)
    add_repos = absolute_repos(add_repos)
    conn = tx.conn
    row = get(conn, raw_id)
    item_id = row["id"]
    if owner is not None and row["form"] == "ask":
        raise refused("ask-no-owner", f"{item_id} is an ask; it is owned by its parent's owner", item=item_id)
    if clear_owner and row["parent_id"] is None:
        raise refused("owner-required", f"{item_id} is top-level and must keep its own owner; use --owner to change it",
                      item=item_id)
    old, new = {}, {}
    if title is not None:
        title = check_title(title)
        if title != row["title"]:
            old["title"], new["title"] = row["title"], title
    if urgent is not None and bool(urgent) != bool(row["urgent"]):
        old["urgent"], new["urgent"] = bool(row["urgent"]), bool(urgent)
    target_jira = None if clear_jira else jira_url
    if (clear_jira or jira_url is not None) and target_jira != row["jira_url"]:
        old["jira_url"], new["jira_url"] = row["jira_url"], target_jira
    if client is not None:
        client = check_client(client)
    target_client = None if clear_client else client
    if (clear_client or client is not None) and target_client != row["client"]:
        old["client"], new["client"] = row["client"], target_client
    if archive is not None:
        archive = check_archive(archive)
    target_archive = None if clear_archive else archive
    if (clear_archive or archive is not None) and target_archive != row["archive"]:
        old["archive"], new["archive"] = row["archive"], target_archive
    repos_before = repos(conn, item_id)
    removed = {*remove_repos, *(os.path.abspath(os.path.expanduser(r)) for r in remove_repos if not REMOTE.match(r))}
    repos_after = [r for r in _unique([*repos_before, *add_repos]) if r not in removed]
    if repos_after != repos_before:
        old["repos"], new["repos"] = repos_before, repos_after
    if owner is not None:
        require_actor(conn, owner)
    owner_changed = (clear_owner or owner is not None) and owner != row["owner"]
    if not new and not owner_changed:
        return item_id
    routed = asks.snapshot(conn) if owner_changed else None
    if "title" in new:
        conn.execute("UPDATE items SET title = ? WHERE id = ?", (new["title"], item_id))
    if "urgent" in new:
        conn.execute("UPDATE items SET urgent = ? WHERE id = ?", (int(new["urgent"]), item_id))
    if "jira_url" in new:
        conn.execute("UPDATE items SET jira_url = ? WHERE id = ?", (new["jira_url"], item_id))
    if "client" in new:
        conn.execute("UPDATE items SET client = ? WHERE id = ?", (new["client"], item_id))
    if "archive" in new:
        conn.execute("UPDATE items SET archive = ? WHERE id = ?", (new["archive"], item_id))
    if "repos" in new:
        conn.execute("DELETE FROM item_repos WHERE item_id = ?", (item_id,))
        _insert_repos(conn, item_id, repos_after)
    if owner_changed:
        conn.execute("UPDATE items SET owner = ? WHERE id = ?", (owner, item_id))
    conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (clock.stamp(tx.now), item_id))
    fields = feed_fields(conn, item_id)
    if new:
        tx.journal("item.updated", fields, data={"fields": list(new), "old": old, "new": new})
    if owner_changed:
        tx.journal("owner.changed", fields, data={"old": row["owner"], "new": owner})
        asks.reroute(tx, routed, "owner-changed")
    return item_id


def set_own_owner(tx, item_id, owner, reason):
    """Give an item its own owner outside `edit`; journals `owner.changed` with `data.reason`."""
    old = tx.conn.execute("SELECT owner FROM items WHERE id = ?", (item_id,)).fetchone()["owner"]
    tx.conn.execute("UPDATE items SET owner = ?, updated_at = ? WHERE id = ?", (owner, clock.stamp(tx.now), item_id))
    tx.journal("owner.changed", feed_fields(tx.conn, item_id), data={"old": old, "new": owner, "reason": reason})


def journal_updated(tx, item_id, fields, old, new):
    """Bump updated_at and journal `item.updated` (fields, old, new)."""
    tx.conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (clock.stamp(tx.now), item_id))
    tx.journal("item.updated", feed_fields(tx.conn, item_id), data={"fields": fields, "old": old, "new": new})


def set_section(tx, raw_id, handle, body):
    """Write one section; journals `item.updated` with field `section:<handle>` and no values, unless unchanged."""
    from . import release

    sections.check_handle(handle)
    item_id = sections.check_written(get(tx.conn, raw_id), handle)
    sections.check_body(handle, body, item=item_id)
    if sections.body(tx.conn, item_id, handle) == body:
        return item_id
    old = sections.body(tx.conn, item_id, handle)
    sections.put(tx.conn, item_id, handle, body, tx.now)
    release.attest(tx, item_id, handle, old, body)
    journal_updated(tx, item_id, [f"section:{handle}"], {}, {})
    _refresh_red_asks(tx, item_id, handle)
    return item_id


def remove_section(tx, raw_id, handle):
    """Remove one section, journaled like a write; removing an absent one changes nothing."""
    from . import release

    sections.check_handle(handle)
    item_id = sections.check_written(get(tx.conn, raw_id), handle)
    old = sections.body(tx.conn, item_id, handle)
    if not sections.delete(tx.conn, item_id, handle):
        return item_id
    release.attest(tx, item_id, handle, old, None)
    journal_updated(tx, item_id, [f"section:{handle}"], {}, {})
    _refresh_red_asks(tx, item_id, handle)
    return item_id


def _refresh_red_asks(tx, item_id, handle):
    """A ticket's `review-brief` changed: its open RED-read asks show the new text."""
    from . import blast

    if handle == blast.BRIEF_HANDLE:
        refresh_builder_asks(tx, item_id)


def refresh_builder_asks(tx, item_id):
    """Rewrite a ticket's open RED-read asks from the record as it is now: its `review-brief` changed, or
    its builder record did (`pr`/`test-run` evidence, a `builder` ledger row, a claim before Built), which
    changes who the brief's byline may call independent."""
    from . import blast, stages

    row = get(tx.conn, item_id)
    if stages.is_ticket(row):
        blast.refresh_red_asks(tx, row)


OUTCOME_MAX = 300


def check_outcome(outcome, missing, limit=OUTCOME_MAX, name="an outcome"):
    """A one-line text of at most `limit` chars, stripped: an outcome, or a reason that becomes part of
    one. Usage errors otherwise; `missing` is the message for an empty one, `name` what it is called."""
    outcome = (outcome or "").strip()
    if not outcome:
        raise usage(missing)
    if "\n" in outcome or "\r" in outcome:
        raise usage(f"{name} is one line, no newline")
    if len(outcome) > limit:
        raise usage(f"{name} is at most {limit} chars (got {len(outcome)})")
    return outcome


# Resolutions that name another item: the flag and `item.closed` data key, and the edges they write.
_CLOSE_EDGES = {
    "superseded": ("by", lambda tx, item_id, other: edges.supersede(tx, item_id, other)),
    "duplicate": ("of", lambda tx, item_id, other: edges.add(tx, item_id, "relates", other)),
}


def _other_item(resolution, by, of):
    """The raw id `--by` or `--of` names, after checking each goes with its resolution only."""
    given = {"by": by, "of": of}
    for resolution_needing, (key, _) in _CLOSE_EDGES.items():
        if given[key] is None and resolution == resolution_needing:
            raise usage(f"--resolution {resolution} needs --{key} <id>")
        if given[key] is not None and resolution != resolution_needing:
            raise usage(f"--{key} goes with --resolution {resolution_needing} only")
    return given[_CLOSE_EDGES[resolution][0]] if resolution in _CLOSE_EDGES else None


def close(tx, raw_id, resolution, outcome, by=None, of=None):
    """Close an item with a resolution (CLI spelling) and a one-line outcome; journals `item.closed`.

    An open ask among the item's children or its descendants' refuses first with rule `open-asks`,
    naming each: answer it or `withdraw` it on purpose (tr-jwbg9; a close never withdraws one).
    Then children close first: any other open child refuses with rule `open-children`, naming them.
    Then `done` on a project, program or decision checks gate `close` (rule `gate-unmet`).
    `superseded` takes `by`, the replacement: it adds `by supersedes id` and moves the item's
    outgoing blocks edges onto it (edges.supersede). `duplicate` takes `of` and adds a relates
    edge. `item.closed` data carries `by` or `of`. Only an ask may name an ask there: any other item is
    refused with rule `ask-cannot-replace`. A live claim ends right after it (`claim.released`,
    reason closed), then the edge rows follow. An ask closes only as a withdrawal (asks.check_close),
    journaled `ask.withdrawn` in place of `item.closed`. A ticket closes `done` only by reaching
    Verified (close_verified); asked here it is refused with `done-needs-verified`.
    """
    return _close(tx, raw_id, resolution, outcome, by, of, verified=False)


def close_verified(tx, item_id, outcome):
    """Close a ticket `done` as it reaches Verified (stages.advance), through the same path as `close`."""
    return _close(tx, item_id, "done", outcome, None, None, verified=True)


def _close(tx, raw_id, resolution, outcome, by, of, verified):
    from . import stages

    if resolution not in RESOLUTION_IN:
        raise usage(f"unknown resolution {resolution!r}")
    raw_other = _other_item(resolution, by, of)
    outcome = check_outcome(outcome, "close needs --outcome \"<one sentence>\"")
    conn = tx.conn
    row = get(conn, raw_id)
    item_id = row["id"]
    is_ask = row["form"] == "ask"
    if is_ask:
        asks.check_close(row, resolution)
    other_row = get(conn, raw_other) if raw_other is not None else None
    other = other_row["id"] if other_row is not None else None
    if row["status"] == "closed":
        raise refused("already-closed", f"{item_id} is already closed", item=item_id)
    if other_row is not None and other_row["form"] == "ask" and not is_ask:
        raise refused("ask-cannot-replace", f"{other} is an ask, a question rather than work: it cannot replace"
                      f" or duplicate {item_id}", item=item_id)
    if is_ask:
        asks.check_withdraw(tx, row)
    open_asks = _open_asks_below(conn, item_id)
    if open_asks:
        listed = "; ".join(f"{a['id']} ({a['title']})" for a in open_asks)
        routes = " or ".join(f"`trk answer {a['id']} …` / `trk withdraw {a['id']} --reason …`" for a in open_asks[:1])
        raise refused("open-asks", f"{item_id} has open asks; answer each or withdraw it on purpose: {listed}."
                      f" For each, {routes}", item=item_id)
    open_children = [c for c in children(conn, item_id, open_only=True) if c["form"] != "ask"]
    if open_children:
        listed = ", ".join(f"{c['id']} ({c['title']})" for c in open_children)
        raise refused("open-children", f"{item_id} has open children; close them first: {listed}", item=item_id)
    if resolution == "done" and not verified and stages.is_ticket(row):
        raise refused("done-needs-verified", f"{item_id} is a ticket: it closes done only by reaching verified"
                      f" (`trk advance {item_id} --outcome \"<one sentence>\"` from merged); any other"
                      " resolution closes it at any stage", item=item_id)
    if resolution == "done":
        _check_close_gate(conn, row)
    mark_closed(tx, item_id, resolution, outcome)
    data = {"resolution": resolution, "outcome": outcome}
    if other is not None:
        key, write_edges = _CLOSE_EDGES[resolution]
        data[key] = other
    if is_ask:
        asks.journal_withdrawn(tx, row, data, reason=None)
    else:
        tx.journal("item.closed", feed_fields(conn, item_id), status_old="open", status_new="closed", data=data)
    claims.end_on_close(tx, item_id)
    if other is not None:
        write_edges(tx, item_id, other)
    return item_id


def _open_asks_below(conn, item_id):
    """Open asks among an item's children and, through its open children, among its descendants."""
    found = []
    for child in children(conn, item_id, open_only=True):
        if child["form"] == "ask":
            found.append(child)
        else:
            found.extend(_open_asks_below(conn, child["id"]))
    return found


def _check_close_gate(conn, row):
    """Closing a project, program or decision `done` needs gate `close` (rule `gate-unmet` otherwise)."""
    from . import release, stages

    if stages.closes_gated(row):
        # `release-run-check` is `show`'s stand-in for the run check made just below
        missing = [m for m in stages.gate(conn, row, "close") if m["need"] != release.PENDING]
        if missing:
            raise gate_unmet(row["id"], "close", missing)
        release.verify(conn, row)  # a program's run evidence is read against its run (gh) only when it closes


def mark_closed(tx, item_id, resolution, outcome):
    """Set an item closed with a resolution (CLI spelling) and outcome, without journaling."""
    now = clock.stamp(tx.now)
    tx.conn.execute(
        "UPDATE items SET status = 'closed', resolution = ?, outcome = ?, closed_at = ?, updated_at = ? WHERE id = ?",
        (RESOLUTION_IN[resolution], outcome, now, now, item_id),
    )


def reopen(tx, raw_id, reason=None, to_stage=None):
    """Reopen a closed item, clearing resolution, outcome and closed_at; journals `item.reopened`.

    Refused while the parent is closed (rule `parent-closed`). An ask reopens through asks.reopen,
    which takes the `reason` only an ask has. A ticket closed at Verified takes `to_stage` (a stage
    before Verified) and `reason`, and moves back there after `item.reopened` (`via: reopen`, with
    reopen-stage's cut-off); any other item takes neither.
    """
    from . import stages

    conn = tx.conn
    row = get(conn, raw_id)
    item_id = row["id"]
    at_verified = row["status"] == "closed" and stages.is_ticket(row) and row["stage"] == "verified"
    if to_stage is not None and not at_verified:
        raise usage("--to-stage goes with reopening a ticket closed at verified only")
    if row["form"] == "ask":
        return asks.reopen(tx, row, reason)
    if at_verified:
        if to_stage is None:
            raise usage(f"{item_id} closed at verified: reopening it needs --to-stage <stage> (one before"
                        " verified) and --reason \"<one line>\"")
        reason = stages.check_reason(reason, "reopening a ticket closed at verified")
        if to_stage not in stages.STAGES[:-1]:
            raise usage(f"--to-stage is one of {', '.join(stages.STAGES[:-1])}, got {to_stage!r}")
    elif reason is not None:
        raise usage("--reason goes with reopening an ask, or a ticket closed at verified, only")
    if row["status"] != "closed":
        raise refused("not-closed", f"{item_id} is not closed", item=item_id)
    check_parent_open(get(conn, row["parent_id"]) if row["parent_id"] is not None else None)
    mark_open(tx, item_id)
    tx.journal("item.reopened", feed_fields(conn, item_id), status_old="closed", status_new="open",
               data={"resolution": RESOLUTION_OUT[row["resolution"]]})
    if at_verified:
        stages.move_back(tx, row, to_stage, "reopen", reason)
    return item_id


def mark_open(tx, item_id):
    """Set a closed item open again, clearing resolution, outcome and closed_at, without journaling."""
    tx.conn.execute(
        "UPDATE items SET status = 'open', resolution = NULL, outcome = NULL, closed_at = NULL, updated_at = ?"
        " WHERE id = ?",
        (clock.stamp(tx.now), item_id),
    )


SPEC_STATES = ("draft", "accepted")


def set_spec(tx, raw_id, state, reason=None):
    """Set a project's spec state (draft or accepted); journals `spec.changed` (old, new).

    Refused on non-projects (rule `not-project`) and closed projects (rule `closed`). Setting the
    state it already has changes nothing. Accepting checks gate `accept` and records the acceptor and
    the deciding judgment; a return to draft needs a one-line `reason`. Either way the project's
    tickets follow (stages.follow_spec), their `stage.changed` rows after `spec.changed`.
    """
    from . import stages

    if state not in SPEC_STATES:
        raise usage(f"unknown spec state {state!r}")
    if state == "draft":
        reason = check_outcome(reason, "spec draft needs --reason \"<one line>\"", name="a reason")
    elif reason is not None:
        raise usage("--reason goes with spec draft only")
    conn = tx.conn
    row = get(conn, raw_id)
    item_id = row["id"]
    if row["kind"] != "project":
        raise refused("not-project", f"{item_id} is a {KIND_OUT[row['kind']]}; only projects have a spec", item=item_id)
    if row["status"] == "closed":
        raise refused("closed", f"{item_id} is closed", item=item_id)
    if row["spec_state"] == state:
        return item_id
    data = {"old": row["spec_state"], "new": state}
    data.update(stages.check_accept(conn, row) if state == "accepted" else {"reason": reason})
    conn.execute("UPDATE items SET spec_state = ?, updated_at = ? WHERE id = ?", (state, clock.stamp(tx.now), item_id))
    tx.journal("spec.changed", feed_fields(conn, item_id), data=data)
    stages.follow_spec(tx, item_id, state, reason)
    return item_id


def page_unpublished(page):
    """A `pages` row (or None) whose page doesn't exist yet as far as the tracker knows: no row, no slug,
    or a publish that may not have finished (`publishing`)."""
    return page is None or page["slug"] is None or page["state"] == "publishing"


def page_dirty(page):
    """Whether a page waits to be sent: never published, or marked (`due_at` set)."""
    return page_unpublished(page) or page["due_at"] is not None


def page_json(conn, row):
    """An item's published page, `{url, version, published_at, dirty}`; None before its first publish
    and for asks (Page publisher, Amendments to Core)."""
    if row["form"] == "ask":
        return None
    page = conn.execute("SELECT * FROM pages WHERE item_id = ? AND slug IS NOT NULL", (row["id"],)).fetchone()
    if page is None:
        return None
    return {"url": page["url"], "version": page["version"], "published_at": page["published_at"],
            "dirty": page_dirty(page)}


def to_json(conn, row, section_bodies=True):
    """The JSON item object shared by show, tree, ready and claim."""
    from . import blast, criteria, ledger, stages, worktrees

    derived = hierarchy(conn, row["id"])
    readiness = ready.status(conn, row)
    repo_list = repos(conn, row["id"])
    claim = claims.live(conn, row["id"])
    item = {
        "id": row["id"],
        "kind": KIND_OUT[row["kind"]],
        "form": FORM_OUT.get(row["form"]),
        "subtype": row["subtype"],
        "title": row["title"],
        "parent": row["parent_id"],
        "root": derived["root"],
        "depth": derived["depth"],
        "rank": row["rank"],
        "urgent": bool(row["urgent"]),
        "owner": row["owner"],
        "effective_owner": derived["effective_owner"],
        "client": row["client"],
        "effective_client": derived["effective_client"],
        "status": row["status"],
        "resolution": RESOLUTION_OUT.get(row["resolution"]),
        "outcome": row["outcome"],
        "spec_state": row["spec_state"],
        "jira_url": row["jira_url"],
        "repos": repo_list,
        "worktree": worktrees.to_json(row),
        "archive": row["archive"],
        "effective_archive": effective_archive(conn, row["id"]),
        "stage": row["stage"],
        "merged_skipped": bool(row["merged_skipped"]),
        "lane": blast.lane(conn, row),
        "gate": stages.gate_json(conn, row),
        "sections": sections.listing(conn, row, bodies=section_bodies),
        "criteria": criteria.listing(conn, row["id"]),
        "ledger_rows": ledger.rows_json(conn, row["id"]),
        "edges": edges.listing(conn, row["id"]),
        "children": [
            {"id": c["id"], "kind": KIND_OUT[c["kind"]], "form": FORM_OUT.get(c["form"]),
             "title": c["title"], "status": c["status"]}
            for c in children(conn, row["id"])
        ],
        "open_asks": asks.open_under(conn, row["id"]),
        "blocked": readiness["blocked"],
        "in_progress": readiness["in_progress"],
        "claim": claims.to_json(claim) if claim is not None else None,
        "handoff": handoffs.summary(conn, row["id"]),
        "page": page_json(conn, row),
        "ready": readiness["ready"],
        "why_not": readiness["why_not"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
    }
    if row["form"] == "ask":
        item.update(asks.fields(conn, row))
    else:
        item["comments"] = comments.unacknowledged(conn, row["id"])
    return item
