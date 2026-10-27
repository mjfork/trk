"""UP-0018: a findings item (a review, an audit, a sweep, a diagnosis) reaches done with no pr row, merge or
blast-radius: every criterion passes on a run or an independent reader's doc verdict, then a judgment from a session
that wrote none of the evidence. And `close --resolution moot --by-ask` closes work an answered ask made moot."""

import pytest

from gated import DEST, accepting, section_file, ticket

W = "11111111-1111-1111-1111-111111111111"   # the builder
R = "33333333-3333-3333-3333-333333333333"   # the independent reader
S = "44444444-4444-4444-4444-444444444444"   # the spec's judge


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def refusal(trk, *argv, code=1):
    result = trk(*argv, "--json")
    assert result.code == code, (argv, result.out, result.err)
    return result.error()


def as_(session, actor="worker"):
    return ("--as", actor, "--session", session)


@pytest.fixture
def world(trk):
    ok(trk, "init", "--me", "michael")
    for agent in ("auditor",):   # `worker` comes with the store (trk init)
        ok(trk, "actor", "add", agent, "--kind", "agent")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p)
    for argv in accepting(j):
        ok(trk, *argv)
    return j


def findings(trk, project, criteria=("Every reader is listed with its writer's key.", "Each drop names its cause.")):
    flags = [f for c in criteria for f in ("--criterion", c)]
    return ok(trk, "new", "work-item", "Audit the readers", "--form", "findings", "--parent", project,
              "--section", f"question=@{section_file('question', 'Which readers drop keys?')}", *flags)


def doc(t, n, *extra):
    return ("evidence", "add", t, "--kind", "doc", "--gate", "reviewed", "--criterion", f"ac-{n}",
            "--ref", "tmp/findings.md", "--note", "the findings file", *extra)


def show(trk, t):
    return trk("show", t, "--json").json()["item"]


def to_specd(trk, t):
    ok(trk, "advance", t, "--judgment", "The question and criteria are clear.", *as_(S))
    ok(trk, "claim", t, *as_(W))


def test_a_findings_item_reaches_done_with_no_pr_row(trk, world):
    t = findings(trk, world)
    assert show(trk, t)["stage"] == "framed"
    to_specd(trk, t)
    for n in (1, 2):
        ok(trk, *doc(t, n), *as_(W))
        ok(trk, *doc(t, n, "--verdict", "pass"), *as_(R, "auditor"))
    assert show(trk, t)["gate"] == {"next": "reviewed", "missing": [{"need": "judgment"}]}
    ok(trk, "evidence", "add", t, "--kind", "judgment", "--gate", "reviewed", "--note", "Both hold.", *as_(R, "auditor"))
    ok(trk, "advance", t, *as_(W))
    assert show(trk, t)["stage"] == "reviewed"
    ok(trk, "advance", t, "--outcome", "Two readers drop keys; both filed.", *as_(W))
    item = show(trk, t)
    assert (item["status"], item["resolution"], item["stage"]) == ("closed", "done", "verified")
    assert not [e for e in trk("evidence", t, "--json").json()["evidence"] if e["kind"] in ("pr", "merge")]


def test_reviewed_waits_for_every_criterion(trk, world):
    t = findings(trk, world)
    to_specd(trk, t)
    ok(trk, *doc(t, 1), *as_(W))
    ok(trk, *doc(t, 1, "--verdict", "pass"), *as_(R, "auditor"))
    ok(trk, "evidence", "add", t, "--kind", "judgment", "--gate", "reviewed", "--note", "j", *as_(R, "auditor"))
    error = refusal(trk, "advance", t, *as_(W))
    assert error["rule"] == "gate-unmet" and [m["criterion"] for m in error["missing"]] == ["ac-2"]


def test_a_failing_verdict_does_not_count(trk, world):
    t = findings(trk, world, criteria=("It holds.",))
    to_specd(trk, t)
    ok(trk, *doc(t, 1), *as_(W))
    ok(trk, *doc(t, 1, "--verdict", "fail"), *as_(R, "auditor"))
    assert show(trk, t)["gate"]["missing"][0]["need"] == "criterion-pass"


def test_a_run_counts_as_a_criterion_pass(trk, world):
    t = findings(trk, world, criteria=("The sweep script finds them all.",))
    to_specd(trk, t)
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "reviewed", "--criterion", "ac-1", "--ref", "ci/1",
       "--verdict", "pass", "--note", "sweep run", *as_(W))
    ok(trk, "evidence", "add", t, "--kind", "judgment", "--gate", "reviewed", "--note", "j", *as_(R, "auditor"))
    ok(trk, "advance", t, *as_(W))


def test_the_evidence_writer_may_not_give_its_verdict(trk, world):
    t = findings(trk, world)
    to_specd(trk, t)
    ok(trk, *doc(t, 1), *as_(W))
    assert refusal(trk, *doc(t, 1, "--verdict", "pass"), *as_(W))["rule"] == "not-independent"
    ok(trk, *doc(t, 2, "--verdict", "pass"), *as_(W))  # it wrote no evidence for ac-2


def test_the_evidence_writer_may_not_judge(trk, world):
    t = findings(trk, world)
    to_specd(trk, t)
    ok(trk, *doc(t, 1), *as_(W))
    judge = ("evidence", "add", t, "--kind", "judgment", "--gate", "reviewed", "--note", "j")
    assert refusal(trk, *judge, *as_(W))["rule"] == "not-independent"
    assert refusal(trk, *judge, "--as", "auditor")["rule"] == "session-required"
    ok(trk, *judge, "--as", "michael")  # a person at a terminal may judge


def test_a_doc_verdict_belongs_to_a_findings_items_reviewed_gate(trk, world):
    code = ok(trk, "new", "work-item", "T", "--form", "spec-bound", "--parent", world, *ticket("spec-bound"))
    error = trk("evidence", "add", code, "--kind", "doc", "--gate", "built", "--ref", "r", "--note", "n",
                "--verdict", "pass", *as_(R, "auditor"))
    assert error.code == 2 and "findings" in error.err
    t = findings(trk, world)
    no_criterion = trk("evidence", "add", t, "--kind", "doc", "--gate", "reviewed", "--ref", "r", "--note", "n",
                       "--verdict", "pass", *as_(R, "auditor"))
    assert no_criterion.code == 2


def test_a_findings_item_takes_no_pr_merge_or_blast_radius(trk, world):
    t = findings(trk, world)
    to_specd(trk, t)
    pr = trk("evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", "none", "--sha", "abcdef1",
             "--note", "pr", *as_(W))
    assert pr.code == 2 and "not a gate" in pr.err
    assert trk("blast-radius", t, *as_(W)).code == 2


def test_a_code_ticket_still_refuses_done_without_a_pr_row(trk, world):
    code = ok(trk, "new", "work-item", "T", "--form", "spec-bound", "--parent", world, *ticket("spec-bound"))
    assert refusal(trk, "close", code, "--resolution", "done", "--outcome", "o")["rule"] == "done-needs-verified"
    t = findings(trk, world)
    error = refusal(trk, "close", t, "--resolution", "done", "--outcome", "o")
    assert error["rule"] == "done-needs-verified" and "from reviewed" in error["message"]


def test_reopen_stage_moves_a_findings_item_back_and_cuts_its_evidence(trk, world):
    t = findings(trk, world, criteria=("It holds.",))
    to_specd(trk, t)
    ok(trk, *doc(t, 1), *as_(W))
    ok(trk, *doc(t, 1, "--verdict", "pass"), *as_(R, "auditor"))
    ok(trk, "evidence", "add", t, "--kind", "judgment", "--gate", "reviewed", "--note", "j", *as_(R, "auditor"))
    ok(trk, "advance", t, *as_(W))
    assert refusal(trk, "reopen-stage", t, "built", "--reason", "r", *as_(W), code=2)
    ok(trk, "reopen-stage", t, "specd", "--reason", "a reader found a gap", *as_(W))
    assert show(trk, t)["stage"] == "specd"
    assert {m["need"] for m in show(trk, t)["gate"]["missing"]} == {"criterion-pass", "judgment"}


# --- moot: closed citing the answered ask that made the item unnecessary ---

def ask_on(trk, item):
    return ok(trk, "ask", item, "--to", "engineering", "Still needed?",
              "--question", f"@{section_file('question', 'Is this still needed?')}", "--as", "worker")


def test_an_item_made_moot_by_an_answered_ask_closes_moot_citing_it(trk, world):
    t = findings(trk, world)
    a = ask_on(trk, world)
    refused = refusal(trk, "close", t, "--resolution", "moot", "--by-ask", a, "--outcome", "o")
    assert refused["rule"] == "ask-not-answered"
    ok(trk, "answer", a, "--outcome", "No: the importer was removed.", "--answer",
       f"@{section_file('answer', 'Removed.')}", "--as", "michael")
    ok(trk, "close", t, "--resolution", "moot", "--by-ask", a, "--outcome", "The importer it audits was removed.")
    item = show(trk, t)
    assert (item["status"], item["resolution"]) == ("closed", "moot")
    closed = [r for r in trk("changes", "--since", "0", "--json").json()["changes"]
              if r["change"] == "item.closed" and r["item"] == t]
    assert closed[-1]["data"]["by_ask"] == a


def test_moot_needs_an_ask_and_names_only_an_ask(trk, world):
    t = findings(trk, world)
    other = findings(trk, world)
    assert trk("close", t, "--resolution", "moot", "--outcome", "o").code == 2
    assert trk("close", t, "--resolution", "wont-do", "--by-ask", other, "--outcome", "o").code == 2
    assert refusal(trk, "close", t, "--resolution", "moot", "--by-ask", other, "--outcome", "o")["rule"] == \
        "ask-not-answered"
