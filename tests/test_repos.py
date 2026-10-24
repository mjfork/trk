"""Repos are stored absolute (a relative `--repo` resolves against the current directory), `repos absolutize`
migrates old relative rows once, and a sha-unverifiable need names the checkout it tried."""

import subprocess

import pytest

from gated import DEST, git, commit, ticket


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, result.err
    return result.out.strip()


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """A HOME whose ~/projects holds a checkout `webapp` and a plain directory `plain`."""
    home = tmp_path / "home"
    (home / "projects").mkdir(parents=True)
    repo = home / "projects" / "webapp"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "work")
    (home / "projects" / "plain").mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home / "projects"


@pytest.fixture
def program(trk):
    ok(trk, "init", "--me", "michael")
    return ok(trk, "new", "program", "P", *DEST)


def repos_of(trk, item):
    return trk("show", item, "--json").json()["item"]["repos"]


def test_a_relative_repo_is_stored_absolute_from_any_directory_and_a_non_checkout_is_refused(
        trk, program, projects, monkeypatch):
    monkeypatch.chdir(projects)
    item = ok(trk, "new", "project", "A", "--parent", program, "--repo", "webapp")
    assert repos_of(trk, item) == [str(projects / "webapp")]
    monkeypatch.chdir(projects / "webapp")
    again = ok(trk, "new", "project", "B", "--parent", program, "--repo", ".")
    assert repos_of(trk, again) == [str(projects / "webapp")]
    monkeypatch.chdir(projects)
    refused = trk("new", "project", "C", "--parent", program, "--repo", "plain")
    assert refused.code == 2 and "not a git checkout" in refused.err
    ok(trk, "edit", item, "--add-repo", "git@github.com:o/r.git", "--add-repo", "/abs/x")
    assert repos_of(trk, item) == [str(projects / "webapp"), "git@github.com:o/r.git", "/abs/x"]
    ok(trk, "edit", item, "--remove-repo", "webapp")
    assert repos_of(trk, item) == ["git@github.com:o/r.git", "/abs/x"]


def plant_relative(trk_home, item, repo):
    import sqlite3

    conn = sqlite3.connect(trk_home / "store.db")
    conn.execute("INSERT INTO item_repos (item_id, repo) VALUES (?, ?)", (item, repo))
    conn.commit()
    conn.close()


def test_absolutize_changes_what_it_can_places_lists_the_rest_and_dry_run_writes_nothing(
        trk, program, projects, trk_home):
    a = ok(trk, "new", "project", "A", "--parent", program)
    b = ok(trk, "new", "project", "B", "--parent", program)
    plant_relative(trk_home, a, "webapp")
    plant_relative(trk_home, b, "gone")
    before = trk("show", a, "--json").json()
    dry = trk("repos", "absolutize", "--dry-run", "--json").json()
    assert dry["dry_run"] is True
    assert dry["changed"] == [{"id": a, "old": ["webapp"], "new": [str(projects / "webapp")]}]
    assert dry["unresolved"] == [{"item": b, "repo": "gone"}]
    assert trk("show", a, "--json").json() == before
    done = trk("repos", "absolutize", "--json").json()
    assert done["changed"] == dry["changed"] and done["unresolved"] == dry["unresolved"]
    assert repos_of(trk, a) == [str(projects / "webapp")] and repos_of(trk, b) == ["gone"]
    assert trk("repos", "absolutize", "--json").json()["changed"] == []  # once


def test_a_gate_result_is_the_same_from_two_directories(trk, program, projects, monkeypatch):
    monkeypatch.chdir(projects)
    j = ok(trk, "new", "project", "J", "--parent", program, "--repo", "webapp")
    t = ok(trk, "new", "work-item", "T", "--form", "self-contained", "--parent", j, *ticket("self-contained"))
    elsewhere = projects / "webapp"
    first = trk("show", t, "--json").json()["item"]["gate"]
    monkeypatch.chdir(elsewhere)
    assert trk("show", t, "--json").json()["item"]["gate"] == first


def test_an_unplaceable_sha_names_the_checkout_tried_and_suggests_fetch(trk, program, projects):
    repo = str(projects / "webapp")
    j = ok(trk, "new", "project", "J", "--parent", program, "--repo", repo)
    t = ok(trk, "new", "work-item", "T", "--form", "self-contained", "--parent", j, *ticket("self-contained"))
    ok(trk, "advance", t, "--judgment", "framed well")
    ok(trk, "evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", "none", "--sha", git(repo, "rev-parse", "HEAD"),
       "--note", "PR")
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "run", "--sha", "deadbeef",
       "--verdict", "pass", "--note", "ok")
    result = trk("advance", t, "--json")
    need = next(m for m in result.error()["missing"] if m["need"] == "sha-unverifiable")
    assert need["checkout"] == repo and "git fetch" in need["hint"]
