"""Red-first proof rows (tr-fep6h): a run that failed on purpose at a base before the fix. It is recorded with
`evidence add --red-first`, listed as `failed as intended`, valid on an ancestor of the merge, and counted only
beside a passing run of the same criterion; it never decides the criterion's status.

Runs against a throwaway git repo R: `base` on main, `head` on the feature branch, `other` a later main commit
that does not contain `head`, and `m`, main's merge of the feature branch."""

import re

import pytest

from gated import DEST, accept, commit, git, ticket


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "R"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    shas = {"path": str(r), "base": commit(r, ".trk/blast-radius", "# no rules: every change rates green\n")}
    git(r, "checkout", "-q", "-b", "feature")
    shas["head"] = commit(r, "feature")
    git(r, "checkout", "-q", "main")
    shas["other"] = commit(r, "other")
    git(r, "merge", "-q", "--no-ff", "feature", "-m", "merge")
    shas["m"] = git(r, "rev-parse", "HEAD")
    return shas


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def listed(trk, item_id, *flags):
    return trk("evidence", item_id, *flags, "--json").json()["evidence"]


def add(trk, item_id, kind, gate, *flags, note="x"):
    return ok(trk, "evidence", "add", item_id, "--kind", kind, "--gate", gate, *flags, "--note", note)


def verified_run(trk, t, sha, *flags, verdict=None):
    verdict_flags = ("--verdict", verdict) if verdict else ()
    return add(trk, t, "test-run", "verified", "--ref", "run-1", "--sha", sha, "--criterion", "ac-1",
               *verdict_flags, *flags)


@pytest.fixture
def merged(trk, repo):
    """A ticket T at Merged on repo R, as the green lane walks it."""
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", repo["path"])
    accept(trk, j)
    t = ok(trk, "new", "work-item", "T", "--form", "spec-bound", "--parent", j, *ticket("spec-bound"))
    add(trk, t, "pr", "built", "--ref", "https://github.com/o/r/pull/1", "--sha", repo["head"])
    add(trk, t, "test-run", "built", "--ref", "run-1", "--sha", repo["head"], "--verdict", "pass")
    ok(trk, "advance", t)
    ok(trk, "blast-radius", t, "--base", repo["base"])
    add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", repo["m"])
    ok(trk, "advance", t)
    return t


def by_id(trk, t):
    return {e["id"]: e for e in listed(trk, t)}


def test_a_red_first_row_is_a_fail_run_marked_red_first_and_listed_as_failed_as_intended(trk, merged, repo):
    red = verified_run(trk, merged, repo["base"], "--red-first")
    row = by_id(trk, merged)[red]
    assert (row["verdict"], row["red_first"]) == ("fail", True)
    line = ok(trk, "evidence", merged)
    assert "failed as intended" in line
    assert "fail  " not in line.split("—")[0]


def test_a_red_first_row_on_an_ancestor_of_the_merge_has_no_wrong_sha(trk, merged, repo):
    red = verified_run(trk, merged, repo["base"], "--red-first")
    green = verified_run(trk, merged, repo["m"], verdict="pass")
    rows = by_id(trk, merged)
    assert (rows[red]["reason"], rows[red]["counts"]) == (None, True)
    assert (rows[green]["reason"], rows[green]["counts"]) == (None, True)
    # a plain fail run on the same ancestor is still a wrong-sha row
    plain = verified_run(trk, merged, repo["base"], verdict="fail")
    assert by_id(trk, merged)[plain]["reason"] == "wrong-sha"


def test_a_red_first_row_not_contained_in_the_merge_is_wrong_sha(trk, merged, repo):
    git(repo["path"], "checkout", "-q", "-b", "side", repo["base"])
    stray = commit(repo["path"], "stray")
    git(repo["path"], "checkout", "-q", "main")
    red = verified_run(trk, merged, stray, "--red-first")
    verified_run(trk, merged, repo["m"], verdict="pass")
    assert by_id(trk, merged)[red]["reason"] == "wrong-sha"


def test_a_red_first_row_counts_only_beside_a_green_row_of_its_criterion(trk, merged, repo):
    red = verified_run(trk, merged, repo["base"], "--red-first")
    row = by_id(trk, merged)[red]
    assert (row["counts"], row["reason"]) == (False, "unpaired-red-first")
    verified_run(trk, merged, repo["m"], verdict="pass")
    assert by_id(trk, merged)[red]["counts"] is True


def test_a_red_first_row_never_decides_the_criterion_or_blocks_verified(trk, merged, repo):
    verified_run(trk, merged, repo["m"], verdict="pass")
    verified_run(trk, merged, repo["base"], "--red-first")
    shown = trk("criteria", merged, "--json").json()
    status = {c["handle"]: c["status"] for c in shown["criteria"]}
    assert status["ac-1"] == "pass"
    ok(trk, "advance", merged, "--outcome", "Done.")
    assert trk("show", merged, "--json").json()["item"]["status"] == "closed"


def test_a_red_first_row_alone_leaves_the_criterion_unproven(trk, merged, repo):
    verified_run(trk, merged, repo["base"], "--red-first")
    result = trk("advance", merged, "--outcome", "Done.", "--json")
    assert result.code == 1
    assert result.error()["missing"][0]["need"] == "criterion-pass"
    assert result.error()["missing"][0]["latest"] is None


def test_red_first_needs_a_run_at_verified_with_a_criterion_and_no_pass_verdict(trk, merged, repo):
    sha = repo["base"]
    for flags in (
        ("--kind", "test-run", "--gate", "built", "--sha", sha, "--criterion", "ac-1"),
        ("--kind", "test-run", "--gate", "verified", "--sha", sha),
        ("--kind", "judgment", "--gate", "verified", "--sha", sha, "--criterion", "ac-1"),
        ("--kind", "test-run", "--gate", "verified", "--sha", sha, "--criterion", "ac-1", "--verdict", "pass"),
    ):
        result = trk("evidence", "add", merged, *flags, "--ref", "r", "--red-first", "--note", "x", "--json")
        assert result.code == 2, (flags, result.out, result.err)
    assert listed(trk, merged, "--gate", "verified") == []


def test_red_first_accepts_an_explicit_fail_verdict(trk, merged, repo):
    red = verified_run(trk, merged, repo["base"], "--red-first", verdict="fail")
    assert by_id(trk, merged)[red]["red_first"] is True


def test_a_red_first_row_at_the_merge_sha_cannot_hide_a_failure(trk, merged, repo):
    verified_run(trk, merged, repo["m"], verdict="pass")
    hidden = verified_run(trk, merged, repo["m"], "--red-first")
    row = by_id(trk, merged)[hidden]
    assert (row["red_first"], row["verdict"], row["counts"]) == (False, "fail", True)
    shown = trk("criteria", merged, "--json").json()
    assert {c["handle"]: c["status"] for c in shown["criteria"]}["ac-1"] == "fail"
    result = trk("advance", merged, "--outcome", "Done.", "--json")
    assert result.code == 1
    assert result.error()["missing"][0]["need"] == "criterion-pass"
    assert trk("show", merged, "--json").json()["item"]["status"] != "closed"


def test_a_red_first_row_after_the_merge_cannot_hide_a_failure(trk, merged, repo):
    verified_run(trk, merged, repo["m"], verdict="pass")
    git(repo["path"], "checkout", "-q", "main")
    later = commit(repo["path"], "later")
    verified_run(trk, merged, later, "--red-first")
    assert trk("advance", merged, "--outcome", "Done.", "--json").code == 1


def test_the_page_shows_a_red_first_row_as_failed_as_intended(trk, merged, repo):
    red = verified_run(trk, merged, repo["base"], "--red-first")
    plain = verified_run(trk, merged, repo["m"], verdict="pass")
    result = trk("pages", "render", merged, "--out", "-")
    assert result.code == 0, result.err
    rows = {i: re.search(rf'<tr[^>]*evidence-{i}.*?</tr>', result.out, re.S).group(0) for i in (red, plain)}
    assert "failed as intended" in rows[red]
    assert "<td>fail</td>" not in rows[red]
    assert "<td>pass</td>" in rows[plain]
    assert "failed as intended" not in rows[plain]
