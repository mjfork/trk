"""Function roles: the table asks are addressed through, each role held by one registered actor.

`parent` is the relative role, resolved against the asked item's hierarchy; it is reserved and
never stored here.
"""

from . import clock, store  # store imports roles back; both bind modules only
from .errors import refused, usage

PARENT = "parent"
RESERVED = (PARENT,)
SEEDED = ("product", "engineering")


def check_name(role):
    """A role name is a lowercase slug (usage error) and not a reserved one (rule `reserved-role`)."""
    if not store.ACTOR_RE.match(role or ""):  # role names are slugs spelled like actor ids
        raise usage(f"role name {role!r} is not a lowercase slug")
    if role in RESERVED:
        raise refused("reserved-role", f"{role!r} is a relative role; it resolves per ask and cannot be set")
    return role


def holder(conn, role):
    """The actor holding a role, or None for a role that does not exist."""
    row = conn.execute("SELECT actor FROM roles WHERE role = ?", (role,)).fetchone()
    return row["actor"] if row else None


def set_role(tx, role, actor):
    """Create or reassign a role; journals `role.set` (item null; data: role, old, new) unless unchanged,
    then `ask.readdressed` (cause `role-set`) for each open ask it reroutes."""
    from . import asks  # asks imports items, which needs store fully loaded; store imports roles

    check_name(role)
    store.require_actor(tx.conn, actor)
    old = holder(tx.conn, role)
    if old == actor:
        return
    routed = asks.snapshot(tx.conn)
    tx.conn.execute(
        "INSERT INTO roles (role, actor, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT (role) DO UPDATE SET actor = excluded.actor, updated_at = excluded.updated_at",
        (role, actor, clock.stamp(tx.now)),
    )
    tx.journal("role.set", {}, data={"role": role, "old": old, "new": actor})
    asks.reroute(tx, routed, "role-set")


def seed(tx, me):
    """init and the schema 2 migration: `product` and `engineering`, both held by `me`."""
    for role in SEEDED:
        set_role(tx, role, me)


def to_json(row):
    """A role row joined with its holder, as `roles --json` prints it."""
    return {"role": row["role"], "actor": row["actor"], "actor_kind": row["kind"], "display": row["display"]}


_JOINED = "SELECT roles.role, roles.actor, actors.kind, actors.display FROM roles JOIN actors ON actors.id = roles.actor"


def listing(conn):
    """Every function role, sorted by name, with its holder's kind and display name."""
    return [to_json(row) for row in conn.execute(_JOINED + " ORDER BY roles.role")]


def entry(conn, role):
    """One role as `listing` shows it."""
    return to_json(conn.execute(_JOINED + " WHERE roles.role = ?", (role,)).fetchone())
