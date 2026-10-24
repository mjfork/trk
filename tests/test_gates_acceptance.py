"""Spec 03 (Stage gates + evidence + enforcement) acceptance criteria 1-15, end to end through the `trk` on
PATH, each against a fresh store. AC 15's replay of every kind is tests/test_hardening.py's; AC 16 is the
dogfood upgrade of the live store, recorded on tr-bqx0y.

Every store starts as the spec's setup says: `trk init --me michael`; agents `worker`, `builder` and
`reviewer`; a throwaway git repo R with `main` and a feature branch; program P with one criterion; project
J under P on R, accepted through its gate. Sessions W (builder) and V (reviewer) are distinct. Here the
`trk` fixture runs the installed binary, so the helpers of test_blast_and_verified and test_merged_review
drive it unchanged.
"""

import os
import sqlite3
import subprocess

import pytest

from conftest import Result
from gated import DEST, accept, accepting, closable, commit, git, resolving, section_file
from installed_trk import MISSING, TRK
from test_blast_and_verified import blast, feed, head, item, merge, ok, refusal, write_brief
from test_blast_and_verified import new_ticket as new_unclaimed_ticket
from test_merged_review import FORK, REVIEW, V, W, build_at, ledger_needs, tied_ask
from store_note import without_note


def new_ticket(trk, parent, form="spec-bound", title="T"):
    """A new ticket, claimed by the builder in W when it is ready (its writes need the claim)."""
    t = new_unclaimed_ticket(trk, parent, form, title)
    trk("claim", t, *W)
    return t


@pytest.fixture
def trk(trk_home):
    """Overrides conftest's in-process `trk`: the same Result (exit code, stdout, stderr), from the
    installed binary in a subprocess, so the shared helpers drive it unchanged. Without `trk` on PATH every
    test fails rather than skips, as test_hardening's do."""
    assert TRK, MISSING

    def installed(*argv, stdin=None):
        done = without_note(subprocess.run([TRK, *argv], capture_output=True, text=True, env=os.environ.copy(),
                                           input=stdin, check=False))
        return Result(done.returncode, done.stdout, done.stderr)

    return installed


def reasons(trk, t, *flags):
    """Each evidence row's id → why it does not count (None when it counts)."""
    return {e["id"]: e["reason"] for e in trk("evidence", t, *flags, "--json").json()["evidence"]}


def needs(missing):
    return [m["need"] for m in missing]


def lane(trk, t, *flags):
    """Rate `t` with `blast-radius` and return the lane its evidence row records."""
    return blast(trk, t, *flags)["detail"]["lane"]


def record(trk, t, kind, gate, *flags):
    return ok(trk, "evidence", "add", t, "--kind", kind, "--gate", gate, *flags, "--note", "x", *W)


def pr(trk, t, sha, ref="https://github.com/o/r/pull/1"):
    return record(trk, t, "pr", "built", "--ref", ref, "--sha", sha)


def record_run(trk, t, gate, sha, verdict="pass", criterion=None):
    return record(trk, t, "test-run", gate, "--ref", "run-1", "--sha", sha, "--verdict", verdict,
                  *(("--criterion", criterion) if criterion else ()))


def built(trk, t, sha):
    """pr and a passing test-run at `sha` as builder in W, then advance to Built."""
    build_at(trk, t, sha)
    ok(trk, "advance", t, *W)


def make_repo(path, rules=None):
    """R: `base` on main (with the rules file when given), the feature branch's `head`, a later main commit
    `other` not containing `head`, and `m`, main's merge of the feature branch. The checkout is left on
    `feature` with `m` made, so the rules apply to what the feature branch changed."""
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    shas = {"path": str(path), "base": commit(path, ".trk/blast-radius", rules) if rules else commit(path, ".trk/blast-radius", "# no rules: every change rates green\n")}
    git(path, "checkout", "-q", "-b", "feature")
    shas["head"] = commit(path, "src/a.py")
    git(path, "checkout", "-q", "main")
    shas["other"] = commit(path, "other")
    git(path, "merge", "-q", "--no-ff", "feature", "-m", "merge")
    shas["m"] = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "-q", "feature")
    return shas


@pytest.fixture
def world(trk, tmp_path):
    """The spec's setup on repo R without a rules file."""
    return setup(trk, make_repo(tmp_path / "R"))


def setup(trk, repo):
    ok(trk, "init", "--me", "michael")
    for agent in ("worker", "builder", "reviewer"):
        ok(trk, "actor", "add", agent, "--kind", "agent")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", repo["path"])
    accept(trk, j)
    return {"P": p, "J": j, **repo}


def lane_world(trk, tmp_path):
    """The setup on a repo whose rules are `YELLOW src/**` and `RED contract/**`, with `main` not yet
    holding the feature branch, so `blast-radius` diffs against main's merge-base."""
    r = tmp_path / "L"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, ".trk/blast-radius", "YELLOW src/**\nRED contract/**\n")
    git(r, "checkout", "-q", "-b", "feature")
    return setup(trk, {"path": str(r)})


def lane_ticket(trk, world, name, title="T"):
    """Commit `name` on the feature branch and take a new ticket under J to Built there; (ticket, sha)."""
    sha = commit(world["path"], name)
    t = new_ticket(trk, world["J"], title=title)
    built(trk, t, sha)
    return t, sha


def merge_feature(world):
    git(world["path"], "checkout", "-q", "main")
    git(world["path"], "merge", "-q", "--no-ff", "feature", "-m", "merge")
    sha = git(world["path"], "rev-parse", "HEAD")
    git(world["path"], "checkout", "-q", "feature")
    return sha


# --- AC 1: creation gate, spec accept and spec draft ---

def test_ac1_a_ticket_is_born_framed_through_its_gate_and_follows_its_projects_spec(trk, world):
    k = ok(trk, "new", "project", "K", "--parent", world["P"])
    before = head(trk)
    error = refusal(trk, "new", "work-item", "T", "--form", "spec-bound", "--parent", k)
    assert error["rule"] == "gate-unmet"
    assert [(m["need"], m.get("section")) for m in error["missing"]] == [("section", "what-to-build"),
                                                                        ("criterion", None)]
    assert head(trk) == before

    created = trk("new", "work-item", "T", "--form", "spec-bound", "--parent", k, "--section", "what-to-build=-",
                  "--criterion", "c1", stdin="Build T.\n")
    assert created.code == 0, created.err
    t = created.out.strip()
    assert item(trk, t)["stage"] == "framed"
    rows = feed(trk, before)
    assert [r["change"] for r in rows] == ["item.created", "criterion.added", "stage.changed"]
    assert rows[-1]["data"]["via"] == "create"

    error = refusal(trk, "spec", "accept", k)
    assert error["rule"] == "gate-unmet"
    assert {m["section"] for m in error["missing"] if m["need"] == "section"} == {
        "problem", "solution", "user-stories", "implementation-decisions", "testing-decisions", "out-of-scope"}
    assert "judgment" in needs(error["missing"])

    since = head(trk)
    for argv in accepting(k):
        ok(trk, *argv)
    assert item(trk, t)["stage"] == "specd"
    assert [r["data"]["via"] for r in feed(trk, since) if r["change"] == "stage.changed"] == ["spec-accept"]

    ok(trk, "spec", "draft", k, "--reason", "Reopened the question of units")
    shown = item(trk, t)
    assert shown["stage"] == "framed"
    assert "spec-not-accepted" in shown["why_not"]


# --- AC 2: a self-contained ticket is judged complete ---

def test_ac2_a_self_contained_ticket_is_judged_to_specd_and_claimable(trk, world):
    s = new_ticket(trk, world["J"], form="self-contained")
    assert item(trk, s)["stage"] == "framed"
    ok(trk, "advance", s, "--judgment", "complete repro", *W)
    assert item(trk, s)["stage"] == "specd"
    assert [e["kind"] for e in trk("evidence", s, "--json").json()["evidence"]] == ["judgment"]
    ok(trk, "claim", s, "--session", "W", "--as", "builder")


# --- AC 3: Built ---

def test_ac3_built_needs_a_pr_and_a_passing_test_run_at_the_head(trk, world):
    t1 = new_ticket(trk, world["J"])
    assert item(trk, t1)["stage"] == "specd"
    assert needs(refusal(trk, "advance", t1, *W)["missing"]) == ["pr", "test-run-pass"]
    pr(trk, t1, world["head"])
    record_run(trk, t1, "built", world["base"])
    assert needs(refusal(trk, "advance", t1, *W)["missing"]) == ["test-run-pass"]
    record_run(trk, t1, "built", world["head"])
    ok(trk, "advance", t1, *W)
    assert item(trk, t1)["stage"] == "built"

    refused = refusal(trk, "advance", t1, *W)
    ok(trk, "claim", "release", t1, *W)  # a claimed ticket is off the frontier
    row = next(r for r in trk("ready", "--root", world["P"], "--json").json()["items"] if r["id"] == t1)
    assert row["gate"]["next"] == item(trk, t1)["gate"]["next"] == "merged"
    assert row["gate"]["missing"] == item(trk, t1)["gate"]["missing"] == refused["missing"]


# --- AC 4: Merged and Verified in the GREEN lane ---

def test_ac4_a_green_ticket_merges_unreviewed_and_verifies_at_a_sha_containing_the_merge(trk, world):
    t1 = new_ticket(trk, world["J"])
    built(trk, t1, world["head"])
    assert lane(trk, t1, "--base", world["base"], *W) == "green"
    merge(trk, t1, world["m"])
    ok(trk, "advance", t1, *W)
    assert item(trk, t1)["stage"] == "merged"

    elsewhere = record_run(trk, t1, "verified", world["other"], criterion="ac-1")
    assert reasons(trk, t1)[elsewhere] == "wrong-sha"
    record_run(trk, t1, "verified", world["m"], verdict="fail", criterion="ac-1")
    error = refusal(trk, "advance", t1, "--outcome", "Done.", *W)
    assert [(m["need"], m["latest"]["verdict"]) for m in error["missing"]] == [("criterion-pass", "fail")]

    record_run(trk, t1, "verified", world["m"], criterion="ac-1")
    since = head(trk)
    ok(trk, "advance", t1, "--outcome", "Done.", *W)
    shown = item(trk, t1)
    assert (shown["stage"], shown["status"], shown["resolution"]) == ("verified", "closed", "done")
    assert [r["change"] for r in feed(trk, since)][:2] == ["stage.changed", "item.closed"]


# --- AC 5: tickets close done only at Verified ---

def test_ac5_a_ticket_never_closes_done_directly_but_may_close_wont_do(trk, world):
    t = new_ticket(trk, world["J"])
    assert refusal(trk, "close", t, "--resolution", "done", "--outcome", "Done.")["rule"] == "done-needs-verified"
    s = new_ticket(trk, world["J"], form="self-contained")
    assert item(trk, s)["stage"] == "framed"
    ok(trk, "close", s, "--resolution", "wont-do", "--outcome", "Not needed.")
    assert item(trk, s)["status"] == "closed"


# --- AC 6: criteria are rows ---

def test_ac6_criteria_version_retire_and_refuse_their_generated_section(trk, world):
    t2 = new_ticket(trk, world["J"])
    old = record_run(trk, t2, "verified", world["head"], criterion="ac-1")
    ok(trk, "criterion", "edit", t2, "ac-1", "It works, measured.")
    assert trk("criteria", t2, "--json").json()["criteria"][0]["version"] == 2
    assert reasons(trk, t2)[old] == "old-version"

    ok(trk, "criterion", "add", t2, "It is fast.")
    ok(trk, "criterion", "retire", t2, "ac-2", "--reason", "Folded into ac-1.")
    assert [(c["handle"], c["retired"]) for c in trk("criteria", t2, "--json").json()["criteria"]] == [
        ("ac-1", False), ("ac-2", True)]
    assert refusal(trk, "criterion", "retire", t2, "ac-1", "--reason", "x")["rule"] == "last-criterion"
    error = refusal(trk, "section", "set", t2, "acceptance-criteria", "--file", str(section_file("ac")))
    assert error["rule"] == "generated-section"
    assert trk("evidence", "add", t2, "--kind", "skip", "--gate", "merged", "--note", "x", "--json").code == 2


# --- AC 7: reopen-stage cuts off the gates above it ---

def test_ac7_reopen_stage_leaves_rows_above_the_target_before_reopen(trk, world):
    t3 = new_ticket(trk, world["J"])
    built(trk, t3, world["head"])
    ok(trk, "blast-radius", t3, "--base", world["base"], *W)
    merged = merge(trk, t3, world["m"])
    ok(trk, "advance", t3, *W)
    passed = record_run(trk, t3, "verified", world["m"], criterion="ac-1")
    ok(trk, "reopen-stage", t3, "built", "--reason", "The fix was incomplete", *W)
    assert item(trk, t3)["stage"] == "built"
    weighed = reasons(trk, t3)
    assert (weighed[passed], weighed[merged]) == ("before-reopen", "before-reopen")
    assert list(reasons(trk, t3, "--gate", "built").values()) == [None, None]  # pr and test-run still count


# --- AC 8: skipping Merged for a ticket that changes no repo ---

def test_ac8_a_no_repo_change_ticket_skips_merged_and_others_cannot(trk, world):
    t4 = new_ticket(trk, world["J"])
    pr(trk, t4, world["head"], ref="none")
    record_run(trk, t4, "built", world["head"])
    ok(trk, "advance", t4, *W)
    ok(trk, "skip", t4, "merged", "--reason", "no repo change", *W)
    shown = item(trk, t4)
    assert (shown["stage"], shown["merged_skipped"]) == ("merged", True)
    record_run(trk, t4, "verified", world["head"], criterion="ac-1")
    ok(trk, "advance", t4, "--outcome", "Done.", *W)
    assert item(trk, t4)["stage"] == "verified"

    t = new_ticket(trk, world["J"])
    built(trk, t, world["head"])
    assert refusal(trk, "skip", t, "merged", "--reason", "x", *W)["rule"] == "not-skippable"
    assert refusal(trk, "skip", t, "built", "--reason", "x", *W)["rule"] == "not-skippable"


# --- AC 9: a YELLOW ticket's WRONG row, fixed and re-checked ---

def test_ac9_a_wrong_verdict_blocks_merged_until_its_fix_is_re_checked(trk, tmp_path):
    world = lane_world(trk, tmp_path)
    t5, sha = lane_ticket(trk, world, "src/a.py")
    assert lane(trk, t5, *W) == "yellow"
    assert ok(trk, "ledger", "add", t5, *FORK, *W) == "ledger-1"
    for who in (W, ("--as", "builder", "--session", "X")):
        assert refusal(trk, "ledger", "verdict", t5, "1", "sound", "--note", "fine", *who)["rule"] == "not-independent"
    assert ok(trk, "ledger", "add", t5, *FORK, "--origin", "auditor", *V) == "ledger-2"
    audit = ok(trk, "ledger", "audit", t5, "--note", "Read the diff", *V)
    ok(trk, "ledger", "verdict", t5, "1", "wrong", "--note", "It goes stale", *V)
    ok(trk, "ledger", "verdict", t5, "2", "sound", "--note", "Fine", *V)
    merge(trk, t5, sha)
    error = refusal(trk, "advance", t5, *W)
    assert error["missing"] == [{"need": "ledger-settled", "ledger": "ledger-1"}]

    fix = commit(world["path"], "src/b.py")
    pr(trk, t5, fix)
    record_run(trk, t5, "built", fix)
    ok(trk, "ledger", "settle", t5, "1", "fixed", "--sha", fix, *W)
    assert blast(trk, t5, *W)["sha"] == fix
    ok(trk, "ledger", "verdict", t5, "1", "sound", "--note", "Fixed", "--sha", fix, *V)
    merge(trk, t5, merge_feature(world))
    ok(trk, "advance", t5, *W)
    assert item(trk, t5)["stage"] == "merged"
    assert reasons(trk, t5)[audit] is None  # the audit at <head> carried over to <f>


# --- AC 10: reward-hack flags, open asks and deferral ---

def test_ac10_flags_are_only_fixed_asks_must_be_answered_and_deferral_files_a_ticket(trk, tmp_path):
    world = lane_world(trk, tmp_path)
    t6, _ = lane_ticket(trk, world, "src/a.py")
    flag = ok(trk, "ledger", "add", t6, "--kind", "reward-hack", "--decided", "Regenerated the golden",
              "--why", "The golden changed with the code", "--origin", "auditor", *V)
    ok(trk, "ledger", "verdict", t6, flag, "confirmed", "--note", "Hid a regression", *V)
    a = tied_ask(trk, t6, flag)
    for settlement in (("overruled", "--ask", a), ("accepted", "--ask", a), ("deferred",)):
        assert refusal(trk, "ledger", "settle", t6, flag, *settlement, *V)["rule"] == "reward-hack-final"
    assert lane(trk, t6, *W) == "yellow"
    ok(trk, "ledger", "verdict", t6, flag, "refuted", "--note", "Looks fine now", *V)
    assert ledger_needs(trk, t6) == [("reward-hack-fixed", flag)]  # a REFUTED without a fix clears nothing
    fix = commit(world["path"], "tests/golden.txt")
    pr(trk, t6, fix)
    record_run(trk, t6, "built", fix)
    ok(trk, "ledger", "settle", t6, flag, "fixed", "--sha", fix, *W)
    assert lane(trk, t6, *W) == "yellow"
    assert ledger_needs(trk, t6) == [("reward-hack-fixed", flag)]
    ok(trk, "ledger", "verdict", t6, flag, "refuted", "--note", "Golden now matches", *V)
    assert ledger_needs(trk, t6) == []

    risky = ok(trk, "ledger", "add", t6, *FORK, *W)
    ok(trk, "ledger", "verdict", t6, risky, "risky", "--note", "Invalidate on write", *V)
    b = tied_ask(trk, t6, risky, answer=False)
    error = refusal(trk, "ledger", "settle", t6, risky, "accepted", "--ask", b, *V)
    assert error["rule"] == "settle-needs-answer"

    other = ok(trk, "ledger", "add", t6, *FORK, *W)
    ok(trk, "ledger", "verdict", t6, other, "risky", "--note", "Invalidate on write", *V)
    ok(trk, "ledger", "settle", t6, other, "deferred", *V)
    deferred = next(r for r in trk("ledger", "list", t6, "--json").json()["ledger"] if r["handle"] == other)
    filed = item(trk, deferred["settlement"]["ticket"])
    assert (filed["form"], filed["stage"], filed["parent"]) == ("self-contained", "framed", world["J"])
    assert [(e["type"], e["id"]) for e in filed["edges"]] == [("relates", t6)]


# --- AC 11: the RED lane ---

def test_ac11_a_red_ticket_opens_one_read_ask_and_merges_once_approved(trk, tmp_path):
    world = lane_world(trk, tmp_path)
    t7, sha = lane_ticket(trk, world, "contract/x.json")
    assert lane(trk, t7, "--as", "worker") == "red"
    ok(trk, "blast-radius", t7, "--as", "worker")
    asks = [c for c in trk("tree", t7, "--all", "--json").json()["items"] if c["form"] == "ask"]
    assert [(a["asker"], a["addressee_role"], a["status"]) for a in asks] == [("worker", "engineering", "open")]

    ok(trk, "ledger", "add", t7, *FORK, *W)
    ok(trk, "ledger", "verdict", t7, "1", "sound", "--note", "Fine", *V)
    write_brief(trk, t7)
    ok(trk, "ledger", "audit", t7, "--note", "Read the diff", *V)
    merge(trk, t7, sha)
    ok(trk, "review", "record", t7, "--ref", REVIEW, "--sha", sha, "changes-requested", "--note", "Fix x",
       "--as", "michael")
    assert item(trk, asks[0]["id"])["status"] == "open"
    assert needs(refusal(trk, "advance", t7, *W)["missing"]) == ["human-review"]

    since = head(trk)
    ok(trk, "review", "record", t7, "--ref", REVIEW, "--sha", sha, "approved", "--note", "Good", "--as", "michael")
    answered = [r for r in feed(trk, since) if r["change"] == "ask.answered"]
    assert [(r["item"], r["data"]["proxy"], r["data"]["via"]) for r in answered] == [(asks[0]["id"], True, "evidence")]
    ok(trk, "advance", t7, *W)
    assert item(trk, t7)["stage"] == "merged"


# --- AC 12: repeated guesses file one template ticket ---

def test_ac12_a_repeated_guess_files_one_template_ticket_and_relates_later_repeats(trk, world):
    w = ok(trk, "new", "project", "Workflow skills", "--parent", world["P"])
    ok(trk, "config", "set", "template-parent", w)
    t5, t6, t8 = (new_ticket(trk, world["J"], title=title) for title in ("T5", "T6", "T8"))
    ok(trk, "ledger", "add", t6, *FORK, *W)

    def templates():
        return [(c["id"], c["parent"]) for c in trk("tree", w, "--json").json()["items"]
                if c["title"] == "Ticket template: add a field for units"]

    def related(template):
        return sorted(e["id"] for e in item(trk, template)["edges"] if e["type"] == "relates")

    ok(trk, "ledger", "add", t5, *FORK, "--origin", "auditor", "--repeat-of", f"{t6}:1", "--topic", "units", *V)
    [(template, parent)] = templates()
    assert (parent, related(template)) == (w, sorted([t5, t6]))
    ok(trk, "ledger", "add", t8, *FORK, "--origin", "auditor", "--repeat-of", f"{t6}:1", "--topic", "units", *V)
    assert templates() == [(template, w)]
    assert related(template) == sorted([t5, t6, t8])


# --- AC 13: close done on projects, decisions and draft projects ---

def test_ac13_projects_and_decisions_close_done_through_their_close_gate(trk, world):
    project = ok(trk, "new", "project", "L", "--parent", world["P"])
    accept(trk, project)
    t = new_ticket(trk, project)
    ok(trk, "close", t, "--resolution", "wont-do", "--outcome", "Not needed.")
    error = refusal(trk, "close", project, "--resolution", "done", "--outcome", "Done.")
    assert (error["rule"], [(m["need"], m["criterion"]) for m in error["missing"]]) == (
        "gate-unmet", [("criterion-pass", "ac-1")])
    for argv in closable(project):
        ok(trk, *argv)
    ok(trk, "close", project, "--resolution", "done", "--outcome", "Done.")

    d = ok(trk, "new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", world["J"])
    assert needs(refusal(trk, "close", d, "--resolution", "done", "--outcome", "x")["missing"]) == ["resolution"]
    for argv in resolving(d):
        ok(trk, *argv)
    ok(trk, "close", d, "--resolution", "done", "--outcome", "Decided.")

    draft = ok(trk, "new", "project", "M", "--parent", world["P"])
    ok(trk, "criterion", "add", draft, "It holds.")
    for argv in closable(draft):
        ok(trk, *argv)
    assert needs(refusal(trk, "close", draft, "--resolution", "done", "--outcome", "x")["missing"]) == [
        "spec-accepted"]
    accept(trk, draft)  # adds ac-2, "The spec holds."
    for argv in closable(draft, ("ac-2",)):
        ok(trk, *argv)
    ok(trk, "close", draft, "--resolution", "done", "--outcome", "Done.")

    dropped = ok(trk, "new", "project", "N", "--parent", world["P"])
    ok(trk, "close", dropped, "--resolution", "wont-do", "--outcome", "Dropped.")


# --- AC 14: ready by stage, and its missing lists ---

def test_ac14_ready_keeps_one_stage_and_its_missing_lists_equal_shows(trk, world):
    specd = new_ticket(trk, world["J"], title="specd")
    at_built = new_ticket(trk, world["J"], title="built")
    built(trk, at_built, world["head"])
    at_merged = new_ticket(trk, world["J"], title="merged")
    built(trk, at_merged, world["head"])
    ok(trk, "blast-radius", at_merged, "--base", world["base"], *W)
    merge(trk, at_merged, world["m"])
    ok(trk, "advance", at_merged, *W)
    new_ticket(trk, world["J"], form="self-contained", title="framed")
    for claimed in (specd, at_built, at_merged):  # a claimed ticket is off the frontier
        ok(trk, "claim", "release", claimed, *W)

    assert [r["id"] for r in trk("ready", "--root", world["P"], "--stage", "merged", "--json").json()["items"]] == [
        at_merged]
    rows = trk("ready", "--root", world["P"], "--json").json()["items"]
    assert {specd, at_built, at_merged} <= {r["id"] for r in rows}
    for row in rows:
        assert row["gate"] == item(trk, row["id"])["gate"], row["id"]


# --- AC 15: evidence is append-only. Its replay of every new kind is test_hardening's every_kind scenario,
# through the installed binary: test_the_store_writes_every_core_asks_handoffs_and_gates_change_kind and
# test_ac7_since_zero_replays_every_row_from_init_ascending_and_valid. ---

def test_ac15_deleting_evidence_aborts_on_the_trigger(trk, world, trk_home):
    t = new_ticket(trk, world["J"])
    pr(trk, t, world["head"])
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("delete from evidence")
    finally:
        conn.close()
    assert len(trk("evidence", t, "--json").json()["evidence"]) == 1
