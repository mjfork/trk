"""Skip and reopen-stage (Stage gates): `trk skip <ticket> merged` for a no-repo-change ticket, `trk
reopen-stage` moving an open ticket back with the counting cut-off (`before-reopen`), and `trk reopen` of a
ticket closed at Verified taking `--to-stage` and `--reason`.

Every test runs against a real throwaway git repo R: `base` on main (its `.trk/blast-radius` rates
`risky.py` YELLOW), the feature branch's head `head`, `m`, main's merge of the feature branch, and `risky`,
a commit on another branch off `base` adding `risky.py`."""

import pytest

from gated import DEST, accept, commit, git, ticket, walk_to_verified


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "R"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    shas = {"path": str(r), "base": commit(r, ".trk/blast-radius", "YELLOW risky.py\n")}
    git(r, "checkout", "-q", "-b", "feature")
    shas["head"] = commit(r, "feature")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--no-ff", "feature", "-m", "merge")
    shas["m"] = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "-b", "risky", shas["base"])
    shas["risky"] = commit(r, "risky.py")
    git(r, "checkout", "-q", "main")
    return shas


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def item(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


def head(trk):
    return trk("changes", "--since", "0", "--json").json()["head"]


def feed(trk, since=0):
    return trk("changes", "--since", str(since), "--json").json()["changes"]


def reasons(trk, item_id):
    """{evidence id: reason} for every row, None for one that counts."""
    return {e["id"]: e["reason"] for e in trk("evidence", item_id, "--json").json()["evidence"]}


def refusal(trk, *argv, code=1):
    before = head(trk)
    result = trk(*argv, "--json")
    assert result.code == code, (argv, result.out, result.err)
    assert head(trk) == before, argv
    return result.error()


@pytest.fixture
def world(trk, repo):
    """Program P and accepted project J on repo R."""
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", repo["path"])
    accept(trk, j)
    return {"P": p, "J": j, **repo}


def new_ticket(trk, parent, title="T"):
    return ok(trk, "new", "work-item", title, "--form", "spec-bound", "--parent", parent, *ticket("spec-bound"))


def add(trk, item_id, kind, gate, *flags, note="x"):
    return ok(trk, "evidence", "add", item_id, "--kind", kind, "--gate", gate, *flags, "--note", note)


def pr(trk, t, sha, ref="https://github.com/o/r/pull/1"):
    return add(trk, t, "pr", "built", "--ref", ref, "--sha", sha)


def run(trk, t, gate, sha, *flags, verdict="pass"):
    return add(trk, t, "test-run", gate, "--ref", "run-1", "--sha", sha, "--verdict", verdict, *flags)


def built(trk, t, sha, ref="https://github.com/o/r/pull/1"):
    ids = pr(trk, t, sha, ref), run(trk, t, "built", sha)
    ok(trk, "advance", t)
    return ids


def merged(trk, w, t):
    """AC 4's walk: Built at `head`, a GREEN rating against `base`, a merge at `m`; returns the row ids."""
    ids = {"pr": None, "test-run": None}
    ids["pr"], ids["test-run"] = built(trk, t, w["head"])
    ids["blast-radius"] = trk("blast-radius", t, "--base", w["base"], "--json").json()["evidence"]["id"]
    ids["merge"] = add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", w["m"])
    ok(trk, "advance", t)
    return ids


def no_repo_change(trk, w, title="T4"):
    """A ticket at Built whose `pr` row has ref `none`, at `head`."""
    t = new_ticket(trk, w["J"], title)
    built(trk, t, w["head"], ref="none")
    return t


# --- AC 7 of the spec: reopen-stage and the cut-off ---

def test_ac7_reopen_stage_to_built_cuts_off_merged_and_verified_but_built_still_counts(trk, world):
    t3 = new_ticket(trk, world["J"], "T3")
    ids = merged(trk, world, t3)
    passed = run(trk, t3, "verified", world["m"], "--criterion", "ac-1")
    assert reasons(trk, t3)[passed] is None
    since = head(trk)
    assert ok(trk, "reopen-stage", t3, "built", "--reason", "Verification found a regression.") == t3
    rows = feed(trk, since)
    assert [r["change"] for r in rows] == ["stage.changed"]
    assert rows[0]["data"] == {"old": "merged", "new": "built", "via": "reopen-stage",
                               "reason": "Verification found a regression."}
    weighed = reasons(trk, t3)
    assert (weighed[passed], weighed[ids["merge"]], weighed[ids["blast-radius"]]) == ("before-reopen",) * 3
    assert (weighed[ids["pr"]], weighed[ids["test-run"]]) == (None, None)
    shown = item(trk, t3)
    assert (shown["stage"], shown["lane"]) == ("built", None)
    assert shown["gate"] == {"next": "merged", "missing": [{"need": "merge"}, {"need": "blast-radius"}]}


def test_after_a_reopen_stage_the_ticket_needs_fresh_merged_and_verified_rows(trk, world):
    t = new_ticket(trk, world["J"])
    merged(trk, world, t)
    run(trk, t, "verified", world["m"], "--criterion", "ac-1")
    ok(trk, "reopen-stage", t, "built", "--reason", "Back.")
    ok(trk, "blast-radius", t, "--base", world["base"])
    add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", world["m"])
    ok(trk, "advance", t)
    error = refusal(trk, "advance", t, "--outcome", "Works.")
    assert error["missing"] == [{"need": "criterion-pass", "criterion": "ac-1", "version": 1, "latest": None}]
    run(trk, t, "verified", world["m"], "--criterion", "ac-1")
    ok(trk, "advance", t, "--outcome", "Works.")
    assert item(trk, t)["resolution"] == "done"


def test_reopen_stage_writes_one_row_per_stage_crossed_and_cuts_off_built_too(trk, world):
    t = new_ticket(trk, world["J"])
    ids = merged(trk, world, t)
    since = head(trk)
    ok(trk, "reopen-stage", t, "specd", "--reason", "Wrong approach.")
    assert [(r["data"]["old"], r["data"]["new"], r["data"]["via"]) for r in feed(trk, since)] == [
        ("merged", "built", "reopen-stage"), ("built", "specd", "reopen-stage")]
    weighed = reasons(trk, t)
    assert {weighed[i] for i in ids.values()} == {"before-reopen"}
    assert item(trk, t)["gate"] == {"next": "built", "missing": [{"need": "pr"},
                                                                 {"need": "test-run-pass", "latest": None}]}


def test_reopen_stage_to_framed_cuts_off_a_self_contained_tickets_judgment(trk, world):
    t = ok(trk, "new", "work-item", "S", "--form", "self-contained", "--parent", world["J"], *ticket("self-contained"))
    ok(trk, "advance", t, "--judgment", "Complete.")
    ok(trk, "reopen-stage", t, "framed", "--reason", "Not complete after all.")
    assert item(trk, t)["gate"] == {"next": "specd", "missing": [{"need": "judgment"}]}
    assert set(reasons(trk, t).values()) == {"before-reopen"}


# --- ac-3: what reopen-stage refuses ---

@pytest.mark.parametrize("target", ["built", "merged", "verified"])
def test_reopen_stage_to_the_current_or_a_later_stage_is_not_backward(trk, world, target):
    t = new_ticket(trk, world["J"])
    built(trk, t, world["head"])
    error = refusal(trk, "reopen-stage", t, target, "--reason", "r")
    assert (error["rule"], error["item"]) == ("not-backward", t)


def test_reopen_stage_on_a_closed_ticket_is_closed(trk, world):
    t = new_ticket(trk, world["J"])
    built(trk, t, world["head"])
    ok(trk, "close", t, "--resolution", "wont-do", "--outcome", "Dropped.")
    assert refusal(trk, "reopen-stage", t, "specd", "--reason", "r")["rule"] == "closed"


@pytest.mark.parametrize("argv", [("specd",), ("specd", "--reason", " "), ("specd", "--reason", "two\nlines"),
                                  ("bogus", "--reason", "r")])
def test_reopen_stage_needs_a_stage_and_a_one_line_reason(trk, world, argv):
    t = new_ticket(trk, world["J"])
    built(trk, t, world["head"])
    refusal(trk, "reopen-stage", t, *argv, code=2)


def test_reopen_stage_takes_tickets_only(trk, world):
    refusal(trk, "reopen-stage", world["J"], "framed", "--reason", "r", code=2)


def test_reopen_stage_clears_merged_skipped_at_built_or_earlier(trk, world):
    t = no_repo_change(trk, world)
    ok(trk, "skip", t, "merged", "--reason", "No repo change.")
    ok(trk, "reopen-stage", t, "built", "--reason", "Back.")
    shown = item(trk, t)
    assert (shown["stage"], shown["merged_skipped"]) == ("built", False)
    assert shown["gate"]["missing"] == [{"need": "merge"}, {"need": "blast-radius"}]


# --- AC 8 of the spec: skip ---

def test_ac8_skip_moves_a_no_repo_change_ticket_to_merged_and_verified_checks_the_head(trk, world):
    t4 = no_repo_change(trk, world)
    since = head(trk)
    assert ok(trk, "skip", t4, "merged", "--reason", "no repo change") == t4
    rows = feed(trk, since)
    assert [r["change"] for r in rows] == ["evidence.added", "stage.changed"]
    assert {k: rows[0]["data"][k] for k in ("kind", "gate", "note", "sha", "verdict")} == {
        "kind": "skip", "gate": "merged", "note": "no repo change", "sha": None, "verdict": None}
    assert rows[1]["data"] == {"old": "built", "new": "merged", "via": "skip", "reason": "no repo change"}
    shown = item(trk, t4)
    assert (shown["stage"], shown["merged_skipped"]) == ("merged", True)
    assert shown["gate"]["next"] == "verified"
    assert shown["gate"]["missing"] == [{"need": "criterion-pass", "criterion": "ac-1", "version": 1,
                                         "latest": None}]
    run(trk, t4, "verified", world["head"], "--criterion", "ac-1")
    ok(trk, "advance", t4, "--outcome", "Nothing to merge; it holds.")
    shown = item(trk, t4)
    assert (shown["stage"], shown["status"], shown["resolution"]) == ("verified", "closed", "done")


def test_a_real_pr_ref_is_not_skippable(trk, world):
    t = new_ticket(trk, world["J"])
    built(trk, t, world["head"])
    error = refusal(trk, "skip", t, "merged", "--reason", "r")
    assert (error["rule"], error["item"]) == ("not-skippable", t)


@pytest.mark.parametrize("stage", ["framed", "specd", "built", "verified", "bogus"])
def test_skip_names_merged_only(trk, world, stage):
    t = no_repo_change(trk, world)
    assert refusal(trk, "skip", t, stage, "--reason", "r")["rule"] == "not-skippable"


def test_skip_is_allowed_at_built_only(trk, world):
    t = new_ticket(trk, world["J"])
    pr(trk, t, world["head"], ref="none")
    assert refusal(trk, "skip", t, "merged", "--reason", "r")["rule"] == "not-skippable"
    t = no_repo_change(trk, world, "T5")
    ok(trk, "skip", t, "merged", "--reason", "r")
    assert refusal(trk, "skip", t, "merged", "--reason", "again")["rule"] == "not-skippable"


def test_a_merge_row_makes_a_ticket_not_skippable(trk, world):
    t = no_repo_change(trk, world)
    add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", world["m"])
    assert refusal(trk, "skip", t, "merged", "--reason", "r")["rule"] == "not-skippable"


def test_a_yellow_rating_makes_a_ticket_not_skippable_but_a_green_one_does_not(trk, world):
    t = new_ticket(trk, world["J"])
    built(trk, t, world["risky"], ref="none")
    assert ok(trk, "blast-radius", t, "--base", world["base"]) == "yellow"
    assert refusal(trk, "skip", t, "merged", "--reason", "r")["rule"] == "not-skippable"
    green = no_repo_change(trk, world, "T5")
    assert ok(trk, "blast-radius", green, "--base", world["base"]) == "green"
    ok(trk, "skip", green, "merged", "--reason", "r")


def test_the_latest_pr_row_decides_whether_a_ticket_is_skippable(trk, world):
    t = no_repo_change(trk, world)
    pr(trk, t, world["head"])
    run(trk, t, "built", world["head"])
    assert refusal(trk, "skip", t, "merged", "--reason", "r")["rule"] == "not-skippable"


def test_skip_needs_built_to_still_hold_at_the_head_sha(trk, world):
    t = no_repo_change(trk, world)
    pr(trk, t, world["m"], ref="none")
    error = refusal(trk, "skip", t, "merged", "--reason", "r")
    assert (error["rule"], error["gate"]) == ("gate-unmet", "merged")
    assert error["missing"] == [{"need": "test-run-pass", "latest": None}]
    run(trk, t, "built", world["m"])
    ok(trk, "skip", t, "merged", "--reason", "r")


@pytest.mark.parametrize("argv", [("merged",), ("merged", "--reason", ""), ("merged", "--reason", "two\nlines")])
def test_skip_needs_a_one_line_reason(trk, world, argv):
    t = no_repo_change(trk, world)
    refusal(trk, "skip", t, *argv, code=2)


def test_skip_refuses_a_closed_ticket_and_items_that_are_not_tickets(trk, world):
    t = no_repo_change(trk, world)
    ok(trk, "close", t, "--resolution", "wont-do", "--outcome", "Dropped.")
    assert refusal(trk, "skip", t, "merged", "--reason", "r")["rule"] == "closed"
    refusal(trk, "skip", world["J"], "merged", "--reason", "r", code=2)


def test_a_skip_row_is_not_written_by_evidence_add_nor_retracted(trk, world):
    t = no_repo_change(trk, world)
    ok(trk, "skip", t, "merged", "--reason", "r")
    skip_row = next(e for e in trk("evidence", t, "--json").json()["evidence"] if e["kind"] == "skip")
    assert refusal(trk, "evidence", "retract", t, skip_row["id"], "--note", "n")["rule"] == "bad-retract"


# --- ac-4: reopening a ticket closed at Verified ---

@pytest.fixture
def verified(trk, world):
    t = new_ticket(trk, world["J"])
    merged(trk, world, t)
    passed = run(trk, t, "verified", world["m"], "--criterion", "ac-1")
    ok(trk, "advance", t, "--outcome", "Works.")
    return t, passed


@pytest.mark.parametrize("flags", [(), ("--reason", "r"), ("--to-stage", "built"), ("--to-stage", "verified",
                                   "--reason", "r"), ("--to-stage", "bogus", "--reason", "r")])
def test_reopening_a_ticket_closed_at_verified_needs_an_earlier_to_stage_and_a_reason(trk, verified, flags):
    t, _ = verified
    refusal(trk, "reopen", t, *flags, code=2)


def test_reopening_a_verified_ticket_moves_it_back_with_the_cut_off(trk, world, verified):
    t, passed = verified
    since = head(trk)
    assert ok(trk, "reopen", t, "--to-stage", "built", "--reason", "It regressed.") == t
    rows = feed(trk, since)
    assert [r["change"] for r in rows] == ["item.reopened", "stage.changed", "stage.changed"]
    assert [r["data"] for r in rows[1:]] == [
        {"old": "verified", "new": "merged", "via": "reopen", "reason": "It regressed."},
        {"old": "merged", "new": "built", "via": "reopen", "reason": "It regressed."}]
    shown = item(trk, t)
    assert (shown["status"], shown["stage"], shown["resolution"]) == ("open", "built", None)
    assert reasons(trk, t)[passed] == "before-reopen"
    assert shown["gate"]["missing"] == [{"need": "merge"}, {"need": "blast-radius"}]
    ok(trk, "blast-radius", t, "--base", world["base"])
    add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", world["m"])
    ok(trk, "advance", t)
    run(trk, t, "verified", world["m"], "--criterion", "ac-1")
    ok(trk, "advance", t, "--outcome", "Works again.")
    assert (item(trk, t)["resolution"], item(trk, t)["outcome"]) == ("done", "Works again.")


def test_reopening_a_verified_ticket_to_merged_keeps_its_merge(trk, world, verified):
    t, passed = verified
    ok(trk, "reopen", t, "--to-stage", "merged", "--reason", "Check again.")
    assert item(trk, t)["gate"]["missing"] == [{"need": "criterion-pass", "criterion": "ac-1", "version": 1,
                                                "latest": None}]
    assert reasons(trk, t)[passed] == "before-reopen"


def test_reopening_a_ticket_closed_wont_do_leaves_its_stage_and_takes_neither_flag(trk, world):
    t = new_ticket(trk, world["J"])
    built(trk, t, world["head"])
    ok(trk, "close", t, "--resolution", "wont-do", "--outcome", "Dropped.")
    refusal(trk, "reopen", t, "--to-stage", "specd", "--reason", "r", code=2)
    since = head(trk)
    ok(trk, "reopen", t)
    assert [r["change"] for r in feed(trk, since)] == ["item.reopened"]
    assert (item(trk, t)["stage"], item(trk, t)["status"]) == ("built", "open")


def test_to_stage_goes_with_tickets_only(trk, world):
    d = ok(trk, "new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", world["J"])
    ok(trk, "close", d, "--resolution", "wont-do", "--outcome", "Dropped.")
    refusal(trk, "reopen", d, "--to-stage", "built", code=2)


# --- ac-5: the walked helper still reaches Verified after a skip and a reopen ---

def test_the_shared_helper_walks_a_reopened_ticket_back_to_verified(trk, world):
    t = new_ticket(trk, world["J"])
    walk = {"sha": world["head"], "repo": world["path"]}
    walk_to_verified(lambda *argv: ok(trk, *argv), t, **walk)
    ok(trk, "reopen", t, "--to-stage", "specd", "--reason", "Start over.")
    walk_to_verified(lambda *argv: ok(trk, *argv), t, outcome="Done again.", **walk)
    assert item(trk, t)["outcome"] == "Done again."
