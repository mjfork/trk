"""`trk spec accept|draft`: a project's spec state, journaled as `spec.changed`."""

import json
import sqlite3

import pytest

from gated import acceptable, new_flags, prove_close, run_all


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
def project(store):
    return new(store, "project", "Project J")


@pytest.fixture
def ready_project(store, project):
    """Project J with its accept gate met."""
    run_all(store, acceptable(project))
    return project


def test_a_new_project_starts_draft(store, project):
    assert store("show", project, "--json").json()["item"]["spec_state"] == "draft"


def test_accept_sets_accepted_and_journals_spec_changed(store, trk_home, ready_project):
    project = ready_project
    result = store("spec", "accept", project, "--session", "s-1")
    assert result.code == 0, result.err
    assert result.out.strip() == project
    assert store("show", project, "--json").json()["item"]["spec_state"] == "accepted"
    row = journal(trk_home)[-1]
    assert row["change"] == "spec.changed"
    assert row["item"] == project
    assert row["session"] == "s-1"
    judgment = store("evidence", project, "--gate", "accept", "--json").json()["evidence"][0]["id"]
    assert row["data"] == {"old": "draft", "new": "accepted", "acceptor": "michael", "judgment": judgment}


def test_draft_puts_an_accepted_spec_back(store, trk_home, ready_project):
    project = ready_project
    store("spec", "accept", project)
    result = store("spec", "draft", project, "--reason", "Rethink.", "--json")
    assert result.code == 0, result.err
    assert result.json()["item"]["spec_state"] == "draft"
    assert journal(trk_home)[-1]["data"] == {"old": "accepted", "new": "draft", "reason": "Rethink."}


def test_setting_the_state_it_already_has_writes_no_row(store, trk_home, project):
    before = len(journal(trk_home))
    assert store("spec", "draft", project, "--reason", "Already.").code == 0
    assert len(journal(trk_home)) == before


def test_accept_bumps_updated_at(store, monkeypatch, ready_project):
    project = ready_project
    monkeypatch.setenv("TRK_NOW", "2030-01-01T00:00:00Z")
    item = store("spec", "accept", project, "--json").json()["item"]
    assert item["updated_at"] == "2030-01-01T00:00:00.000Z"


@pytest.mark.parametrize("argv", [
    ("program", "P"),
    ("work-item", "T", "--form", "self-contained"),
])
def test_spec_refuses_non_projects(store, argv):
    item = new(store, *argv)
    result = store("spec", "accept", item, "--json")
    assert result.code == 1
    assert result.error()["rule"] == "not-project"
    assert result.error()["item"] == item


def test_spec_refuses_closed_projects(store, project):
    prove_close(store, project)
    store("close", project, "--resolution", "done", "--outcome", "Shipped.")
    result = store("spec", "draft", project, "--reason", "Reopened.", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "closed"
    assert result.error()["item"] == project


def test_spec_on_an_unknown_item_exits_3(store):
    assert store("spec", "accept", "tr-zzzzz").code == 3


def test_spec_needs_accept_or_draft(store, project):
    assert store("spec", "approve", project).code == 2
