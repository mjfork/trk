"""Evidence rows and advancing to Built (Stage gates): `trk evidence add|retract`, `trk evidence`, weighing,
`trk advance` through Spec'd and Built, and every item JSON's `gate` from the same gate() check.

Sha tests run against a real throwaway git repo R: `base` on main, the feature branch's head `head`, a later
main commit `other` that does not contain `head`, and `m`, main's merge of the feature branch."""

import sqlite3
import subprocess
import sys

import pytest

from gated import DEST, accepting, section_file, ticket


def git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *argv],
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, name, body=None):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if body is not None else f"{name}\n")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


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


def item(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


def head(trk):
    return trk("changes", "--since", "0", "--json").json()["head"]


def feed(trk, since=0):
    return trk("changes", "--since", str(since), "--json").json()["changes"]


def listed(trk, item_id, *flags):
    return trk("evidence", item_id, *flags, "--json").json()["evidence"]


@pytest.fixture
def world(trk, repo):
    """Program P, accepted project J on repo R, and draft project K."""
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", repo["path"])
    k = ok(trk, "new", "project", "K", "--parent", p)
    for argv in accepting(j):
        ok(trk, *argv)
    return {"P": p, "J": j, "K": k, **repo}


def new_ticket(trk, parent, form="spec-bound", title="T"):
    return ok(trk, "new", "work-item", title, "--form", form, "--parent", parent, *ticket(form))


def add(trk, item_id, kind, gate, *flags, note="x"):
    return ok(trk, "evidence", "add", item_id, "--kind", kind, "--gate", gate, *flags, "--note", note)


def pr(trk, t, sha):
    return add(trk, t, "pr", "built", "--ref", "https://github.com/o/r/pull/1", "--sha", sha)


def run(trk, t, gate, sha, verdict="pass", *flags):
    return add(trk, t, "test-run", gate, "--ref", "run-1", "--sha", sha, "--verdict", verdict, *flags)


def refusal(trk, *argv):
    result = trk(*argv, "--json")
    assert result.code == 1, (argv, result.out)
    return result.error()


def built(trk, w, t):
    pr(trk, t, w["head"])
    run(trk, t, "built", w["head"])
    ok(trk, "advance", t)


# --- AC 2 of the spec: a self-contained ticket judged complete ---

def test_a_self_contained_ticket_is_judged_complete_in_one_step_and_then_claimed(trk, world):
    s = new_ticket(trk, world["J"], "self-contained", "S")
    assert item(trk, s)["stage"] == "framed"
    since = head(trk)
    ok(trk, "advance", s, "--judgment", "complete repro")
    assert item(trk, s)["stage"] == "specd"
    rows = feed(trk, since)
    assert [r["change"] for r in rows] == ["evidence.added", "stage.changed"]
    judged = rows[0]["data"]
    assert (judged["kind"], judged["gate"], judged["verdict"], judged["note"]) == ("judgment", "specd", "pass",
                                                                                   "complete repro")
    assert rows[1]["data"] == {"old": "framed", "new": "specd", "via": "advance"}
    assert [(e["kind"], e["counts"]) for e in listed(trk, s)] == [("judgment", True)]
    ok(trk, "claim", s, "--session", "W")


@pytest.mark.parametrize("which", ["spec-bound at specd", "self-contained at specd", "project", "framed spec-bound"])
def test_judgment_is_refused_on_anything_but_a_self_contained_ticket_at_framed(trk, world, which):
    if which == "spec-bound at specd":
        target = new_ticket(trk, world["J"])
    elif which == "self-contained at specd":
        target = new_ticket(trk, world["J"], "self-contained")
        ok(trk, "advance", target, "--judgment", "complete")
    elif which == "project":
        target = world["K"]
    else:
        target = new_ticket(trk, world["K"])
    before = head(trk)
    result = trk("advance", target, "--judgment", "complete", "--json")
    assert result.code == 2
    assert head(trk) == before


def test_a_self_contained_ticket_advanced_without_a_judgment_is_refused_and_a_recorded_one_lets_it(trk, world):
    s = new_ticket(trk, world["J"], "self-contained", "S")
    error = refusal(trk, "advance", s)
    assert (error["rule"], error["item"], error["gate"], error["missing"]) == ("gate-unmet", s, "specd",
                                                                              [{"need": "judgment"}])
    judged = add(trk, s, "judgment", "specd")
    ok(trk, "evidence", "retract", s, judged, "--note", "too hasty")
    assert refusal(trk, "advance", s)["missing"] == [{"need": "judgment"}]
    add(trk, s, "judgment", "specd")
    ok(trk, "advance", s)
    assert item(trk, s)["stage"] == "specd"


def test_advance_refuses_closed_items_and_items_that_are_not_tickets(trk, world):
    t = new_ticket(trk, world["J"])
    ok(trk, "close", t, "--resolution", "wont-do", "--outcome", "Dropped.")
    assert refusal(trk, "advance", t)["rule"] == "closed"
    assert trk("advance", world["J"]).code == 2


# --- AC 3 of the spec: Built at the head sha ---

def test_built_needs_a_pr_and_a_passing_test_run_at_its_head_sha(trk, world):
    t = new_ticket(trk, world["J"])
    since = head(trk)
    error = refusal(trk, "advance", t)
    assert (error["gate"], error["missing"]) == ("built", [{"need": "pr"}, {"need": "test-run-pass", "latest": None}])
    assert head(trk) == since
    pr(trk, t, world["head"])
    elsewhere = run(trk, t, "built", world["other"])
    assert refusal(trk, "advance", t)["missing"] == [
        {"need": "test-run-pass", "latest": None}]
    assert {e["id"]: e["reason"] for e in listed(trk, t)}[elsewhere] == "wrong-sha"
    run(trk, t, "built", world["head"])
    since = head(trk)
    ok(trk, "advance", t)
    assert item(trk, t)["stage"] == "built"
    assert [(r["change"], r["data"]) for r in feed(trk, since)] == [
        ("stage.changed", {"old": "specd", "new": "built", "via": "advance"})]


def test_a_built_tickets_next_gate_is_merged_and_show_ready_and_advance_agree(trk, world):
    t = new_ticket(trk, world["J"])
    built(trk, world, t)
    expected = {"next": "merged", "missing": [{"need": "merge"}, {"need": "blast-radius"}]}
    assert item(trk, t)["gate"] == expected
    ready_rows = {i["id"]: i for i in trk("ready", "--root", world["P"], "--json").json()["items"]}
    assert ready_rows[t]["gate"] == expected
    error = refusal(trk, "advance", t)
    assert (error["gate"], error["missing"]) == (expected["next"], expected["missing"])


def test_a_new_pr_at_another_head_needs_a_test_run_there_for_merged(trk, world):
    t = new_ticket(trk, world["J"])
    built(trk, world, t)
    pr(trk, t, world["m"])
    assert item(trk, t)["stage"] == "built"
    assert item(trk, t)["gate"]["missing"][0] == {
        "need": "test-run-pass", "latest": None}


# --- AC 3 of this ticket: the kinds table ---

@pytest.mark.parametrize("flags, field", [
    (("--kind", "pr", "--gate", "built", "--ref", "r"), "--sha"),
    (("--kind", "pr", "--gate", "built", "--sha", "abcdef1"), "--ref"),
    (("--kind", "test-run", "--gate", "built", "--ref", "r", "--sha", "abcdef1"), "--verdict"),
    (("--kind", "merge", "--gate", "merged", "--ref", "r", "--sha", "abcdef1", "--verdict", "pass"), "--verdict"),
    (("--kind", "human-accept", "--gate", "verified", "--ref", "r", "--sha", "abcdef1", "--verdict", "pass"),
     "--criterion"),
    (("--kind", "test-run", "--gate", "verified", "--ref", "r", "--sha", "abcdef1", "--verdict", "pass"),
     "--criterion"),
    (("--kind", "judgment", "--gate", "built"), "--gate"),
    (("--kind", "judgment", "--gate", "specd", "--verdict", "fail"), "--verdict"),
    (("--kind", "review", "--gate", "built"), "--ref"),
    (("--kind", "review", "--gate", "accept", "--ref", "r"), "--gate"),
    (("--kind", "plan", "--gate", "nowhere", "--ref", "r"), "--gate"),
    (("--kind", "pr", "--gate", "built", "--ref", "r", "--sha", "abc12"), "--sha"),
    (("--kind", "pr", "--gate", "built", "--ref", "r", "--sha", "g" * 7), "--sha"),
    (("--kind", "pr", "--gate", "built", "--ref", "r", "--sha", "a" * 41), "--sha"),
    (("--kind", "bribe", "--gate", "built"), "--kind"),
])
def test_evidence_add_enforces_the_kinds_table(trk, world, flags, field):
    t = new_ticket(trk, world["J"])
    before = head(trk)
    result = trk("evidence", "add", t, *flags, "--note", "x", "--json")
    assert result.code == 2, result.out
    assert result.error()["message"].startswith(field) or field in result.error()["message"]
    assert head(trk) == before


@pytest.mark.parametrize("kind, verb", [("skip", "trk skip"), ("blast-radius", "trk blast-radius"),
                                        ("audit", "trk ledger audit"), ("retract", "trk evidence retract")])
def test_evidence_add_refuses_the_tracker_managed_kinds_naming_their_verb(trk, world, kind, verb):
    t = new_ticket(trk, world["J"])
    result = trk("evidence", "add", t, "--kind", kind, "--gate", "merged", "--ref", "r", "--sha", "abcdef1",
                 "--note", "x", "--json")
    assert result.code == 2
    assert verb in result.error()["message"]


@pytest.mark.parametrize("note", ["", "two\nlines", "x" * 301])
def test_the_note_is_one_line_of_at_most_300_chars(trk, world, note):
    t = new_ticket(trk, world["J"])
    assert trk("evidence", "add", t, "--kind", "plan", "--gate", "built", "--ref", "r", "--note", note).code == 2


def test_the_sha_is_stored_lowercase_and_add_prints_the_id(trk, world):
    t = new_ticket(trk, world["J"])
    evidence_id = pr(trk, t, world["head"].upper())
    assert len(evidence_id) == 6
    (row,) = listed(trk, t)
    assert (row["id"], row["sha"], row["kind"], row["gate"], row["actor"]) == (
        evidence_id, world["head"], "pr", "built", "michael")
    json_row = trk("evidence", "add", t, "--kind", "doc", "--gate", "built", "--ref", "notes.md", "--note", "n",
                   "--json").json()["evidence"]
    assert (json_row["kind"], json_row["counts"], json_row["reason"]) == ("doc", False, "not-weighed")


def test_evidence_add_records_the_criterion_version_and_the_session(trk, world):
    t = new_ticket(trk, world["J"])
    ok(trk, "criterion", "edit", t, "ac-1", "It works well.")
    ok(trk, "claim", t, "--session", "s-9")
    run(trk, t, "verified", world["m"], "pass", "--criterion", "AC-1", "--session", "s-9")
    (row,) = listed(trk, t)
    assert (row["criterion"], row["criterion_version"], row["session"]) == ("ac-1", 2, "s-9")
    assert trk("evidence", "add", t, "--kind", "plan", "--gate", "built", "--ref", "r", "--criterion", "ac-7",
               "--note", "x").code == 3


def test_a_retired_criterion_takes_no_evidence(trk, world):
    t = new_ticket(trk, world["J"])
    ok(trk, "criterion", "add", t, "Second.")
    ok(trk, "criterion", "retire", t, "ac-1", "--reason", "Gone.")
    assert trk("evidence", "add", t, "--kind", "plan", "--gate", "built", "--ref", "r", "--criterion", "ac-1",
               "--note", "x").code == 2


def test_a_closed_item_takes_only_review_and_doc_evidence(trk, world):
    t = new_ticket(trk, world["J"])
    recorded = pr(trk, t, world["head"])
    ok(trk, "close", t, "--resolution", "wont-do", "--outcome", "Dropped.")
    error = refusal(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "r", "--sha",
                    world["head"], "--verdict", "pass", "--note", "x")
    assert (error["rule"], error["item"]) == ("closed", t)
    add(trk, t, "review", "built", "--ref", "https://review/1", "--verdict", "fail")
    add(trk, t, "doc", "built", "--ref", "notes.md")
    ok(trk, "evidence", "retract", t, recorded, "--note", "Wrong PR.")


def open_ask(trk, t):
    return ok(trk, "ask", t, "--to", "product", "Which?", "--question", f"@{section_file('question')}", "--as", "worker")


def test_asks_take_no_evidence(trk, world):
    t = new_ticket(trk, world["J"])
    a = open_ask(trk, t)
    assert trk("evidence", "add", a, "--kind", "review", "--gate", "close", "--ref", "r", "--note", "x").code == 2


def test_evidence_retract_writes_a_retract_row_for_the_rows_gate(trk, world):
    t = new_ticket(trk, world["J"])
    recorded = pr(trk, t, world["head"])
    since = head(trk)
    retraction = ok(trk, "evidence", "retract", t, recorded.upper(), "--note", "Wrong PR.")
    (row,) = feed(trk, since)
    assert row["change"] == "evidence.added"
    assert {k: row["data"][k] for k in ("id", "kind", "gate", "ref", "note")} == {
        "id": retraction, "kind": "retract", "gate": "built", "ref": recorded, "note": "Wrong PR."}
    assert [(e["kind"], e["counts"], e["reason"]) for e in listed(trk, t)] == [
        ("pr", False, "retracted"), ("retract", False, "not-weighed")]


def test_evidence_retract_refuses_bad_retractions(trk, world, trk_home):
    t = new_ticket(trk, world["J"])
    u = new_ticket(trk, world["J"], title="U")
    recorded = pr(trk, t, world["head"])
    retraction = ok(trk, "evidence", "retract", t, recorded, "--note", "Wrong PR.")
    others = pr(trk, u, world["head"])
    conn = sqlite3.connect(trk_home / "store.db")
    with conn:
        conn.execute("INSERT INTO evidence (id, item_id, gate, kind, note, actor, seq, created_at)"
                     " VALUES ('skp000', ?, 'merged', 'skip', 'no repo change', 'michael', 1, 'x')", (t,))
    conn.close()
    before = head(trk)
    for target in (retraction, others, recorded, "skp000"):
        error = refusal(trk, "evidence", "retract", t, target, "--note", "why")
        assert (error["rule"], error["item"]) == ("bad-retract", t), target
    assert trk("evidence", "retract", t, "zzzzzz", "--note", "why").code == 3
    assert head(trk) == before


# --- AC 4 of this ticket: listing and weighing ---

def test_evidence_lists_rows_in_seq_order_and_filters_by_gate(trk, world):
    t = new_ticket(trk, world["J"])
    first = pr(trk, t, world["head"])
    second = run(trk, t, "verified", world["m"], "pass", "--criterion", "ac-1")
    third = run(trk, t, "built", world["head"])
    rows = listed(trk, t)
    assert [r["id"] for r in rows] == [first, second, third]
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
    assert [r["id"] for r in listed(trk, t, "--gate", "built")] == [first, third]
    lines = trk("evidence", t).out.splitlines()
    assert lines[0].startswith(f"{first}  built  pr  {world['head'][:7]}  https://github.com/o/r/pull/1  counts")


def test_a_flaky_fail_then_a_pass_passes(trk, world):
    t = new_ticket(trk, world["J"])
    pr(trk, t, world["head"])
    run(trk, t, "built", world["head"], "fail")
    error = refusal(trk, "advance", t)
    assert error["missing"][0]["need"] == "test-run-pass"
    assert error["missing"][0]["latest"]["verdict"] == "fail"
    run(trk, t, "built", world["head"])
    ok(trk, "advance", t)


def test_a_pass_then_a_fail_blocks(trk, world):
    t = new_ticket(trk, world["J"])
    pr(trk, t, world["head"])
    run(trk, t, "built", world["head"])
    failed = run(trk, t, "built", world["head"], "fail")
    assert refusal(trk, "advance", t)["missing"] == [
        {"need": "test-run-pass", "latest": {"id": failed, "kind": "test-run", "sha": world["head"], "verdict": "fail"}}]


def test_a_retracted_pass_blocks(trk, world):
    t = new_ticket(trk, world["J"])
    pr(trk, t, world["head"])
    passed = run(trk, t, "built", world["head"])
    ok(trk, "evidence", "retract", t, passed, "--note", "Ran the wrong suite.")
    assert refusal(trk, "advance", t)["missing"] == [{"need": "test-run-pass", "latest": None}]


def test_a_retracted_pr_leaves_no_head_sha(trk, world):
    t = new_ticket(trk, world["J"])
    recorded = pr(trk, t, world["head"])
    run(trk, t, "built", world["head"])
    ok(trk, "evidence", "retract", t, recorded, "--note", "Wrong PR.")
    assert refusal(trk, "advance", t)["missing"] == [{"need": "pr"}]


def test_evidence_on_an_edited_criterions_old_version_stops_counting(trk, world):
    t = new_ticket(trk, world["J"])
    add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", world["m"])
    old = run(trk, t, "verified", world["m"], "pass", "--criterion", "ac-1")
    assert item(trk, t)["criteria"][0]["status"] == "pass"
    ok(trk, "criterion", "edit", t, "ac-1", "It works well.")
    assert {e["id"]: e["reason"] for e in listed(trk, t)}[old] == "old-version"
    assert item(trk, t)["criteria"][0]["status"] == "none"


def test_verified_evidence_at_a_sha_not_containing_the_merge_sha_is_wrong_sha(trk, world):
    t = new_ticket(trk, world["J"])
    add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", world["m"])
    stale = run(trk, t, "verified", world["other"], "pass", "--criterion", "ac-1")
    contained = run(trk, t, "verified", world["m"], "fail", "--criterion", "ac-1")
    reasons = {e["id"]: e["reason"] for e in listed(trk, t)}
    assert (reasons[stale], reasons[contained]) == ("wrong-sha", None)
    assert item(trk, t)["criteria"][0]["status"] == "fail"


def test_short_and_full_shas_of_one_commit_compare_equal(trk, world):
    t = new_ticket(trk, world["J"])
    pr(trk, t, world["head"][:7])
    run(trk, t, "built", world["head"])
    ok(trk, "advance", t)


def test_an_unknown_sha_in_a_checkout_is_sha_unverifiable(trk, world):
    t = new_ticket(trk, world["J"])
    pr(trk, t, world["head"])
    unknown = run(trk, t, "built", "deadbeef")
    assert {e["id"]: e["reason"] for e in listed(trk, t)}[unknown] == "sha-unverifiable"
    (need,) = refusal(trk, "advance", t)["missing"]
    assert need == {"need": "sha-unverifiable", "evidence": unknown, "sha": "deadbeef", "checkout": world["path"],
                    "hint": f"deadbeef is unknown to {world['path']}: run `git fetch` there, then retry"}


def test_without_a_checkout_shas_compare_by_prefix_and_ancestry_is_unverifiable(trk, world, tmp_path):
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    n = ok(trk, "new", "project", "N", "--parent", world["P"], "--repo", str(plain))
    for argv in accepting(n):
        ok(trk, *argv)
    t = new_ticket(trk, n)
    pr(trk, t, "abcdef1234")
    elsewhere = run(trk, t, "built", "abcdef9")
    same = run(trk, t, "built", "abcdef1234567")
    add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", "1234567")
    unplaced = run(trk, t, "verified", "7654321", "pass", "--criterion", "ac-1")
    reasons = {e["id"]: e["reason"] for e in listed(trk, t)}
    assert (reasons[elsewhere], reasons[same], reasons[unplaced]) == ("wrong-sha", None, "sha-unverifiable")
    ok(trk, "advance", t)


def test_a_ticket_uses_its_own_checkout_before_its_projects(trk, world, tmp_path):
    other = tmp_path / "R2"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    own_head = commit(other, "x")
    t = new_ticket(trk, world["J"])
    ok(trk, "edit", t, "--add-repo", str(tmp_path / "missing"), "--add-repo", str(other))
    pr(trk, t, own_head[:7])
    run(trk, t, "built", own_head)
    ok(trk, "advance", t)


# --- AC 5 of this ticket: the gate table ---

def _create(trk, parent, form, skip_section=False, skip_criterion=False):
    argv = ["new", "work-item", "T", "--form", form, "--parent", parent]
    flags = list(ticket(form, skip=("what-to-build", "current-behaviour") if skip_section else ()))
    if skip_criterion:
        flags = flags[:-2]
    return trk(*argv, *flags, "--json")


@pytest.mark.parametrize("form, skip, need", [
    ("spec-bound", "section", {"need": "section", "section": "what-to-build"}),
    ("spec-bound", "criterion", {"need": "criterion"}),
    ("self-contained", "section", {"need": "section", "section": "current-behaviour"}),
    ("self-contained", "criterion", {"need": "criterion"}),
])
def test_framed_missing_exactly_one_piece(trk, world, form, skip, need):
    result = _create(trk, world["K"], form, skip_section=skip == "section", skip_criterion=skip == "criterion")
    assert result.code == 1
    assert (result.error()["gate"], result.error()["missing"]) == ("framed", [need])


def _specd_missing_spec(trk, w):
    return new_ticket(trk, w["K"]), [{"need": "spec-accepted", "project": w["K"]}]


def _specd_missing_judgment(trk, w):
    return new_ticket(trk, w["J"], "self-contained"), [{"need": "judgment"}]


def _built_missing_pr(trk, w):
    t = new_ticket(trk, w["J"])
    run(trk, t, "built", w["head"])
    return t, [{"need": "pr"}]


def _built_missing_test_run(trk, w):
    t = new_ticket(trk, w["J"])
    pr(trk, t, w["head"])
    return t, [{"need": "test-run-pass", "latest": None}]


def _merged_missing_merge(trk, w):
    t = new_ticket(trk, w["J"])
    built(trk, w, t)
    return t, [{"need": "merge"}, {"need": "blast-radius"}]


def _built_missing_pr_after_reopen_stage(trk, w):
    t = new_ticket(trk, w["J"])
    built(trk, w, t)
    ok(trk, "reopen-stage", t, "specd", "--reason", "Rebuild it.")
    return t, [{"need": "pr"}, {"need": "test-run-pass", "latest": None}]


def _merged_missing_merge_after_reopen_stage(trk, w):
    t = new_ticket(trk, w["J"])
    built(trk, w, t)
    ok(trk, "blast-radius", t, "--base", w["base"])
    add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", w["m"])
    ok(trk, "advance", t)
    ok(trk, "reopen-stage", t, "built", "--reason", "Merge it again.")
    return t, [{"need": "merge"}, {"need": "blast-radius"}]


def _verified_missing_a_pass_after_skip(trk, w):
    t = new_ticket(trk, w["J"])
    add(trk, t, "pr", "built", "--ref", "none", "--sha", w["head"])
    run(trk, t, "built", w["head"])
    ok(trk, "advance", t)
    ok(trk, "skip", t, "merged", "--reason", "No repo change.")
    return t, [{"need": "criterion-pass", "criterion": "ac-1", "version": 1, "latest": None}]


ONE_PIECE = [_specd_missing_spec, _specd_missing_judgment, _built_missing_pr, _built_missing_test_run,
             _merged_missing_merge, _built_missing_pr_after_reopen_stage, _merged_missing_merge_after_reopen_stage,
             _verified_missing_a_pass_after_skip]


def advance_refusal(trk, t):
    """The refusal of advancing `t`, with the outcome advancing into verified needs."""
    outcome = ("--outcome", "Done.") if item(trk, t)["stage"] == "merged" else ()
    return refusal(trk, "advance", t, *outcome)


@pytest.mark.parametrize("setup", ONE_PIECE)
def test_a_ticket_missing_exactly_one_piece_is_refused_with_exactly_that_need(trk, world, setup):
    t, missing = setup(trk, world)
    error = advance_refusal(trk, t)
    assert error["missing"] == missing
    assert item(trk, t)["gate"] == {"next": error["gate"], "missing": missing}


def test_every_items_show_gate_equals_its_refusal(trk, world):
    for setup in ONE_PIECE:
        setup(trk, world)
    new_ticket(trk, world["J"], title="Ready to build")
    tickets = [i for i in trk("tree", "--json").json()["items"] if i["stage"]]
    assert len(tickets) == len(ONE_PIECE) + 1
    for shown in tickets:
        error = advance_refusal(trk, shown["id"])
        assert shown["gate"] == {"next": error["gate"], "missing": error["missing"]}, shown["id"]


# --- the item JSON's gate, ready --stage and tree ---

def test_every_item_json_carries_its_next_gate_merged_skipped_and_lane(trk, world):
    t = new_ticket(trk, world["J"])
    d = ok(trk, "new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", world["J"])
    a = open_ask(trk, t)
    gates = {name: item(trk, i)["gate"]["next"] for name, i in
             (("P", world["P"]), ("J", world["J"]), ("K", world["K"]), ("T", t), ("D", d), ("A", a))}
    assert gates == {"P": "close", "J": "close", "K": "accept", "T": "built", "D": "close", "A": None}
    assert item(trk, world["K"])["gate"]["missing"][-1] == {"need": "judgment"}
    shown = item(trk, t)
    assert (shown["merged_skipped"], shown["lane"]) == (False, None)
    ok(trk, "close", d, "--resolution", "wont-do", "--outcome", "Dropped.")
    assert item(trk, d)["gate"] == {"next": None, "missing": []}
    waiting = trk("waiting", "--all", "--json").json()["asks"]
    assert waiting[0]["gate"] == {"next": None, "missing": []}
    u = new_ticket(trk, world["J"], title="U")
    claimed = trk("claim", u, "--session", "W", "--json").json()["claimed"]
    assert claimed["gate"]["next"] == "built"


def test_ready_stage_keeps_tickets_at_that_stage(trk, world):
    specd = new_ticket(trk, world["J"], title="Spec'd")
    at_built = new_ticket(trk, world["J"], title="Built")
    built(trk, world, at_built)
    s = new_ticket(trk, world["J"], "self-contained", "S")
    assert [i["id"] for i in trk("ready", "--root", world["P"], "--stage", "built", "--json").json()["items"]] == [at_built]
    assert [i["id"] for i in trk("ready", "--root", world["P"], "--stage", "specd", "--json").json()["items"]] == [specd]
    assert [i["id"] for i in trk("ready", "--root", world["P"], "--stage", "framed", "--json").json()["items"]] == [s]
    assert trk("ready", "--stage", "done").code == 2


def test_tree_prints_each_tickets_stage_after_its_form(trk, world):
    t = new_ticket(trk, world["J"])
    line = next(line for line in trk("tree").out.splitlines() if line.strip().startswith(t))
    assert line.strip().startswith(f"{t}  work-item/spec-bound [specd]  T")


# --- AC 6 of this ticket: criterion lines carry their evidence status ---

def test_generated_criterion_lines_and_criteria_show_the_latest_counting_rows_status(trk, world):
    t = new_ticket(trk, world["J"])
    ok(trk, "criterion", "add", t, "It is fast.")
    add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", world["m"])
    passed = run(trk, t, "verified", world["m"], "pass", "--criterion", "ac-1")
    failed = add(trk, t, "webtester-run", "verified", "--ref", "wt-1", "--sha", world["m"], "--verdict", "fail",
                 "--criterion", "ac-2")
    m7 = world["m"][:7]
    assert trk("criteria", t).out.splitlines() == [
        f"- [x] ac-1 It works. — pass (test-run {passed}, {m7})",
        f"- [ ] ac-2 It is fast. — fail (webtester-run {failed}, {m7})",
    ]
    listed_criteria = trk("criteria", t, "--json").json()["criteria"]
    assert [(c["status"], c["latest"]) for c in listed_criteria] == [
        ("pass", {"id": passed, "kind": "test-run", "sha": world["m"], "verdict": "pass"}),
        ("fail", {"id": failed, "kind": "webtester-run", "sha": world["m"], "verdict": "fail"}),
    ]
    assert trk("section", "get", t, "acceptance-criteria").out == trk("criteria", t).out


def test_a_projects_criteria_weigh_close_evidence(trk, world):
    ok(trk, "criterion", "add", world["J"], "The project delivers.")
    judged = add(trk, world["J"], "judgment", "close", "--criterion", "ac-2", note="Delivered by T1 and T2.")
    add(trk, world["J"], "test-run", "close", "--ref", "e2e", "--verdict", "fail",
        "--criterion", "ac-2")
    assert "- [ ] ac-2 The project delivers. — fail (test-run " in trk("criteria", world["J"]).out
    assert [e["counts"] for e in listed(trk, world["J"], "--gate", "close")] == [True, True]
    assert judged in trk("evidence", world["J"]).out


def test_claim_renew_loads_none_of_the_gates_modules(trk, world):
    probe = ("import sys; from trk.cli import main; main(['claim', 'renew', '--session', 's-1']);"
             " print(sorted(m for m in ('trk.criteria', 'trk.evidence', 'trk.gitrepo', 'trk.stages') if m in sys.modules))")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]"


def test_a_verified_row_is_unverifiable_until_there_is_a_merge_sha(trk, world):
    t = new_ticket(trk, world["J"])
    early = run(trk, t, "verified", world["m"], "pass", "--criterion", "ac-1")
    assert {e["id"]: e["reason"] for e in listed(trk, t)}[early] == "sha-unverifiable"
    assert item(trk, t)["criteria"][0]["status"] == "none"
    add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", world["m"])
    assert item(trk, t)["criteria"][0]["status"] == "pass"


def test_a_retracted_review_row_says_retracted_and_an_unknown_gate_filter_is_a_usage_error(trk, world):
    t = new_ticket(trk, world["J"])
    review = add(trk, t, "review", "built", "--ref", "https://review/1")
    ok(trk, "evidence", "retract", t, review, "--note", "Wrong PR.")
    assert [e["reason"] for e in listed(trk, t)] == ["retracted", "not-weighed"]
    assert trk("evidence", t, "--gate", "biult").code == 2
