"""A rating's base may not hide the PR's own changes (tr-eec4a): `--base` is refused (rule `base-hides-pr`, nothing
written) when the diff it leaves, `base...sha`, starts after a commit the PR itself brought in, and when a file the PR
changes (`gh`) is missing from the rated diff. The row keeps its base, so the Merged gate checks it again.

The fixtures are those of tr-xqea9: a `feature` branch cut at `old`, one PR commit (feature.txt), develop's 0032
migration merged in (the head until the tests add commits), develop then gaining 0033."""

import json
from pathlib import Path

import pytest

from trk import evidence
from gated import DEST, accept, commit, git
from test_blast_and_verified import blast, lane_ticket, listed, new_ticket, ok, refusal, run
from test_blast_stale_base import PR, gh, lanes, stale  # noqa: F401  (fixtures)

MIGRATION = "backend/drizzle/0032_add_jobs.sql"
RED = "backend/drizzle/9999_pr.sql"


def two_commit_pr(stale, gh):
    """The PR gains a second own commit on top of the merge of develop; returns (head, the first own commit)."""
    first = git(stale["path"], "rev-list", "--no-merges", "-n", "1", "HEAD", "--", "feature.txt")
    commit(stale["path"], "second.txt")
    gh(files=("feature.txt", "second.txt"))
    return git(stale["path"], "rev-parse", "HEAD"), first


def branch_ticket(trk, path, sha):
    """A built ticket whose `pr` row names a branch, so no `gh` is asked."""
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", path)
    accept(trk, j)
    t = new_ticket(trk, j)
    ok(trk, "evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", "feature", "--sha", sha, "--note", "x")
    run(trk, t, "built", sha)
    ok(trk, "advance", t)
    return t


def nothing_written(trk, t):
    assert trk("evidence", t, "--json").json()["evidence"][-1]["kind"] != "blast-radius"
    assert trk("show", t, "--json").json()["item"]["lane"] is None
    assert trk("waiting", "--all", "--json").json()["asks"] == []


def test_a_base_at_the_pr_head_is_refused_and_records_nothing(trk, stale, gh):
    head, _ = two_commit_pr(stale, gh)
    t = lane_ticket(trk, stale["path"], head)
    error = refusal(trk, "blast-radius", t, "--base", head)
    assert error["rule"] == "base-hides-pr" and error["item"] == t and "--base" in error["message"]
    nothing_written(trk, t)


def test_a_base_at_a_commit_the_pr_made_is_refused_and_records_nothing(trk, stale, gh):
    head, first = two_commit_pr(stale, gh)
    t = lane_ticket(trk, stale["path"], head)
    error = refusal(trk, "blast-radius", t, "--base", first)
    assert error["rule"] == "base-hides-pr" and first[:9] in error["message"]
    nothing_written(trk, t)
    mid = git(stale["path"], "rev-parse", "HEAD~1")  # the commit before the head is the PR's own too
    assert refusal(trk, "blast-radius", t, "--base", mid)["rule"] == "base-hides-pr"


def test_the_commit_the_branch_took_from_develop_is_accepted(trk, stale, gh):
    head, _ = two_commit_pr(stale, gh)
    t = lane_ticket(trk, stale["path"], head)
    row = blast(trk, t, "--base", stale["merged"])  # the last commit of develop the branch holds
    assert lanes(row) == {"feature.txt": "green", "second.txt": "green"}
    assert row["detail"]["base"] == stale["merged"]
    assert lanes(blast(trk, t)) == lanes(row)


def test_a_stacked_prs_parent_head_is_accepted(trk, stale, gh):
    """`--base <parent PR head>` (docs/agents.md step 7): the parent PR's head is the branch point of the child, whose
    base branch is the parent's."""
    parent = stale["head"]
    git(stale["path"], "push", "-q", "origin", f"{parent}:refs/heads/parent")
    commit(stale["path"], "child.txt")
    head = git(stale["path"], "rev-parse", "HEAD")
    gh(files=("child.txt",), base="parent")
    t = lane_ticket(trk, stale["path"], head)
    assert lanes(blast(trk, t, "--base", parent)) == {"child.txt": "green"}
    assert lanes(blast(trk, t)) == {"child.txt": "green"}
    assert refusal(trk, "blast-radius", t, "--base", head)["rule"] == "base-hides-pr"


def test_a_child_that_targets_develop_cannot_use_its_parents_head_as_base(trk, stale, gh):
    """trk cannot tell a parent PR's head from a commit of the PR when the child's base branch is not the parent's."""
    parent = stale["head"]
    commit(stale["path"], "child.txt")
    head = git(stale["path"], "rev-parse", "HEAD")
    gh(files=("child.txt",))
    t = lane_ticket(trk, stale["path"], head)
    assert refusal(trk, "blast-radius", t, "--base", parent)["rule"] == "base-hides-pr"


def test_a_pr_file_the_rated_diff_lacks_is_refused_naming_it(trk, stale, gh):
    t = lane_ticket(trk, stale["path"], stale["head"])
    gh(files=("feature.txt", "ghost.txt", MIGRATION))
    error = refusal(trk, "blast-radius", t, "--base", stale["old"])
    assert error["rule"] == "base-hides-pr" and "ghost.txt" in error["message"] and PR in error["message"]
    nothing_written(trk, t)
    assert refusal(trk, "blast-radius", t)["rule"] == "base-hides-pr"  # the default base is checked too


def test_a_merged_pr_rated_against_its_own_commit_is_refused(trk, stale, gh):
    head, first = two_commit_pr(stale, gh)
    t = lane_ticket(trk, stale["path"], head)
    git(stale["seed"], "fetch", "-q", stale["path"], "feature")
    git(stale["seed"], "merge", "-q", "--no-ff", head, "-m", "Merge PR 1")
    merge = git(stale["seed"], "rev-parse", "HEAD")
    git(stale["seed"], "push", "-q", "origin", "develop")
    git(stale["path"], "fetch", "-q", "origin")
    ok(trk, "evidence", "add", t, "--kind", "merge", "--gate", "merged", "--ref", PR, "--sha", merge, "--note", "m")
    for hiding in (head, first, merge):
        assert refusal(trk, "blast-radius", t, "--base", hiding)["rule"] in ("base-hides-pr", "empty-diff", "stale-base")
        assert refusal(trk, "blast-radius", t, "--base", hiding)["rule"] == "base-hides-pr"
    nothing_written(trk, t)
    assert blast(trk, t)["detail"]["lane"] == "green"  # the pre-merge parent still rates


def merge_into_develop(stale, head, how="--no-ff"):
    git(stale["seed"], "fetch", "-q", stale["path"], "feature")
    git(stale["seed"], "merge", "-q", how, head, *(["-m", "Merge PR 1"] if how == "--no-ff" else []))
    if how == "--squash":
        git(stale["seed"], "commit", "-q", "-m", "Squash PR 1")
    merge = git(stale["seed"], "rev-parse", "HEAD")
    git(stale["seed"], "push", "-q", "origin", "develop")
    git(stale["path"], "fetch", "-q", "origin")
    git(stale["path"], "remote", "set-head", "origin", "develop")
    return merge


class Checkout:
    """Stands in for `evidence._Repo`: every sha is in the one checkout."""

    def __init__(self, path):
        self.path = path

    def get(self, *shas):
        return self.path


@pytest.fixture
def red(stale):
    """The PR's first own commit after merging develop adds a RED migration (R), the next a GREEN file (G, the head)."""
    r = commit(stale["path"], RED)
    g = commit(stale["path"], "second.txt")
    git(stale["path"], "fetch", "-q", "origin")  # a builder without gh who merged develop has fetched it
    return {**stale, "R": r, "G": g}


def weigh_time(red, merge, base):
    row = {"detail": json.dumps({"lane": "green", "files": [], "base": base})}
    return evidence._base_hides(Checkout(red["path"]), row, red["G"], merge, red["G"])


def test_the_weigh_time_check_reads_a_merge_commits_first_parent(red):
    """A row recorded before the merge row existed: the merge row's first parent shows where the PR began."""
    merge = merge_into_develop(red, red["G"])
    assert weigh_time(red, merge, red["R"]) == "base-hides-pr"
    assert weigh_time(red, merge, red["merged"]) is None
    assert weigh_time(red, merge, red["old"]) is None


def test_a_merge_row_that_is_an_earlier_head_of_the_ticket_shows_nothing(red):
    """Only the merge of this PR gives a branch point: a stale merge row (another head, or its squash) must not."""
    earlier = git(red["path"], "rev-parse", f"{red['R']}^")
    assert weigh_time(red, earlier, red["R"]) is None
    assert weigh_time(red, red["G"], red["R"]) is None  # a fast-forward: the merge is the head itself


def test_the_weigh_time_check_reads_a_squash_merges_first_parent(red):
    """A squash commit does not hold the head, so only its first parent shows where the PR began."""
    merge = merge_into_develop(red, red["G"], "--squash")
    assert weigh_time(red, merge, red["R"]) == "base-hides-pr"
    assert weigh_time(red, merge, red["merged"]) is None
    assert weigh_time(red, merge, red["old"]) is None


def test_a_no_ff_merged_pr_shows_where_it_began_without_a_merge_row(trk, red):
    """develop's first-parent line has the merge that brought the PR in: no gh and no merge row are needed."""
    t = branch_ticket(trk, red["path"], red["G"])
    merge_into_develop(red, red["G"])
    assert refusal(trk, "blast-radius", t, "--base", red["R"])["rule"] == "base-hides-pr"
    nothing_written(trk, t)
    assert blast(trk, t, "--base", red["merged"])["detail"]["lane"] == "red"


def test_a_checkout_with_no_branch_the_pr_merges_into_refuses_a_base_without_gh(trk, red):
    """No origin/HEAD, main or master: nothing shows where the PR began, so a mid-PR base rated its RED file GREEN."""
    git(red["path"], "remote", "set-head", "origin", "-d")
    t = branch_ticket(trk, red["path"], red["G"])
    for base in (red["R"], red["merged"]):
        error = refusal(trk, "blast-radius", t, "--base", base)
        assert error["rule"] == "base-hides-pr" and "cannot show where the PR began" in error["message"]
    nothing_written(trk, t)


def test_a_branch_the_builder_just_made_is_no_branch_point(trk, red):
    """A local `main` made at a mid-PR commit would put the branch point after the hidden RED file."""
    git(red["path"], "remote", "set-head", "origin", "-d")
    git(red["path"], "branch", "main", red["R"])
    t = branch_ticket(trk, red["path"], red["G"])
    assert refusal(trk, "blast-radius", t, "--base", red["R"])["rule"] == "base-hides-pr"
    nothing_written(trk, t)


def test_an_origin_head_that_is_the_pr_itself_is_no_branch_point(trk, red):
    git(red["path"], "push", "-q", "origin", "feature")
    git(red["path"], "remote", "set-head", "origin", "feature")
    t = branch_ticket(trk, red["path"], red["G"])
    assert refusal(trk, "blast-radius", t, "--base", red["R"])["rule"] == "base-hides-pr"
    nothing_written(trk, t)


def test_a_fast_forward_merged_pr_refuses_a_base_without_gh(trk, red):
    """The PR's commits are history of develop now, so nothing shows which of them are the PR's."""
    t = branch_ticket(trk, red["path"], red["G"])
    git(red["path"], "push", "-q", "origin", f"{red['G']}:refs/heads/develop", "--force")
    git(red["path"], "fetch", "-q", "origin")
    git(red["path"], "remote", "set-head", "origin", "develop")
    error = refusal(trk, "blast-radius", t, "--base", red["R"])
    assert error["rule"] == "base-hides-pr" and "already holds the PR" in error["message"]
    nothing_written(trk, t)


def test_a_fast_forward_merge_row_does_not_make_the_heads_parent_a_branch_point(trk, red):
    """The merge row of a fast-forward is the head, and its first parent is a commit of the PR."""
    t = branch_ticket(trk, red["path"], red["G"])
    git(red["path"], "push", "-q", "origin", f"{red['G']}:refs/heads/develop", "--force")
    git(red["path"], "fetch", "-q", "origin")
    git(red["path"], "remote", "set-head", "origin", "develop")
    ok(trk, "evidence", "add", t, "--kind", "merge", "--gate", "merged", "--ref", "feature", "--sha", red["G"], "--note", "m")
    for base in (git(red["path"], "rev-parse", f"{red['G']}^"), red["R"]):
        error = refusal(trk, "blast-radius", t, "--base", base)
        assert error["rule"] == "base-hides-pr" and "already holds the PR" in error["message"]
    nothing_written(trk, t)


def test_a_fast_forward_merged_pr_with_gh_is_checked_by_its_file_list(trk, red, gh):
    gh(files=("feature.txt", RED, "second.txt"))
    t = lane_ticket(trk, red["path"], red["G"])
    git(red["path"], "push", "-q", "origin", f"{red['G']}:refs/heads/develop", "--force")
    git(red["path"], "fetch", "-q", "origin")
    error = refusal(trk, "blast-radius", t, "--base", red["R"])
    assert error["rule"] == "base-hides-pr" and RED in error["message"]
    nothing_written(trk, t)


def test_a_stale_branch_ref_is_named_in_the_refusal_not_blamed_on_the_base(trk, red):
    """No gh, and origin/develop is older than the develop commit the branch merged in: that legitimate base is
    refused, and the message says what trk knows."""
    git(red["path"], "update-ref", "refs/remotes/origin/develop", red["old"])
    t = branch_ticket(trk, red["path"], red["G"])
    error = refusal(trk, "blast-radius", t, "--base", red["merged"])
    assert error["rule"] == "base-hides-pr"
    assert "git fetch" in error["message"] and "as this checkout knows it" in error["message"]
    assert red["old"][:9] in error["message"]
