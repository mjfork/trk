"""A ticket whose PR merged before its repo had `.trk/blast-radius` (Stage gates, Merged): `blast-radius` is
refused at the merge sha (no rules there), so a rating at a descendant of the merge sha, whose diff from the
pre-merge base holds the whole PR, counts at the Merged gate in place of one at the head sha.

Every test runs against a real throwaway git repo R: `base` (no rules file) on main, the feature branch's head
`head`, `m`, main's merge of it, `rules`, the later main commit that adds the rules file, and `side`, a commit
off `base` with the rules file that does not contain the merge."""

import pytest

from gated import DEST, accept, commit, git
from test_blast_and_verified import add, built, item, listed, merge, new_ticket, ok, refusal, write_brief

RULES = ".trk/blast-radius"


@pytest.fixture
def late(trk, tmp_path):
    r = tmp_path / "R"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    shas = {"path": str(r), "base": commit(r, "README.md")}
    git(r, "checkout", "-q", "-b", "feature")
    shas["head"] = commit(r, "src/a.py")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--no-ff", "feature", "-m", "merge")
    shas["m"] = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "-b", "side", shas["base"])
    shas["side"] = commit(r, RULES, "# all green\n")
    git(r, "checkout", "-q", "main")
    shas["rules"] = commit(r, RULES, "RED src/**\n")
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", str(r))
    accept(trk, j)
    return {**shas, "J": j}


def merged_ticket(trk, late):
    """A ticket Built at `head` whose merge row is at `m`, still waiting on its rating."""
    t = new_ticket(trk, late["J"])
    built(trk, late, t)
    merge(trk, t, late["m"])
    return t


def needs(trk, t):
    return [m["need"] for m in item(trk, t)["gate"]["missing"]]


def test_the_rules_arrived_after_the_merge_so_a_rating_at_the_merge_sha_is_refused(trk, late):
    t = merged_ticket(trk, late)
    error = refusal(trk, "blast-radius", t, "--sha", late["m"], "--base", late["base"])
    assert (error["rule"], error["item"]) == ("no-blast-rules", t)
    assert needs(trk, t) == ["blast-radius"]


def test_a_rating_at_a_descendant_of_the_merge_sha_counts_and_the_ticket_merges(trk, late):
    t = merged_ticket(trk, late)
    result = trk("blast-radius", t, "--sha", late["rules"], "--base", late["base"], "--json")
    assert result.code == 0, result.err
    row = result.json()["evidence"]
    assert (row["sha"], row["detail"]["lane"]) == (late["rules"], "red")
    assert {e["id"]: e["reason"] for e in listed(trk, t)}[row["id"]] is None
    assert item(trk, t)["lane"] == "red"
    assert "blast-radius" not in needs(trk, t)


def test_a_descendant_that_adds_the_rules_file_rates_the_prs_files_by_its_rules_and_does_not_list_it(trk, late):
    """tr-3qtnd (ac-1): the rules file a later PR added is that PR's file, not the merged PR's, so it is not listed
    (it was, as a RED floor, before); the merged PR's files are rated by the rules of the rated tree."""
    git(late["path"], "checkout", "-q", "main")
    green = commit(late["path"], RULES, "# all green\n")
    t = merged_ticket(trk, late)
    assert rated(trk, t, "--sha", green, "--base", late["base"]) == ("green", ["src/a.py"], True)
    assert "blast-radius" not in needs(trk, t)


def test_a_rating_at_a_commit_that_does_not_contain_the_merge_still_does_not_count(trk, late):
    t = merged_ticket(trk, late)
    row = ok(trk, "blast-radius", t, "--sha", late["side"], "--base", late["base"])
    assert row == "red"  # the rules file it adds is a RED floor
    reasons = {e["kind"]: e["reason"] for e in listed(trk, t) if e["kind"] == "blast-radius"}
    assert reasons == {"blast-radius": "wrong-sha"}
    assert needs(trk, t) == ["blast-radius"]


def test_a_base_that_already_holds_the_merge_would_leave_the_pr_out_of_the_rating(trk, late):
    t = merged_ticket(trk, late)
    before = len(listed(trk, t))
    error = refusal(trk, "blast-radius", t, "--sha", late["rules"], "--base", late["m"])
    assert error["rule"] == "stale-base"
    assert late["m"] in error["message"]
    assert len(listed(trk, t)) == before


def test_the_red_read_ask_names_the_head_to_approve_not_the_descendant_that_was_rated(trk, late):
    t = merged_ticket(trk, late)
    ok(trk, "blast-radius", t, "--sha", late["rules"], "--base", late["base"], "--as", "worker")
    write_brief(trk, t)  # a RED-read ask stays held from `waiting` until the brief is complete (tr-4bhay)
    asks = trk("waiting", "--me", "michael", "--json").json()
    ask = next(a for a in asks["asks"] if a["parent"]["id"] == t)
    question = ok(trk, "section", "get", ask["id"], "question")
    assert f"rated RED at {late['rules']}" in question
    assert f"--sha {late['head']}" in question
    assert f"--sha {late['rules']}" not in question
    ok(trk, "review", "record", t, "approved", "--sha", late["head"], "--note", "Read it.", "--as", "michael")
    assert any(e["kind"] == "human-review" and e["counts"] for e in listed(trk, t))


def test_a_base_at_the_pr_head_after_a_no_ff_merge_would_hide_the_prs_red_files(trk, late):
    """`m` does not descend from `head`'s side of the merge, so `base...rules` with base `head` shows only the
    rules file: GREEN, with the PR's `src/a.py` (RED under the rules) never rated."""
    t = merged_ticket(trk, late)
    before = len(listed(trk, t))
    error = refusal(trk, "blast-radius", t, "--sha", late["rules"], "--base", late["head"])
    assert error["rule"] == "stale-base"
    assert late["head"] in error["message"] and "PR head" in error["message"]
    assert len(listed(trk, t)) == before
    assert needs(trk, t) == ["blast-radius"]


@pytest.fixture
def early(trk, tmp_path):
    """The PR's head predates the rules file, which main already had when it merged the PR (tr-e799e, PR A):
    `head` has no rules, `rules` lands on main, `m` is the no-ff merge of `head` into it."""
    r = tmp_path / "E"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    shas = {"path": str(r), "base": commit(r, "README.md")}
    git(r, "checkout", "-q", "-b", "feature")
    shas["head"] = commit(r, "src/a.py")
    git(r, "checkout", "-q", "main")
    shas["rules"] = commit(r, RULES, "RED src/**\n")
    git(r, "merge", "-q", "--no-ff", "feature", "-m", "merge")
    shas["m"] = git(r, "rev-parse", "HEAD")
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    shas["J"] = ok(trk, "new", "project", "J", "--parent", p, "--repo", str(r))
    accept(trk, shas["J"])
    return shas


@pytest.mark.parametrize("flags", [(), ("--base", "{rules}")], ids=["default-base", "first-parent-base"])
def test_a_rating_at_the_merge_commit_itself_counts_with_the_prs_red_files_still_red(trk, early, flags):
    t = merged_ticket(trk, early)
    flags = [f.format(**early) for f in flags]
    result = trk("blast-radius", t, "--sha", early["m"], *flags, "--json")
    assert result.code == 0, result.err
    row = result.json()["evidence"]
    assert (row["sha"], row["detail"]["lane"]) == (early["m"], "red")
    assert [(f["path"], f["colour"]) for f in row["detail"]["files"]] == [("src/a.py", "red")]
    assert {e["id"]: e["reason"] for e in listed(trk, t)}[row["id"]] is None
    assert item(trk, t)["lane"] == "red"
    assert "blast-radius" not in needs(trk, t)


def test_a_rating_at_the_merge_commit_is_refused_against_a_base_at_the_pr_head(trk, early):
    t = merged_ticket(trk, early)
    error = refusal(trk, "blast-radius", t, "--sha", early["m"], "--base", early["head"])
    assert error["rule"] == "stale-base" and "PR head" in error["message"]


# --- the audit of 207ee96 (ledger-6, -7, -8): each probe is a way to count a rating that hides the PR ---

def rated(trk, t, *flags):
    """(lane, the rated paths, whether the row counts) of a `trk blast-radius` run, or its refusal rule."""
    result = trk("blast-radius", t, *flags, "--json")
    if result.code:
        return ("refused", result.error()["rule"])
    row = result.json()["evidence"]
    counts = {e["id"]: e["reason"] for e in listed(trk, t)}[row["id"]] is None
    return row["detail"]["lane"], [f["path"] for f in row["detail"]["files"]], counts


def test_a_later_revert_of_the_merge_cannot_rate_below_what_the_pr_changed(trk, early):
    """ledger-6: `git revert -m1 m` rated against the pre-PR base shows no files; the PR's own `src/a.py` stays in."""
    git(early["path"], "revert", "-m", "1", "--no-edit", early["m"])  # on main: a late rating's sha is on the base branch
    evil = git(early["path"], "rev-parse", "HEAD")
    t = merged_ticket(trk, early)
    assert rated(trk, t, "--sha", evil, "--base", early["rules"]) == ("red", ["src/a.py"], True)
    assert item(trk, t)["lane"] == "red"
    assert "blast-radius" not in needs(trk, t) and "audit" in needs(trk, t)


def test_a_later_delete_of_the_prs_file_cannot_rate_below_what_the_pr_changed(trk, early):
    git(early["path"], "rm", "-q", "src/a.py")
    git(early["path"], "commit", "-q", "-m", "rm")
    evil = git(early["path"], "rev-parse", "HEAD")
    t = merged_ticket(trk, early)
    assert rated(trk, t, "--sha", evil, "--base", early["rules"]) == ("red", ["src/a.py"], True)


def test_a_rating_made_before_the_merge_row_never_counts_even_with_a_base_at_the_pr_head(trk, early):
    """ledger-7: the base check cannot run without a merge row, so the row waits for one to be re-rated against."""
    later = commit(early["path"], "docs/x.md")  # main after the merge
    t = new_ticket(trk, early["J"])
    built(trk, early, t)
    # tr-eec4a: with no merge row, a base at the PR head is refused outright (the head is the PR's own commit)
    assert rated(trk, t, "--sha", later, "--base", early["head"]) == ("refused", "base-hides-pr")
    merge(trk, t, early["m"])
    assert [e["reason"] for e in listed(trk, t) if e["kind"] == "blast-radius"] == []
    assert needs(trk, t) == ["blast-radius"]
    assert rated(trk, t, "--sha", later, "--base", early["head"]) == ("refused", "stale-base")
    assert rated(trk, t, "--sha", later, "--base", early["rules"])[2] is True  # re-rated after the merge row: counts


def test_a_merge_row_naming_a_pre_pr_commit_anchors_no_descendant_rating(trk, early):
    """ledger-8: the base commit holds neither the PR head nor its patch, so a rating above it proves nothing."""
    git(early["path"], "checkout", "-q", "-b", "other", early["rules"])
    other = commit(early["path"], "docs/y.md")
    t = new_ticket(trk, early["J"])
    built(trk, early, t)
    merge(trk, t, early["rules"])
    assert rated(trk, t, "--sha", other, "--base", early["base"])[2] is False
    assert needs(trk, t) == ["blast-radius"]


def test_a_merge_row_naming_an_unrelated_commit_anchors_a_rating_at_that_commit_no_better(trk, early):
    git(early["path"], "checkout", "-q", "-b", "other", early["rules"])
    other = commit(early["path"], "docs/y.md")
    t = new_ticket(trk, early["J"])
    built(trk, early, t)
    merge(trk, t, other)
    assert rated(trk, t, "--sha", other, "--base", early["rules"])[2] is False
    assert needs(trk, t) == ["blast-radius"]


def test_a_squash_merge_carries_the_prs_patch_so_a_later_rating_counts_and_keeps_its_files(trk, tmp_path):
    r = tmp_path / "Q"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    base = commit(r, "README.md")
    git(r, "checkout", "-q", "-b", "feature")
    head = commit(r, "src/a.py")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--squash", "feature")
    git(r, "commit", "-q", "-m", "squash")
    squash = git(r, "rev-parse", "HEAD")
    rules = commit(r, RULES, "RED src/**\n")
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", str(r))
    accept(trk, j)
    world = {"J": j, "head": head}
    t = new_ticket(trk, j)
    built(trk, world, t)
    merge(trk, t, squash)
    assert rated(trk, t, "--sha", rules, "--base", base)[::2] == ("red", True)
    assert "blast-radius" not in needs(trk, t)


# --- audit of 86119ff (ledger-13, -14): containing the PR head is not landing it ---

def landed_world(trk, path, head, merge_sha):
    """A ticket Built at `head` with its merge row at `merge_sha`, on a project over repo `path`."""
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", str(path))
    accept(trk, j)
    t = new_ticket(trk, j)
    built(trk, {"head": head}, t)
    merge(trk, t, merge_sha)
    return t


def test_a_merge_row_at_a_later_revert_commit_anchors_nothing(trk, early):
    """ledger-14 (a): the revert holds the head, as every descendant of the merge does, but it did not land the PR."""
    git(early["path"], "checkout", "-q", "-b", "evil", early["m"])
    git(early["path"], "revert", "-m", "1", "--no-edit", early["m"])
    evil = git(early["path"], "rev-parse", "HEAD")
    t = new_ticket(trk, early["J"])
    built(trk, early, t)
    merge(trk, t, evil)
    assert rated(trk, t, "--sha", evil, "--base", early["rules"])[2] is False
    assert needs(trk, t) == ["blast-radius"]


def test_a_merge_row_at_a_later_merge_of_a_side_branch_after_a_revert_anchors_nothing(trk, early):
    """ledger-17: the later merge has a parent that holds the head (the revert) and one that lacks it (the side
    branch), but the head is not one of its parents, so it did not land the PR; `side...X` is the revert's net diff."""
    git(early["path"], "revert", "-m", "1", "--no-edit", early["m"])
    git(early["path"], "checkout", "-q", "-b", "side", early["rules"])
    commit(early["path"], "docs/s.md")
    git(early["path"], "checkout", "-q", "main")
    git(early["path"], "merge", "-q", "--no-ff", "side", "-m", "merge side")
    later = git(early["path"], "rev-parse", "HEAD")
    t = new_ticket(trk, early["J"])
    built(trk, early, t)
    merge(trk, t, later)
    assert rated(trk, t, "--sha", later, "--base", early["rules"])[2] is False
    assert needs(trk, t) == ["blast-radius"]


def test_a_main_merged_into_the_branch_then_fast_forwarded_still_reads_the_pr_against_main(trk, tmp_path):
    """ledger-14 (b): the landing merge has the head on its FIRST parent; the PR is what it brought over main."""
    r = tmp_path / "F"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "README.md")
    base = commit(r, RULES, "RED src/**\n")
    git(r, "checkout", "-q", "-b", "f")
    head = commit(r, "src/a.py")
    git(r, "checkout", "-q", "main")
    commit(r, "docs/main.md")
    git(r, "checkout", "-q", "f")
    git(r, "merge", "-q", "--no-ff", "main", "-m", "merge main into f")
    landing_merge = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--ff-only", "f")
    git(r, "rm", "-q", "src/a.py")
    git(r, "commit", "-q", "-m", "rm")
    later = git(r, "rev-parse", "HEAD")
    t = landed_world(trk, r, head, landing_merge)
    lane, paths, counts = rated(trk, t, "--sha", later, "--base", base)
    assert (lane, counts) == ("red", True) and "src/a.py" in paths  # the rm leaves no net diff; the PR's path stays
    assert item(trk, t)["lane"] == "red"


def rebase_merged(tmp_path, name, commits):
    """A repo where `commits` (path names) were cut from `base`, main then moved on, and the PR was rebase-merged
    (fast-forward of the rebased commits); the rules file arrives afterwards. Returns the shas."""
    r = tmp_path / name
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    shas = {"path": r, "base": commit(r, "README.md")}
    git(r, "checkout", "-q", "-b", "f")
    for path in commits:
        shas["head"] = commit(r, path)
    git(r, "checkout", "-q", "main")
    commit(r, "docs/main.md")
    git(r, "checkout", "-q", "-b", "rebased", shas["head"])
    git(r, "rebase", "-q", "main")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--ff-only", "rebased")
    shas["m"] = git(r, "rev-parse", "HEAD")
    shas["rules"] = commit(r, RULES, "RED src/**\n")
    return shas


def test_a_two_commit_pr_rebase_merged_after_main_moved_anchors_a_later_rating(trk, tmp_path):
    """ledger-13: the last n commits carry the PR's patches, so the rebase merge is a landing, and the PR's own
    paths come from `merge~n`, not from the last commit alone."""
    s = rebase_merged(tmp_path, "H", ["src/a.py", "docs/b.md"])
    t = landed_world(trk, s["path"], s["head"], s["m"])
    lane, paths, counts = rated(trk, t, "--sha", s["rules"], "--base", s["base"])
    assert (lane, counts) == ("red", True)
    assert paths == ["docs/b.md", "src/a.py"]  # the landing's, not base...rules: main's docs/main.md is another PR's
    assert "blast-radius" not in needs(trk, t)


def test_a_rebase_merged_pr_later_reverted_still_lists_all_of_its_paths(trk, tmp_path):
    s = rebase_merged(tmp_path, "I", ["docs/b.md", "src/a.py"])
    git(s["path"], "revert", "--no-edit", f"{s['m']}~2..{s['m']}")  # both rebased commits
    later = git(s["path"], "rev-parse", "HEAD")
    t = landed_world(trk, s["path"], s["head"], s["m"])
    lane, paths, counts = rated(trk, t, "--sha", later, "--base", s["base"])
    assert (lane, counts) == ("red", True)
    assert "src/a.py" in paths and "docs/b.md" in paths  # the revert leaves no net diff against base


def test_a_s_ours_merge_of_the_head_still_lists_the_heads_own_paths(trk, early):
    """ledger-19: the head is a parent, but `git merge -s ours` brought none of its tree in."""
    git(early["path"], "checkout", "-q", "-B", "main", early["rules"])  # main itself takes the `ours` merge
    git(early["path"], "merge", "-q", "-s", "ours", "--no-ff", early["head"], "-m", "ours")
    fake = git(early["path"], "rev-parse", "HEAD")
    t = new_ticket(trk, early["J"])
    built(trk, early, t)
    merge(trk, t, fake)
    lane, paths, counts = rated(trk, t, "--sha", fake, "--base", early["rules"])
    assert (lane, counts) == ("red", True) and paths == ["src/a.py"]


def test_a_head_that_merged_main_in_lists_nothing_beyond_the_pr_when_main_merges_it(trk, tmp_path):
    """The honest case beside ledger-19: the branch merged main (its head is a merge commit), then main merged it
    no-ff. `pre...head` is only the branch's own change, so nothing extra is listed."""
    r = tmp_path / "P"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "README.md")
    base = commit(r, RULES, "RED src/**\n")
    git(r, "checkout", "-q", "-b", "f")
    commit(r, "src/a.py")
    git(r, "checkout", "-q", "main")
    commit(r, "docs/main.md")
    git(r, "checkout", "-q", "f")
    git(r, "merge", "-q", "--no-ff", "main", "-m", "merge main into f")
    head = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--no-ff", "f", "-m", "merge f")
    landing_merge = git(r, "rev-parse", "HEAD")
    t = landed_world(trk, r, head, landing_merge)
    assert rated(trk, t, "--sha", landing_merge, "--base", base) == ("red", ["docs/main.md", "src/a.py"], True)


def test_a_pr_whose_first_commit_reached_main_alone_still_lists_it_when_the_rest_is_merged_later(trk, tmp_path):
    """ledger-19 variant: main fast-forwarded to c1 only, later merged the head no-ff, and a commit then removed
    c1's file. `pre` is c1, so `pre...merge` and `pre...head` both miss `src/a.py`; `base...head` does not."""
    r = tmp_path / "Q"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "README.md")
    base = commit(r, RULES, "RED src/**\n")
    git(r, "checkout", "-q", "-b", "f")
    commit(r, "src/a.py")
    head = commit(r, "docs/b.md")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--ff-only", "f~1")
    git(r, "merge", "-q", "--no-ff", "f", "-m", "merge f")
    landing_merge = git(r, "rev-parse", "HEAD")
    git(r, "rm", "-q", "src/a.py")
    git(r, "commit", "-q", "-m", "rm")
    later = git(r, "rev-parse", "HEAD")
    t = landed_world(trk, r, head, landing_merge)
    lane, paths, counts = rated(trk, t, "--sha", later, "--base", base)
    assert (lane, counts) == ("red", True) and paths == ["docs/b.md", "src/a.py"]
