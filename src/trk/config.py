"""Store settings: the default owner, the shared agent and the template parent are the ones a writer can change."""

from . import store  # store imports config back; both bind modules only
from .errors import usage

# CLI/feed spelling -> meta key. Read-only settings are listed by `trk config` too.
SETTABLE = {"default-owner": "default_owner", "shared-agent": "shared_agent", "template-parent": "template_parent"}
LISTED = {"default-actor": "default_actor", "default-owner": "default_owner", "id-prefix": "id_prefix",
          "lease-minutes": "lease_minutes", "shared-agent": "shared_agent", "template-parent": "template_parent"}

# Settings with a value before anyone sets them: the shared agent, for whom only the ledger's session
# check applies (Stage gates, Builder and independence).
DEFAULTS = {"shared_agent": "worker"}


def _value(st, key):
    value = st.meta(key)
    return DEFAULTS.get(key) if value is None else value


def listing(st):
    """Every listed setting by its CLI name, in name order."""
    return {name: _value(st, key) for name, key in sorted(LISTED.items())}


def set_value(tx, name, value):
    """Change a setting; journals `config.changed` (item null; data: key, old, new) unless unchanged.

    `default-owner` takes a registered actor and `shared-agent` a comma-separated list of them (rule
    `unknown-actor`); the list is stored without spaces or repeats. `template-parent`
    takes an item a ticket can be filed under: known (exit 3), open (`parent-closed`), not an ask (`ask-leaf`).
    """
    if name not in SETTABLE:
        raise usage(f"{name!r} is not a setting you can change; settable: {', '.join(SETTABLE)}")
    if name == "template-parent":
        value = check_template_parent(tx.conn, value)
    elif name == "shared-agent":
        value = check_shared_agents(tx.conn, value)
    else:
        store.require_actor(tx.conn, value)
    key = SETTABLE[name]
    old = _value(tx.store, key)
    if old == value:
        return
    tx.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, value))
    tx.journal("config.changed", {}, data={"key": name, "old": old, "new": value})


def check_shared_agents(conn, raw):
    """The shared agents as a canonical comma-separated list: each registered, none repeated."""
    names = list(dict.fromkeys(part.strip() for part in raw.split(",")))
    for name in names:
        store.require_actor(conn, name)
    return ",".join(names)


def check_template_parent(conn, raw_id):
    """The id of an item that can take a filed ticket: the template parent's checks."""
    from . import items

    parent = items.get(conn, raw_id)
    items.check_parent_open(parent)
    items.check_not_ask(parent)
    return parent["id"]


def seed(tx, me):
    """init and the schema 2 migration: the default owner starts as `me` (old null)."""
    set_value(tx, "default-owner", me)
