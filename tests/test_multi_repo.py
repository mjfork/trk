"""A sha on a ticket with several repos is placed in whichever repo holds it (`gitrepo.place`): a ticket whose code
lives in its second repo passes the gates on its real commits, and a sha in none of them stays sha-unverifiable."""

import pytest

from gated import DEST, accept, commit, git, ticket


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, result.err
    return result.out.strip()


def checkout(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


@pytest.fixture
def world(trk, tmp_path):
    """Repo A (first) and repo B, both on the project J; the ticket's code is in B: b0, b1 (head), b2 (later)."""
    a, b = checkout(tmp_path, "A"), checkout(tmp_path, "B")
    commit(a, "a.txt")
    b0 = commit(b, ".trk/blast-radius", "# no rules: every change rates green\n")
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", str(a), "--repo", str(b))
    accept(trk, j)
    t = ok(trk, "new", "work-item", "T", "--form", "spec-bound", "--parent", j, *ticket("spec-bound"))
    return {"t": t, "a": a, "b": b, "b0": b0}


def add(trk, t, *argv):
    return ok(trk, "evidence", "add", t, *argv, "--as", "worker")


def test_rows_at_a_commit_of_the_second_repo_count_and_the_ticket_reaches_verified(trk, world):
    t, b = world["t"], world["b"]
    git(b, "checkout", "-q", "-b", "feature")  # main is where the PR merges into
    b1 = commit(b, "b1.txt")
    add(trk, t, "--kind", "pr", "--gate", "built", "--ref", "none", "--sha", b1, "--note", "PR")
    add(trk, t, "--kind", "test-run", "--gate", "built", "--ref", "run", "--sha", b1[:9], "--verdict", "pass",
        "--note", "green")  # the short form equals the head only once git places it, in B and not in A
    ok(trk, "advance", t, "--as", "worker")
    assert ok(trk, "blast-radius", t, "--base", world["b0"], "--as", "worker") == "green"
    add(trk, t, "--kind", "merge", "--gate", "merged", "--ref", "main", "--sha", b1, "--note", "merged")
    ok(trk, "advance", t, "--as", "worker")
    b2 = commit(b, "b2.txt")
    add(trk, t, "--kind", "test-run", "--gate", "verified", "--criterion", "ac-1", "--ref", "run", "--sha", b2,
        "--verdict", "pass", "--note", "holds")  # contains the merge sha, in B
    ok(trk, "advance", t, "--outcome", "Done.", "--as", "worker")


def test_a_sha_in_neither_repo_is_still_sha_unverifiable(trk, world):
    t, b = world["t"], world["b"]
    b1 = commit(b, "b1.txt")
    add(trk, t, "--kind", "pr", "--gate", "built", "--ref", "none", "--sha", b1, "--note", "PR")
    unknown = add(trk, t, "--kind", "test-run", "--gate", "built", "--ref", "run", "--sha", "deadbeef",
                  "--verdict", "pass", "--note", "green")
    result = trk("advance", t, "--as", "worker", "--json")
    assert result.code == 1
    missing = result.error()["missing"]
    (need,) = [m for m in missing if m["need"] == "sha-unverifiable"]
    assert (need["evidence"], need["sha"]) == (unknown, "deadbeef")
    assert need["checkout"] == [str(world["a"]), str(world["b"])]  # every repo searched, in order
    assert str(world["a"]) in need["hint"] and str(world["b"]) in need["hint"] and "git fetch" in need["hint"]
    text = trk("show", t).out
    assert f"{world['a']}, {world['b']}" in text  # the text form joins them
