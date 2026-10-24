"""Owners: `--owner` on new, `--owner/--no-owner` on edit, effective owner on rows, show and tree;
the default owner every top-level item gets (spec 02, Owners)."""

import json
import sqlite3

import pytest
from gated import DEST, new_flags


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
    trk("actor", "add", "builder", "--kind", "agent")
    trk("actor", "add", "ana", "--kind", "human")
    return trk


def new(trk, *argv):
    result = trk("new", *argv, *new_flags(argv), "--json")
    assert result.code == 0, result.err
    return result.json()["item"]["id"]


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


def created(trk_home, item_id):
    """The item's `item.created` row."""
    return next(r for r in journal(trk_home) if r["change"] == "item.created" and r["item"] == item_id)


def item(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


@pytest.fixture
def chain(store):
    """P (owned by ana) > J > T, and program Q, owned by the default owner michael."""
    p = new(store, "program", "Program P", "--owner", "ana")
    j = new(store, "project", "Project J", "--parent", p)
    t = new(store, "work-item", "Ticket T", "--form", "spec-bound", "--parent", j)
    q = new(store, "program", "Program Q")
    return {"P": p, "J": j, "T": t, "Q": q}


def test_new_with_owner_sets_it_and_the_created_row_carries_it(store, trk_home):
    p = new(store, "program", "P", "--owner", "builder")
    assert (item(store, p)["owner"], item(store, p)["effective_owner"]) == ("builder", "builder")
    row = created(trk_home, p)
    assert (row["change"], row["owner"], row["data"]["owner"]) == ("item.created", "builder", "builder")


def test_children_inherit_the_nearest_ancestors_owner(store, trk_home, chain):
    t = item(store, chain["T"])
    assert (t["owner"], t["effective_owner"]) == (None, "ana")
    row = created(trk_home, chain["T"])
    assert (row["item"], row["owner"], row["data"]["owner"]) == (chain["T"], "ana", None)
    assert item(store, chain["Q"])["owner"] == "michael"


def test_new_with_an_unregistered_owner_is_refused(store, trk_home):
    before = len(journal(trk_home))
    result = store("new", "program", "P", "--owner", "mallory", "--json", *DEST)
    assert result.code == 1
    assert (result.error()["rule"], result.error()["item"]) == ("unknown-actor", "mallory")
    assert len(journal(trk_home)) == before


def test_edit_owner_journals_owner_changed_only_for_the_item(store, trk_home, chain):
    before = len(journal(trk_home))
    assert store("edit", chain["J"], "--owner", "builder", "--session", "s-1").code == 0
    rows = journal(trk_home)[before:]
    assert [r["change"] for r in rows] == ["owner.changed"]
    row = rows[0]
    assert (row["item"], row["owner"], row["session"], row["root"]) == (chain["J"], "builder", "s-1", chain["P"])
    assert row["data"] == {"old": None, "new": "builder"}
    assert item(store, chain["T"])["effective_owner"] == "builder"
    assert item(store, chain["P"])["effective_owner"] == "ana"


def test_no_owner_clears_so_the_item_inherits_again(store, trk_home, chain):
    store("edit", chain["J"], "--owner", "builder")
    assert store("edit", chain["J"], "--no-owner").code == 0
    row = journal(trk_home)[-1]
    assert (row["change"], row["owner"], row["data"]) == ("owner.changed", "ana", {"old": "builder", "new": None})
    j = item(store, chain["J"])
    assert (j["owner"], j["effective_owner"]) == (None, "ana")
    assert item(store, chain["T"])["effective_owner"] == "ana"


def test_owner_edit_that_changes_nothing_writes_no_row(store, trk_home, chain):
    before = len(journal(trk_home))
    assert store("edit", chain["P"], "--owner", "ana").code == 0
    assert store("edit", chain["J"], "--no-owner").code == 0
    assert len(journal(trk_home)) == before


def test_rows_after_an_owner_change_carry_the_new_effective_owner(store, trk_home, chain):
    store("edit", chain["P"], "--owner", "builder")
    store("edit", chain["T"], "--urgent")
    assert journal(trk_home)[-1]["owner"] == "builder"


def test_edit_with_owner_and_scalars_writes_both_rows_with_the_new_owner(store, trk_home, chain):
    before = len(journal(trk_home))
    store("edit", chain["T"], "--title", "Ticket T2", "--owner", "builder")
    rows = journal(trk_home)[before:]
    assert [r["change"] for r in rows] == ["item.updated", "owner.changed"]
    assert [r["owner"] for r in rows] == ["builder", "builder"]
    assert rows[0]["data"]["fields"] == ["title"]
    assert rows[1]["data"] == {"old": None, "new": "builder"}


def test_edit_owner_bumps_updated_at(store, monkeypatch, chain):
    monkeypatch.setenv("TRK_NOW", "2030-01-01T00:00:00Z")
    store("edit", chain["T"], "--owner", "builder")
    assert item(store, chain["T"])["updated_at"] == "2030-01-01T00:00:00.000Z"


def test_edit_with_an_unregistered_owner_is_refused(store, trk_home, chain):
    before = len(journal(trk_home))
    result = store("edit", chain["T"], "--owner", "mallory", "--title", "X", "--json")
    assert result.code == 1 and result.error()["rule"] == "unknown-actor"
    assert len(journal(trk_home)) == before
    assert item(store, chain["T"])["title"] == "Ticket T"


def test_owner_and_no_owner_together_is_a_usage_error(store, chain):
    result = store("edit", chain["T"], "--owner", "ana", "--no-owner", "--json")
    assert result.code == 2


def test_no_owner_alone_is_something_to_edit(store, chain):
    assert store("edit", chain["T"], "--no-owner").code == 0


def test_show_marks_an_inherited_owner(store, chain):
    assert "  owner: ana\n" in store("show", chain["P"]).out + "\n"
    assert "  owner: ana (inherited)" in store("show", chain["T"]).out
    assert "  owner: michael\n" in store("show", chain["Q"]).out


def test_tree_lines_show_the_effective_owner(store, chain):
    lines = store("tree").out.splitlines()
    assert lines == [
        f"{chain['P']}  program  Program P  @ana",
        f"  {chain['J']}  project  Project J  [ready]  @ana",
        f"    {chain['T']}  work-item/spec-bound [framed]  Ticket T  @ana",
        f"{chain['Q']}  program  Program Q  @michael",
    ]


# --- the default owner (spec 02, Owners) --------------------------------------


def test_a_top_level_item_without_owner_gets_the_default_owner_on_its_created_row(store, trk_home):
    p = new(store, "work-item", "Loose", "--form", "decision", "--subtype", "task", "--as", "builder")
    assert item(store, p)["owner"] == "michael"
    row = journal(trk_home)[-1]
    assert (row["change"], row["actor"], row["owner"], row["data"]["owner"]) == ("item.created", "builder", "michael", "michael")


def test_a_child_without_owner_still_inherits(store, chain):
    child = new(store, "project", "Child", "--parent", chain["Q"])
    assert (item(store, child)["owner"], item(store, child)["effective_owner"]) == (None, "michael")


def test_an_explicit_owner_beats_the_default(store):
    assert item(store, new(store, "program", "P", "--owner", "ana"))["owner"] == "ana"


def test_new_top_level_items_follow_a_changed_default_owner(store):
    store("config", "set", "default-owner", "ana")
    assert item(store, new(store, "program", "P"))["owner"] == "ana"


def test_no_owner_on_a_top_level_item_is_refused(store, trk_home, chain):
    before = len(journal(trk_home))
    result = store("edit", chain["P"], "--no-owner", "--title", "Renamed", "--json")
    assert (result.code, result.error()["rule"], result.error()["item"]) == (1, "owner-required", chain["P"])
    assert len(journal(trk_home)) == before
    assert item(store, chain["P"])["title"] == "Program P"


def test_move_top_of_an_inheriting_item_stores_its_prior_effective_owner_first(store, trk_home, chain):
    before = len(journal(trk_home))
    assert store("move", chain["J"], "--top", "--session", "s-1").code == 0
    rows = journal(trk_home)[before:]
    assert [r["change"] for r in rows] == ["owner.changed", "item.moved"]
    owner_row = rows[0]
    assert owner_row["data"] == {"old": None, "new": "ana", "reason": "moved-top"}
    assert (owner_row["parent"], owner_row["root"], owner_row["owner"], owner_row["session"]) == (chain["P"], chain["P"], "ana", "s-1")
    assert (rows[1]["parent"], rows[1]["owner"]) == (None, "ana")
    assert item(store, chain["J"])["owner"] == "ana"


def test_move_top_of_an_item_with_its_own_owner_writes_no_owner_row(store, trk_home, chain):
    store("edit", chain["J"], "--owner", "builder")
    before = len(journal(trk_home))
    assert store("move", chain["J"], "--top").code == 0
    assert [r["change"] for r in journal(trk_home)[before:]] == ["item.moved"]
    assert item(store, chain["J"])["owner"] == "builder"


def test_a_refused_move_top_writes_no_owner_row(store, trk_home, chain):
    before = len(journal(trk_home))
    result = store("move", chain["J"], "--top", "--before", chain["T"], "--json")
    assert result.code == 1 and result.error()["rule"] == "not-sibling"
    assert len(journal(trk_home)) == before
    assert item(store, chain["J"])["owner"] is None
