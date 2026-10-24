"""`trk move`: reparent, lift to top level, reorder; the check order; one `item.moved` row."""

import json
import sqlite3

import pytest

from trk import ranks
from gated import new_flags, prove_close


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
    trk("actor", "add", "alice", "--kind", "human")
    trk("actor", "add", "bob", "--kind", "human")
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


def item(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


def titles(trk, parent):
    return [c["title"] for c in item(trk, parent)["children"]]


def move(trk, *argv):
    return trk("move", *argv, "--json")


@pytest.fixture
def forest(store):
    """P (owner alice) > J > T, P > D; Q (owner bob) > K."""
    p = new(store, "program", "P", "--owner", "alice")
    j = new(store, "project", "J", "--parent", p)
    t = new(store, "work-item", "T", "--form", "spec-bound", "--parent", j)
    d = new(store, "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", p)
    q = new(store, "program", "Q", "--owner", "bob")
    k = new(store, "project", "K", "--parent", q)
    return {"P": p, "J": j, "T": t, "D": d, "Q": q, "K": k}


def test_move_reparents_and_journals_one_moved_row(store, trk_home, forest):
    j, k, p, q = forest["J"], forest["K"], forest["P"], forest["Q"]
    old_rank = item(store, j)["rank"]
    before = len(journal(trk_home))
    result = move(store, j, "--parent", k, "--session", "s-1")
    assert result.code == 0, result.err
    moved = result.json()["item"]
    assert (moved["parent"], moved["root"], moved["depth"], moved["effective_owner"]) == (k, q, 2, "bob")
    rows = journal(trk_home)[before:]
    assert [r["change"] for r in rows] == ["item.moved"]
    row = rows[0]
    assert (row["item"], row["parent"], row["root"], row["depth"], row["owner"], row["session"]) == (j, k, q, 2, "bob", "s-1")
    assert row["data"] == {
        "old": {"parent": p, "root": p, "depth": 1, "rank": old_rank},
        "new": {"parent": k, "root": q, "depth": 2, "rank": moved["rank"]},
    }
    # Descendants come along without rows of their own.
    assert item(store, forest["T"])["root"] == q
    assert item(store, forest["T"])["depth"] == 3


def test_move_plain_prints_the_id(store, forest):
    result = store("move", forest["D"], "--first")
    assert (result.code, result.out.strip()) == (0, forest["D"])


def test_move_top_lifts_to_top_level(store, trk_home, forest):
    j, p = forest["J"], forest["P"]
    result = move(store, j, "--top", "--first")
    assert result.code == 0, result.err
    moved = result.json()["item"]
    assert (moved["parent"], moved["root"], moved["depth"], moved["effective_owner"]) == (None, j, 0, "alice")
    assert [i["title"] for i in store("tree", "--json").json()["items"] if i["depth"] == 0] == ["J", "P", "Q"]
    row = journal(trk_home)[-1]
    assert (row["parent"], row["root"], row["depth"], row["owner"]) == (None, j, 0, "alice")
    assert row["data"]["old"]["root"] == p
    assert row["data"]["new"] == {"parent": None, "root": j, "depth": 0, "rank": moved["rank"]}


def test_reorder_within_the_parent_is_a_move_and_rewrites_only_that_rank(store, trk_home, forest):
    p = forest["P"]
    e = new(store, "work-item", "E", "--form", "decision", "--subtype", "task", "--parent", p)
    ranks_before = {c: item(store, c)["rank"] for c in (forest["J"], forest["D"])}
    before = len(journal(trk_home))
    result = move(store, e, "--before", forest["D"])
    assert result.code == 0, result.err
    assert titles(store, p) == ["J", "E", "D"]
    assert {c: item(store, c)["rank"] for c in ranks_before} == ranks_before
    rows = journal(trk_home)[before:]
    assert [(r["change"], r["item"]) for r in rows] == [("item.moved", e)]
    assert rows[0]["data"]["old"]["parent"] == rows[0]["data"]["new"]["parent"] == p
    assert "rekey" not in rows[0]["data"]


@pytest.mark.parametrize("flags, expected", [
    (("--first",), ["J", "D"]),
    (("--last",), ["D", "J"]),
    (("--after", "D"), ["D", "J"]),
])
def test_reorder_flags(store, forest, flags, expected):
    flags = tuple(forest.get(f, f) for f in flags)
    assert move(store, forest["J"], *flags).code == 0
    assert titles(store, forest["P"]) == expected


def test_move_without_parent_flags_stays_under_its_parent(store, forest):
    result = move(store, forest["J"], "--last")
    assert result.json()["item"]["parent"] == forest["P"]
    assert titles(store, forest["P"]) == ["D", "J"]


def test_move_to_where_it_already_is_writes_nothing(store, trk_home, forest):
    before = journal(trk_home)
    rank = item(store, forest["D"])["rank"]
    for flags in (("--last",), (), ("--parent", forest["P"]), ("--after", forest["J"])):
        result = move(store, forest["D"], *flags)
        assert result.code == 0, result.err
    assert journal(trk_home) == before
    assert item(store, forest["D"])["rank"] == rank


def test_move_bumps_updated_at(store, monkeypatch, forest):
    monkeypatch.setenv("TRK_NOW", "2030-01-01T00:00:00Z")
    assert move(store, forest["D"], "--first").json()["item"]["updated_at"] == "2030-01-01T00:00:00.000Z"


def test_moving_a_program_under_its_own_sub_program_is_a_cycle(store, trk_home):
    p = new(store, "program", "P")
    sub = new(store, "program", "Sub", "--parent", p)
    subsub = new(store, "program", "SubSub", "--parent", sub)
    before = journal(trk_home)
    for target in (sub, subsub, p):
        result = move(store, p, "--parent", target)
        assert result.code == 1
        assert (result.error()["rule"], result.error()["item"]) == ("cycle", p)
    assert journal(trk_home) == before


def test_unknown_item_or_parent_exits_3(store, forest):
    assert move(store, "tr-zzzzz", "--top").code == 3
    result = move(store, forest["D"], "--parent", "tr-zzzzz")
    assert (result.code, result.error()["code"]) == (3, "not-found")


def test_nesting_is_checked_against_the_new_parent(store, forest):
    result = move(store, forest["J"], "--parent", forest["D"])
    assert result.code == 1
    assert (result.error()["rule"], result.error()["item"]) == ("nesting", forest["D"])


def test_open_item_under_a_closed_parent_is_refused(store, forest):
    closed = new(store, "work-item", "Done", "--form", "decision", "--subtype", "task", "--parent", forest["Q"])
    prove_close(store, closed)
    assert store("close", closed, "--resolution", "done", "--outcome", "Done.").code == 0
    result = move(store, forest["T"], "--parent", closed)
    assert result.code == 1
    assert (result.error()["rule"], result.error()["item"]) == ("parent-closed", closed)


def test_a_closed_item_may_move_under_a_closed_parent(store, forest):
    a = new(store, "work-item", "A", "--form", "decision", "--subtype", "task", "--parent", forest["Q"])
    b = new(store, "work-item", "B", "--form", "decision", "--subtype", "task", "--parent", forest["Q"])
    for x in (a, b):
        prove_close(store, x)
        assert store("close", x, "--resolution", "done", "--outcome", "Done.").code == 0
    assert move(store, b, "--parent", a).code == 0


def test_checks_run_in_the_spec_order(store):
    """Each target fails several checks; the first in spec order wins."""
    p = new(store, "program", "P")
    j = new(store, "project", "J", "--parent", p)
    w = new(store, "work-item", "W", "--form", "decision", "--subtype", "task", "--parent", j)
    # Cycle before nesting: W is under P, and a program under a work item breaks nesting too.
    assert move(store, p, "--parent", w).error()["rule"] == "cycle"
    # Nesting before parent-closed.
    prove_close(store, w)
    assert store("close", w, "--resolution", "done", "--outcome", "Done.").code == 0
    q = new(store, "program", "Q")
    assert move(store, q, "--parent", w).error()["rule"] == "nesting"
    # Unknown parent before anything else.
    assert move(store, p, "--parent", "tr-zzzzz").code == 3


def test_anchor_must_be_a_sibling_under_the_target(store, forest):
    result = move(store, forest["D"], "--parent", forest["Q"], "--before", forest["J"])
    assert (result.code, result.error()["rule"]) == (1, "not-sibling")
    result = move(store, forest["D"], "--before", forest["D"])
    assert (result.code, result.error()["rule"]) == (1, "not-sibling")


def test_flags_conflict(store, forest):
    assert move(store, forest["D"], "--top", "--parent", forest["Q"]).code == 2
    assert move(store, forest["D"], "--first", "--last").code == 2


@pytest.mark.parametrize("from_parent", ["Q", "P"])
def test_move_into_a_crowded_spot_rekeys_the_new_siblings(store, trk_home, from_parent):
    p = new(store, "program", "P")
    q = new(store, "program", "Q")
    lo = new(store, "project", "Lo", "--parent", p)
    hi = new(store, "project", "Hi", "--parent", p)
    while len(ranks.key_between(item(store, lo)["rank"], item(store, hi)["rank"])) <= ranks.MAX_LENGTH:
        lo = new(store, "project", "Mid", "--parent", p, "--after", lo)
    mover = new(store, "project", "Mover", "--parent", {"P": p, "Q": q}[from_parent])
    before = len(journal(trk_home))
    result = move(store, mover, "--parent", p, "--after", lo)
    assert result.code == 0, result.err
    rows = journal(trk_home)[before:]
    assert all(r["data"].get("rekey") for r in rows[:-1]) and rows[:-1]
    assert (rows[-1]["item"], "rekey" in rows[-1]["data"]) == (mover, False)
    children = item(store, p)["children"]
    assert children[[c["id"] for c in children].index(lo) + 1]["id"] == mover


def test_changes_root_returns_the_move_under_old_and_new_root(store, forest):
    j, p, q = forest["J"], forest["P"], forest["Q"]
    assert move(store, j, "--parent", q).code == 0
    for root in (p, q):
        rows = store("changes", "--since", "0", "--root", root, "--json").json()["changes"]
        assert [r["item"] for r in rows if r["change"] == "item.moved"] == [j]
