"""`trk close` and `trk reopen`: resolutions, outcomes, bottom-up closing, and `tree --all`."""

import json
import sqlite3

import pytest

from gated import accept, new_flags, prove_close, walk_to_verified


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
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


@pytest.fixture
def forest(store):
    p = new(store, "program", "Program P")
    j = new(store, "project", "Project J", "--parent", p)
    t = new(store, "work-item", "Ticket T", "--form", "spec-bound", "--parent", j)
    d = new(store, "work-item", "Decision D", "--form", "decision", "--subtype", "research", "--parent", p)
    return {"P": p, "J": j, "T": t, "D": d}


def close(trk, item_id, resolution="done", outcome="It shipped."):
    if resolution == "done":
        prove_close(trk, item_id)
    return trk("close", item_id, "--resolution", resolution, "--outcome", outcome, "--json")


def finish(trk, forest, outcome="It shipped."):
    """Close ticket T `done`, which only reaching Verified does: J's spec is accepted, then T walks there."""
    def ok(*argv):
        result = trk(*argv)
        assert result.code == 0, (argv, result.err)
        return result.out

    accept(trk, forest["J"])
    walk_to_verified(ok, forest["T"], outcome)


def test_close_sets_status_resolution_outcome_and_closed_at(store, trk_home, monkeypatch, forest):
    monkeypatch.setenv("TRK_NOW", "2026-10-10T12:00:00Z")
    d = forest["D"]
    result = store("close", d, "--resolution", "wont-do", "--outcome", "Not worth it.", "--session", "s-1")
    assert result.code == 0, result.err
    assert result.out.strip() == d
    item = store("show", d, "--json").json()["item"]
    assert (item["status"], item["resolution"], item["outcome"]) == ("closed", "wont-do", "Not worth it.")
    assert item["closed_at"] == item["updated_at"] == "2026-10-10T12:00:00.000Z"
    row = journal(trk_home)[-1]
    assert (row["change"], row["item"], row["session"]) == ("item.closed", d, "s-1")
    assert (row["status_old"], row["status_new"]) == ("open", "closed")
    assert row["data"] == {"resolution": "wont-do", "outcome": "Not worth it."}


@pytest.mark.parametrize("resolution", ["wont-do", "out-of-scope"])
def test_close_takes_each_resolution_without_edges(store, forest, resolution):
    result = close(store, forest["T"], resolution)
    assert result.code == 0, result.err
    assert result.json()["item"]["resolution"] == resolution


def test_a_ticket_closes_done_by_reaching_verified(store, forest):
    finish(store, forest)
    item = store("show", forest["T"], "--json").json()["item"]
    assert (item["status"], item["resolution"], item["stage"]) == ("closed", "done", "verified")


def test_close_without_outcome_is_a_usage_error(store, forest):
    prove_close(store, forest["D"])
    result = store("close", forest["D"], "--resolution", "done", "--json")
    assert result.code == 2
    assert result.error()["code"] == "usage"
    assert store("show", forest["D"], "--json").json()["item"]["status"] == "open"


@pytest.mark.parametrize("outcome", ["x" * 301, "two\nlines", "   "])
def test_close_refuses_a_bad_outcome(store, forest, outcome):
    assert close(store, forest["D"], outcome=outcome).code == 2


def test_close_accepts_an_outcome_of_exactly_300_chars(store, forest):
    assert close(store, forest["D"], outcome="x" * 300).code == 0


def test_close_with_an_unknown_resolution_is_a_usage_error(store, forest):
    assert close(store, forest["D"], resolution="finished").code == 2


@pytest.mark.parametrize("resolution, flag", [("superseded", "--by"), ("duplicate", "--of")])
def test_superseded_and_duplicate_need_their_edge_flag(store, forest, resolution, flag):
    result = close(store, forest["D"], resolution=resolution)
    assert result.code == 2
    assert flag in result.error()["message"]


def test_close_unknown_item_exits_3(store):
    assert close(store, "tr-zzzzz").code == 3


def test_close_with_open_children_is_refused_and_lists_them(store, forest):
    result = close(store, forest["P"])
    assert result.code == 1
    error = result.error()
    assert (error["code"], error["rule"], error["item"]) == ("refused", "open-children", forest["P"])
    assert forest["J"] in error["message"] and forest["D"] in error["message"]
    assert forest["T"] not in error["message"]  # grandchildren are not P's children
    assert store("show", forest["P"], "--json").json()["item"]["status"] == "open"


def test_close_bottom_up_then_the_parent_closes(store, forest):
    finish(store, forest)
    for key in ("J", "D", "P"):
        assert close(store, forest[key]).code == 0, key


def test_close_an_already_closed_item_is_refused(store, trk_home, forest):
    close(store, forest["D"])
    rows = len(journal(trk_home))
    result = close(store, forest["D"])
    assert (result.code, result.error()["rule"]) == (1, "already-closed")
    assert len(journal(trk_home)) == rows


def test_new_under_a_closed_parent_is_refused(store, forest):
    close(store, forest["D"])
    result = store("new", "work-item", "Late", "--form", "decision", "--subtype", "task",
                   "--parent", forest["D"], "--json")
    assert result.code == 1
    error = result.error()
    assert (error["rule"], error["item"]) == ("parent-closed", forest["D"])


def test_nesting_is_checked_before_parent_closed(store, forest):
    close(store, forest["D"])
    result = store("new", "project", "X", "--parent", forest["D"], "--json")
    assert result.error()["rule"] == "nesting"


def test_reopen_clears_resolution_outcome_and_closed_at(store, trk_home, monkeypatch, forest):
    d = forest["D"]
    close(store, d, resolution="out-of-scope", outcome="Later.")
    monkeypatch.setenv("TRK_NOW", "2026-10-11T08:00:00Z")
    result = store("reopen", d)
    assert result.code == 0, result.err
    assert result.out.strip() == d
    item = store("show", d, "--json").json()["item"]
    assert (item["status"], item["resolution"], item["outcome"], item["closed_at"]) == ("open", None, None, None)
    assert item["updated_at"] == "2026-10-11T08:00:00.000Z"
    row = journal(trk_home)[-1]
    assert (row["change"], row["item"]) == ("item.reopened", d)
    assert (row["status_old"], row["status_new"]) == ("closed", "open")
    assert row["data"] == {"resolution": "out-of-scope"}


def test_reopen_refuses_while_the_parent_is_closed(store, forest):
    close(store, forest["T"], "wont-do")
    close(store, forest["J"])
    result = store("reopen", forest["T"], "--json")
    assert result.code == 1
    assert (result.error()["rule"], result.error()["item"]) == ("parent-closed", forest["J"])
    assert store("reopen", forest["J"]).code == 0
    assert store("reopen", forest["T"]).code == 0


def test_reopen_an_open_item_is_refused(store, forest):
    result = store("reopen", forest["D"], "--json")
    assert (result.code, result.error()["rule"]) == (1, "not-closed")


def test_reopen_unknown_item_exits_3(store):
    assert store("reopen", "tr-zzzzz").code == 3


def test_tree_hides_closed_items_by_default_and_shows_them_under_all(store, forest):
    finish(store, forest, outcome="Shipped the thing.")
    close(store, forest["D"], resolution="wont-do", outcome="Answered elsewhere.")
    default = store("tree").out.splitlines()
    assert [line.split()[0] for line in default] == [forest["P"], forest["J"]]

    lines = store("tree", "--all").out.splitlines()
    assert [line.split()[0] for line in lines] == [forest[k] for k in ("P", "J", "T", "D")]
    assert lines[1] == f"  {forest['J']}  project  Project J  @michael"  # accepted, so off the frontier
    assert lines[2] == f"    {forest['T']}  work-item/spec-bound [verified]  Ticket T  @michael  [done] Shipped the thing."
    assert lines[3] == (f"  {forest['D']}  work-item/decision (research)  Decision D  @michael"
                        "  [wont-do] Answered elsewhere.")


def test_tree_all_json_includes_closed_items(store, forest):
    finish(store, forest)
    ids = [i["id"] for i in store("tree", forest["J"], "--all", "--json").json()["items"]]
    assert ids == [forest["J"], forest["T"]]
    ids = [i["id"] for i in store("tree", forest["J"], "--json").json()["items"]]
    assert ids == [forest["J"]]
