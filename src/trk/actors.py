"""Actors: the registry of people and agent roles that write, own and (later) are asked."""

import re

from . import store
from .errors import not_found, refused, usage
from .store import ACTOR_RE

KINDS = ("human", "agent")


def _check_display(display):
    display = display.strip()
    if not display or "\n" in display:
        raise usage("a display name is one non-empty line")
    return display


def to_json(row):
    return {"id": row["id"], "kind": row["kind"], "display": row["display"]}


def get(conn, actor_id):
    row = conn.execute("SELECT * FROM actors WHERE id = ?", (actor_id,)).fetchone()
    if row is None:
        raise not_found(f"no actor {actor_id!r}", item=actor_id)
    return row


def kind(conn, actor_id):
    """human or agent; None for no actor."""
    return None if actor_id is None else get(conn, actor_id)["kind"]


def add(tx, actor_id, kind, display=None):
    """Register an actor; journals `actor.added` with a null item. Ids are never reused."""
    if not ACTOR_RE.match(actor_id):
        raise usage(f"actor id {actor_id!r} is not a lowercase slug")
    if kind not in KINDS:
        raise usage(f"actor kind is human or agent, not {kind!r}")
    display = _check_display(display) if display is not None else actor_id
    conn = tx.conn
    if conn.execute("SELECT 1 FROM actors WHERE id = ?", (actor_id,)).fetchone():
        raise refused("actor-exists", f"actor {actor_id!r} is already registered", item=actor_id)
    store.add_actor(tx, actor_id, kind, display)


def edit(tx, actor_id, display):
    """Change an actor's display name; journals `actor.updated` only if it changed."""
    display = _check_display(display)
    row = get(tx.conn, actor_id)
    if display == row["display"]:
        return
    tx.conn.execute("UPDATE actors SET display = ? WHERE id = ?", (display, actor_id))
    tx.journal("actor.updated", {}, data={**to_json(row), "display": display})


def link(tx, actor_id, service_user):
    """Link an actor to an artifact-service author id (`trk actor link`), moving the id off any actor that
    held it. Journals `actor.updated` with `fields: ["service_user"]` for each actor whose link changed;
    the id itself stays internal, as the actors JSON does. Linking the holder again writes nothing."""
    service_user = service_user.strip()
    if not service_user or any(ch.isspace() for ch in service_user):
        raise usage("--service-user takes the author id the artifact service shows, one word")
    conn = tx.conn
    row = get(conn, actor_id)
    if row["service_user_id"] == service_user:
        return
    holder = conn.execute("SELECT * FROM actors WHERE service_user_id = ?", (service_user,)).fetchone()
    if holder is not None:
        conn.execute("UPDATE actors SET service_user_id = NULL WHERE id = ?", (holder["id"],))
        tx.journal("actor.updated", {}, data={**to_json(holder), "fields": ["service_user"]})
    conn.execute("UPDATE actors SET service_user_id = ? WHERE id = ?", (service_user, row["id"]))
    tx.journal("actor.updated", {}, data={**to_json(row), "fields": ["service_user"]})


def display(conn, actor_id):
    """How a page reply names an actor: its display name, its id when it has none, "nobody" for None."""
    if actor_id is None:
        return "nobody"
    row = conn.execute("SELECT display FROM actors WHERE id = ?", (actor_id,)).fetchone()
    return (row["display"] if row else None) or actor_id


def by_service_user(conn, service_user):
    """The actor id linked to an author id, or None."""
    row = conn.execute("SELECT id FROM actors WHERE service_user_id = ?", (service_user,)).fetchone()
    return row["id"] if row else None


def _same_name(conn, display):
    """The human actors whose display name is `display`, whitespace and case aside."""
    name = " ".join(display.split()).casefold()
    return [r for r in conn.execute("SELECT * FROM actors WHERE kind = 'human' ORDER BY created_at, rowid")
            if " ".join((r["display"] or r["id"]).split()).casefold() == name]


def merge_note(conn, service_user, display):
    """What a reply tells an admin when an unlinked commenter shares a display name with a registered human
    but is not linked to them (several, or one that is linked already): the new human it registers is
    probably a duplicate. None when no one shares the name, or the commenter is linked or will be linked."""
    if by_service_user(conn, service_user) or not (display or "").strip():
        return None
    named = _same_name(conn, display)
    if not named or (len(named) == 1 and named[0]["service_user_id"] is None):
        return None
    return ("An admin should merge them if they are the same person: "
            f"`trk actor link <actor> --service-user {service_user}`.")


def register_commenter(tx, service_user, display):
    """The actor for a page commenter nobody linked yet. When their display name matches exactly one human
    with no link, that human is linked to their author id (journaled `actor.updated`, as `link`) and returned.
    Otherwise a new `human` is registered and linked, journaled `actor.added` by the transaction's actor (the
    publisher): the id is a slug of the display name, de-duplicated with -2, -3…; the display is the name as
    given. Returns the actor's id."""
    display = " ".join((display or "").split()) or service_user
    matches = [r for r in _same_name(tx.conn, display) if r["service_user_id"] is None]
    if len(matches) == 1 and len(_same_name(tx.conn, display)) == 1:
        link(tx, matches[0]["id"], service_user)
        return matches[0]["id"]
    base = re.sub(r"[^a-z0-9]+", "-", display.lower())[:40].strip("-") or "commenter"
    taken = {r["id"] for r in tx.conn.execute("SELECT id FROM actors")}
    actor_id, n = base, 1
    while actor_id in taken:
        n += 1
        actor_id = f"{base}-{n}"
    store.add_actor(tx, actor_id, "human", display)
    tx.conn.execute("UPDATE actors SET service_user_id = ? WHERE id = ?", (service_user, actor_id))
    return actor_id


def listing(conn):
    """Every actor in registration order."""
    return [to_json(row) for row in conn.execute("SELECT * FROM actors ORDER BY created_at, rowid")]
