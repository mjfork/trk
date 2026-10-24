"""Kinds, forms, nesting, depth and root, through `trk new` and `trk show`."""

import json
import sqlite3

import pytest

from gated import new_flags


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
    return trk


def new(trk, *argv):
    result = trk("new", *argv, *new_flags(argv), "--json")
    assert result.code == 0, result.err
    return result.json()["item"]


def journal(trk_home, change=None):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM journal ORDER BY seq")]
    finally:
        conn.close()
    for row in rows:
        row["data"] = json.loads(row["data"])
    return [r for r in rows if change is None or r["change"] == change]


def test_every_kind_and_form_can_be_created_under_a_program(store):
    program = new(store, "program", "P")
    project = new(store, "project", "J", "--parent", program["id"])
    assert project["kind"] == "project" and project["form"] is None and project["spec_state"] == "draft"
    for form in ("spec-bound", "self-contained"):
        ticket = new(store, "work-item", form, "--form", form, "--parent", project["id"])
        assert (ticket["kind"], ticket["form"], ticket["subtype"]) == ("work-item", form, None)
    decision = new(store, "work-item", "D", "--form", "decision", "--subtype", "grilling", "--parent", program["id"])
    assert (decision["form"], decision["subtype"]) == ("decision", "grilling")
    sub = new(store, "program", "Sub", "--parent", program["id"])
    assert sub["parent"] == program["id"]


@pytest.mark.parametrize(
    "argv, message",
    [
        (("work-item", "T"), "--form"),
        (("work-item", "T", "--form", "decision"), "--subtype"),
        (("work-item", "T", "--form", "ask"), "trk ask"),
        (("work-item", "T", "--form", "spec-bound", "--subtype", "task"), "--subtype"),
        (("project", "J", "--form", "decision"), "--form"),
        (("program", "P", "--subtype", "task"), "--subtype"),
        (("work-item", "T", "--form", "nonsense"), "nonsense"),
        (("program", "P", "--jira", ""), "--jira"),
        (("program", "P", "--repo", ""), "repo"),
    ],
)
def test_form_and_subtype_misuse_is_a_usage_error(store, argv, message):
    result = store("new", *argv, "--json")
    assert result.code == 2
    assert result.error()["code"] == "usage" and message in result.error()["message"]


@pytest.mark.parametrize(
    "parent_argv, child_argv",
    [
        (("work-item", "W", "--form", "self-contained"), ("project", "X")),  # AC 2 (nesting part)
        (("work-item", "W", "--form", "self-contained"), ("program", "X")),
        (("project", "J"), ("program", "X")),
    ],
)
def test_a_child_kind_never_ranks_above_its_parent(store, trk_home, parent_argv, child_argv):
    parent = new(store, *parent_argv)
    rows_before = len(journal(trk_home))
    result = store("new", *child_argv, *new_flags(child_argv), "--parent", parent["id"], "--json")
    assert result.code == 1
    assert result.error()["rule"] == "nesting" and result.error()["item"] == parent["id"]
    assert len(journal(trk_home)) == rows_before


def test_work_items_nest_under_work_items_and_projects_under_projects(store):
    project = new(store, "project", "J")
    assert new(store, "project", "J2", "--parent", project["id"])["depth"] == 1
    ticket = new(store, "work-item", "T", "--form", "self-contained", "--parent", project["id"])
    child = new(store, "work-item", "C", "--form", "decision", "--subtype", "task", "--parent", ticket["id"])
    assert (child["depth"], child["root"]) == (2, project["id"])


def test_unknown_parent_exits_3(store):
    result = store("new", "project", "X", "--parent", "tr-zzzzz", "--json")
    assert result.code == 3 and result.error()["item"] == "tr-zzzzz"


def test_depth_and_root_on_show_and_on_journal_rows(store, trk_home):
    program = new(store, "program", "P")
    assert (program["depth"], program["root"]) == (0, program["id"])
    project = new(store, "project", "J", "--parent", program["id"])
    ticket = new(store, "work-item", "T", "--form", "spec-bound", "--parent", project["id"])
    shown = store("show", ticket["id"], "--json").json()["item"]
    assert (shown["parent"], shown["root"], shown["depth"]) == (project["id"], program["id"], 2)
    row = journal(trk_home, "item.created")[-1]
    assert (row["item"], row["kind"], row["form"], row["parent"], row["root"], row["depth"]) == (
        ticket["id"], "work-item", "spec-bound", project["id"], program["id"], 2,
    )


def test_created_row_carries_subtype_rank_urgent_jira_and_repos(store, trk_home):
    decision = new(
        store, "work-item", "D", "--form", "decision", "--subtype", "research", "--urgent",
        "--jira", "https://example.atlassian.net/browse/PROJ-1",
        "--repo", "/srv/projects/a", "--repo", "git@github.com:x/b.git",
    )
    assert decision["urgent"] is True
    assert decision["jira_url"] == "https://example.atlassian.net/browse/PROJ-1"
    assert decision["repos"] == ["/srv/projects/a", "git@github.com:x/b.git"]
    assert journal(trk_home, "item.created")[-1]["data"] == {
        "subtype": "research",
        "rank": decision["rank"],
        "urgent": True,
        "jira_url": "https://example.atlassian.net/browse/PROJ-1",
        "repos": ["/srv/projects/a", "git@github.com:x/b.git"],
        "stage": None,
        "criteria": [],
        "sections": [],
        "owner": "michael",  # top-level, so the default owner
    }


def test_show_lists_children_in_rank_order(store):
    program = new(store, "program", "P")
    b = new(store, "project", "B", "--parent", program["id"])
    a = new(store, "project", "A", "--parent", program["id"], "--first")
    shown = store("show", program["id"], "--json").json()["item"]
    assert shown["children"] == [
        {"id": a["id"], "kind": "project", "form": None, "title": "A", "status": "open"},
        {"id": b["id"], "kind": "project", "form": None, "title": "B", "status": "open"},
    ]
    human = store("show", program["id"]).out
    assert human.index(a["id"]) < human.index(b["id"])
