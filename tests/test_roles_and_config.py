"""Function roles and store settings (spec 02: Roles, Owners' default owner), with AC 1 and AC 2 as written."""

import json
import sqlite3

import pytest
from gated import DEST


@pytest.fixture
def store(trk):
    """The spec's acceptance store: init --me michael, then kim (human) and worker (agent)."""
    trk("init", "--me", "michael", "--display", "Michael Fork")
    trk("actor", "add", "kim", "--kind", "human", "--display", "Kim")
    trk("actor", "add", "worker", "--kind", "agent")
    return trk


def journal(trk_home):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM journal ORDER BY seq")]
    finally:
        conn.close()
    for row in rows:
        row["data"] = json.loads(row["data"])
    return rows


def roles(trk):
    result = trk("roles", "--json")
    assert result.code == 0, result.err
    return result.json()


# --- AC 1 and AC 2, as written ---------------------------------------------------


def test_ac1_roles_seeded_set_and_parent_reserved(store, trk_home):
    assert roles(store) == {"schema": 1, "roles": [
        {"role": "engineering", "actor": "michael", "actor_kind": "human", "display": "Michael Fork"},
        {"role": "product", "actor": "michael", "actor_kind": "human", "display": "Michael Fork"},
        {"role": "publisher", "actor": "michael", "actor_kind": "human", "display": "Michael Fork"},
    ]}
    assert store("roles", "set", "product", "kim").code == 0
    row = journal(trk_home)[-1]
    assert (row["change"], row["item"], row["actor"], row["data"]) == (
        "role.set", None, "michael", {"role": "product", "old": "michael", "new": "kim"},
    )
    result = store("roles", "set", "parent", "kim", "--json")
    assert (result.code, result.error()["rule"]) == (1, "reserved-role")


def test_ac2_default_owner_owns_top_level_items(store):
    p = store("new", "program", "P", "--json", *DEST).json()["item"]
    assert p["owner"] == "michael"
    result = store("edit", p["id"], "--no-owner", "--json")
    assert (result.code, result.error()["rule"]) == (1, "owner-required")
    assert store("config", "set", "default-owner", "kim").code == 0
    assert store("new", "program", "Q", "--json", *DEST).json()["item"]["owner"] == "kim"


# --- init ------------------------------------------------------------------------


def test_init_seeds_the_roles_and_default_owner_as_me(trk, trk_home):
    trk("init", "--me", "ana")
    assert [(r["role"], r["actor"]) for r in roles(trk)["roles"]] == [("engineering", "ana"), ("product", "ana"),
                                                                   ("publisher", "ana")]
    assert trk("config", "--json").json()["config"]["default-owner"] == "ana"
    assert {r["actor"] for r in journal(trk_home)} == {"ana"}


# --- roles -----------------------------------------------------------------------


def test_roles_set_creates_a_new_role(store, trk_home):
    result = store("roles", "set", "finance", "worker", "--as", "kim", "--session", "s-1", "--json")
    assert result.code == 0, result.err
    assert result.json() == {"schema": 1, "role": {"role": "finance", "actor": "worker", "actor_kind": "agent",
                                                   "display": "worker"}}
    row = journal(trk_home)[-1]
    assert (row["change"], row["actor"], row["session"], row["data"]) == (
        "role.set", "kim", "s-1", {"role": "finance", "old": None, "new": "worker"},
    )
    assert [r["role"] for r in roles(store)["roles"]] == ["engineering", "finance", "product", "publisher"]


def test_roles_set_to_the_current_holder_writes_nothing(store, trk_home):
    before = len(journal(trk_home))
    assert store("roles", "set", "product", "michael").code == 0
    assert len(journal(trk_home)) == before


def test_roles_set_refuses_an_unregistered_actor(store, trk_home):
    before = len(journal(trk_home))
    result = store("roles", "set", "product", "mallory", "--json")
    assert (result.code, result.error()["rule"], result.error()["item"]) == (1, "unknown-actor", "mallory")
    assert len(journal(trk_home)) == before


@pytest.mark.parametrize("role", ["Product", "pro duct", "-x", "pröduct", "a_b", ""])
def test_a_role_name_that_is_not_a_lowercase_slug_is_a_usage_error(store, trk_home, role):
    before = len(journal(trk_home))
    result = store("roles", "set", role, "kim", "--json")
    assert (result.code, result.error()["code"]) == (2, "usage")
    assert len(journal(trk_home)) == before


def test_roles_set_needs_a_role_and_an_actor(store):
    assert store("roles", "set", "product").code == 2


def test_roles_text_lists_each_role_then_the_parent_note(store):
    store("roles", "set", "product", "kim")
    lines = store("roles").out.splitlines()
    assert lines[:3] == ["engineering → michael (Michael Fork)", "product → kim (Kim)",
                         "publisher → michael (Michael Fork)"]
    assert lines[3].startswith("parent is relative")
    assert len(lines) == 4


def test_roles_set_prints_the_role_line(store):
    assert store("roles", "set", "product", "kim").out == "product → kim (Kim)\n"


# --- config ----------------------------------------------------------------------


def test_config_lists_the_settings(store):
    assert store("config", "--json").json() == {"schema": 1, "config": {
        "default-actor": "michael", "default-owner": "michael", "id-prefix": "tr", "lease-minutes": "60",
        "release-verify-job": None, "shared-agent": "worker", "template-parent": None,
    }}
    assert "default-owner  michael" in store("config").out.splitlines()


def test_config_set_default_owner_journals_config_changed(store, trk_home):
    result = store("config", "set", "default-owner", "kim", "--as", "kim", "--json")
    assert result.code == 0, result.err
    assert result.json()["config"]["default-owner"] == "kim"
    row = journal(trk_home)[-1]
    assert (row["change"], row["item"], row["actor"], row["owner"], row["data"]) == (
        "config.changed", None, "kim", None, {"key": "default-owner", "old": "michael", "new": "kim"},
    )


def test_config_set_to_the_current_value_writes_nothing(store, trk_home):
    before = len(journal(trk_home))
    assert store("config", "set", "default-owner", "michael").code == 0
    assert len(journal(trk_home)) == before


@pytest.mark.parametrize("key", ["lease-minutes", "default-actor", "id-prefix", "default_owner", "nope"])
def test_config_set_of_any_other_key_is_a_usage_error(store, trk_home, key):
    before = len(journal(trk_home))
    result = store("config", "set", key, "kim", "--json")
    assert (result.code, result.error()["code"]) == (2, "usage")
    assert len(journal(trk_home)) == before


def test_config_set_refuses_an_unregistered_actor(store, trk_home):
    before = len(journal(trk_home))
    result = store("config", "set", "default-owner", "mallory", "--json")
    assert (result.code, result.error()["rule"]) == (1, "unknown-actor")
    assert len(journal(trk_home)) == before


# --- the feed --------------------------------------------------------------------


def test_role_and_config_rows_never_come_back_under_root(store, trk_home):
    p = store("new", "program", "P", *DEST).out.strip()
    store("roles", "set", "product", "kim")
    store("config", "set", "default-owner", "kim")
    rows = store("changes", "--since", "0", "--root", p, "--json").json()["changes"]
    assert [r["change"] for r in rows] == ["item.created", "criterion.added"]
    everything = store("changes", "--since", "0", "--json").json()["changes"]
    assert {"role.set", "config.changed"} <= {r["change"] for r in everything}
    assert all(r["addressee"] is None for r in everything)


def test_plain_changes_name_the_role_or_setting(store):
    store("roles", "set", "product", "kim")
    store("config", "set", "default-owner", "kim")
    lines = store("changes", "--since", "0").out.splitlines()
    assert lines[-2].split()[2:4] == ["role.set", "product"]
    assert lines[-1].split()[2:4] == ["config.changed", "default-owner"]
