"""`trk tree` outlines open items in rank order; `trk edit` changes scalar fields and journals them."""

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


def close(trk, item_id):
    prove_close(trk, item_id)
    assert trk("close", item_id, "--resolution", "done", "--outcome", "Done.").code == 0


@pytest.fixture
def forest(store):
    p = new(store, "program", "Program P")
    j = new(store, "project", "Project J", "--parent", p)
    t = new(store, "work-item", "Ticket T", "--form", "spec-bound", "--parent", j)
    d = new(store, "work-item", "Decision D", "--form", "decision", "--subtype", "research", "--parent", p, "--first")
    q = new(store, "program", "Program Q")
    return {"P": p, "J": j, "T": t, "D": d, "Q": q}


def test_tree_outlines_the_forest_in_rank_order(store, forest):
    out = store("tree")
    assert out.code == 0
    lines = out.out.splitlines()
    assert [line.split()[0] for line in lines] == [forest[k] for k in ("P", "D", "J", "T", "Q")]
    assert lines[0] == f"{forest['P']}  program  Program P  @michael"
    assert lines[1] == f"  {forest['D']}  work-item/decision (research)  Decision D  [ready]  @michael"
    assert lines[3] == f"    {forest['T']}  work-item/spec-bound [framed]  Ticket T  @michael"


def test_tree_of_one_item_starts_at_it(store, forest):
    lines = store("tree", forest["J"]).out.splitlines()
    assert lines == [f"{forest['J']}  project  Project J  [ready]  @michael",
                     f"  {forest['T']}  work-item/spec-bound [framed]  Ticket T  @michael"]


def test_tree_json_uses_the_shared_item_shape_in_outline_order(store, forest):
    tree = store("tree", forest["P"], "--json").json()
    assert tree["schema"] == 1
    assert [i["id"] for i in tree["items"]] == [forest[k] for k in ("P", "D", "J", "T")]
    for item in tree["items"]:
        assert item == store("show", item["id"], "--json").json()["item"]


def test_tree_hides_closed_items(store, forest):
    def ok(*argv):
        result = store(*argv)
        assert result.code == 0, (argv, result.err)
        return result.out

    accept(store, forest["J"])
    walk_to_verified(ok, forest["T"])
    close(store, forest["D"])
    ids = [i["id"] for i in store("tree", "--json").json()["items"]]
    assert ids == [forest[k] for k in ("P", "J", "Q")]


def test_tree_unknown_id_exits_3(store):
    assert store("tree", "tr-zzzzz", "--json").code == 3


def test_edit_changes_scalars_and_journals_old_and_new(store, trk_home, forest):
    t = forest["T"]
    result = store("edit", t, "--title", "Ticket T2", "--urgent", "--jira", "https://j/PROJ-2",
                   "--add-repo", "/r/a", "--add-repo", "/r/b", "--session", "s-9")
    assert result.code == 0 and result.out.strip() == t
    item = store("show", t, "--json").json()["item"]
    assert (item["title"], item["urgent"], item["jira_url"], item["repos"]) == ("Ticket T2", True, "https://j/PROJ-2", ["/r/a", "/r/b"])
    row = journal(trk_home)[-1]
    assert (row["change"], row["item"], row["title"], row["session"]) == ("item.updated", t, "Ticket T2", "s-9")
    assert (row["status_old"], row["status_new"]) == (None, None)
    assert row["data"] == {
        "fields": ["title", "urgent", "jira_url", "repos"],
        "old": {"title": "Ticket T", "urgent": False, "jira_url": None, "repos": []},
        "new": {"title": "Ticket T2", "urgent": True, "jira_url": "https://j/PROJ-2", "repos": ["/r/a", "/r/b"]},
    }

    store("edit", t, "--no-urgent", "--no-jira", "--remove-repo", "/r/a")
    item = store("show", t, "--json").json()["item"]
    assert (item["urgent"], item["jira_url"], item["repos"]) == (False, None, ["/r/b"])
    assert journal(trk_home)[-1]["data"] == {
        "fields": ["urgent", "jira_url", "repos"],
        "old": {"urgent": True, "jira_url": "https://j/PROJ-2", "repos": ["/r/a", "/r/b"]},
        "new": {"urgent": False, "jira_url": None, "repos": ["/r/b"]},
    }


def test_edit_that_changes_nothing_writes_no_row(store, trk_home, forest):
    before = len(journal(trk_home))
    assert store("edit", forest["T"], "--title", "Ticket T", "--no-urgent", "--remove-repo", "/nope").code == 0
    assert len(journal(trk_home)) == before


def test_edit_bumps_updated_at(store, monkeypatch, forest):
    monkeypatch.setenv("TRK_NOW", "2030-01-01T00:00:00Z")
    store("edit", forest["T"], "--urgent")
    assert store("show", forest["T"], "--json").json()["item"]["updated_at"] == "2030-01-01T00:00:00.000Z"


@pytest.mark.parametrize(
    "argv",
    [
        ("--kind", "project"),
        ("--form", "self-contained"),
        ("--urgent", "--no-urgent"),
        ("--jira", "x", "--no-jira"),
        (),
        ("--title", " "),
        ("--jira", ""),
        ("--add-repo", " "),
    ],
)
def test_edit_misuse_is_a_usage_error(store, forest, argv):
    result = store("edit", forest["T"], *argv, "--json")
    assert result.code == 2 and result.error()["code"] == "usage"


def test_kind_and_form_are_immutable(store, forest):
    message = store("edit", forest["T"], "--form", "self-contained", "--json").error()["message"]
    assert "immutable" in message and "superseded" in message


def test_edit_unknown_item_exits_3(store):
    assert store("edit", "tr-zzzzz", "--urgent", "--json").code == 3
