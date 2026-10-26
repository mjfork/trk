"""A builder's write needs the writer's claim on the ticket (tr-zn1dk): evidence add (pr, test-run, merge),
advance past Framed, blast-radius and a builder ledger add are refused with the refusal `claim` itself would
give, and a ledger row can be withdrawn with a reason (`ledger withdraw`).

Sessions: W builds, X is another. A write with no session (a person's terminal) is not checked."""

import pytest

from gated import DEST, accepting, ticket

W = "11111111-1111-1111-1111-111111111111"
X = "22222222-2222-2222-2222-222222222222"
HEAD = "abcdef1234567"


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def refusal(trk, *argv, code=1):
    result = trk(*argv, "--json")
    assert result.code == code, (argv, result.out, result.err)
    return result.error()


@pytest.fixture
def world(trk):
    ok(trk, "init", "--me", "michael")
    for agent in ("worker", "auditor"):
        ok(trk, "actor", "add", agent, "--kind", "agent")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p)
    for argv in accepting(j):
        ok(trk, *argv)
    return j


def new_ticket(trk, project, title="T"):
    return ok(trk, "new", "work-item", title, "--form", "spec-bound", "--parent", project, *ticket("spec-bound"))


def as_(session, actor="worker"):
    return ("--as", actor, "--session", session)


WRITES = {
    "pr": lambda t: ("evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", "none", "--sha", HEAD,
                     "--note", "pr"),
    "test-run": lambda t: ("evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "none",
                           "--sha", HEAD, "--verdict", "pass", "--note", "tests"),
    "ledger": lambda t: ("ledger", "add", t, "--kind", "fork", "--decided", "d", "--alternative", "a", "--why", "w"),
}


@pytest.mark.parametrize("write", sorted(WRITES))
def test_unclaimed_ready_ticket_is_refused_not_claimed(trk, world, write):
    t = new_ticket(trk, world)
    error = refusal(trk, *WRITES[write](t), *as_(W))
    assert error["rule"] == "not-claimed"
    assert t in error["message"]
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES[write](t), *as_(W))


@pytest.mark.parametrize("write", sorted(WRITES))
def test_blocked_ticket_gets_the_claims_refusal(trk, world, write):
    """The 2026-10-22 incident: the claim was refused not-ready, and the writes went through anyway."""
    t, blocker = new_ticket(trk, world), new_ticket(trk, world, "B")
    ok(trk, "link", blocker, "blocks", t)
    assert refusal(trk, "claim", t, *as_(W))["rule"] == "not-ready"
    error = refusal(trk, *WRITES[write](t), *as_(W))
    assert error["rule"] == "not-ready"
    assert "blocked-by" in error["message"]
    assert trk("ledger", "list", t, "--json").json()["ledger"] == []


@pytest.mark.parametrize("write", sorted(WRITES))
def test_another_sessions_claim_gets_claimed(trk, world, write):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(X))
    assert refusal(trk, *WRITES[write](t), *as_(W), code=4)["rule"] == "claimed"


def test_advance_blast_radius_and_merge_need_the_claim(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["pr"](t), *as_(W))
    ok(trk, *WRITES["test-run"](t), *as_(W))
    ok(trk, "claim", "release", t, *as_(W))
    ok(trk, "claim", t, *as_(X))
    assert refusal(trk, "advance", t, *as_(W), code=4)["rule"] == "claimed"
    assert refusal(trk, "blast-radius", t, *as_(W), code=4)["rule"] == "claimed"
    merge = ("evidence", "add", t, "--kind", "merge", "--gate", "merged", "--ref", "r", "--sha", HEAD, "--note", "m")
    assert refusal(trk, *merge, *as_(W), code=4)["rule"] == "claimed"
    ok(trk, "claim", "release", t, *as_(X))
    assert refusal(trk, "advance", t, *as_(W))["rule"] == "not-claimed"
    assert refusal(trk, "blast-radius", t, *as_(W))["rule"] == "not-claimed"
    assert refusal(trk, *merge, *as_(W))["rule"] == "not-claimed"
    ok(trk, "claim", t, *as_(W))
    ok(trk, "advance", t, *as_(W))


def test_review_evidence_and_an_auditors_row_need_no_claim(trk, world):
    t = new_ticket(trk, world)
    # judgment and review evidence are not the builder's; an auditor's row needs independence, not a claim
    ok(trk, "evidence", "add", t, "--kind", "review", "--gate", "built", "--ref", "r", "--note", "n", *as_(X))
    ok(trk, "ledger", "add", t, "--kind", "fork", "--decided", "d", "--alternative", "a", "--why", "w",
       "--origin", "auditor", *as_(X, "auditor"))


def test_a_write_with_no_session_is_not_checked(trk, world):
    t = new_ticket(trk, world)
    ok(trk, *WRITES["ledger"](t), "--as", "michael")


def test_a_lapsed_claim_is_gone(trk, world, monkeypatch):
    t = new_ticket(trk, world)
    monkeypatch.setenv("TRK_NOW", "2026-10-24T10:00:00.000Z")
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    monkeypatch.setenv("TRK_NOW", "2026-10-24T12:00:00.000Z")
    assert refusal(trk, *WRITES["ledger"](t), *as_(W))["rule"] == "not-claimed"


# --- ledger withdraw ---

def rows(trk, t):
    return trk("ledger", "list", t, "--json").json()["ledger"]


def test_withdraw_marks_the_row_and_the_gate_ignores_it(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    assert ok(trk, *WRITES["ledger"](t), *as_(W)) == "ledger-1"
    assert rows(trk, t)[0]["withdrawn"] is None
    assert ok(trk, "ledger", "withdraw", t, "1", "--reason", "written without a claim", "--as", "michael") == "ledger-1"
    row = rows(trk, t)[0]
    assert row["withdrawn"]["reason"] == "written without a claim"
    assert row["withdrawn"]["actor"] == "michael"
    assert "withdrawn: written without a claim" in ok(trk, "ledger", "list", t)
    change = [c for c in trk("changes", "--since", "0", "--json").json()["changes"] if c["change"] == "ledger.withdrawn"]
    assert change[0]["data"]["ledger"] == "ledger-1" and change[0]["data"]["reason"] == "written without a claim"
    assert trk("show", t, "--json").json()["item"]["ledger_rows"][0]["withdrawn"]["reason"]


def test_withdraw_refusals(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    assert refusal(trk, "ledger", "withdraw", t, "2", "--reason", "x", "--as", "michael", code=3)["rule"]
    assert trk("ledger", "withdraw", t, "1", "--as", "michael").code == 2  # --reason is required
    ok(trk, "ledger", "withdraw", t, "1", "--reason", "r", "--as", "michael")
    assert refusal(trk, "ledger", "withdraw", t, "1", "--reason", "r", "--as", "michael")["rule"] == "ledger-withdrawn"
    assert refusal(trk, "ledger", "verdict", t, "1", "sound", "--note", "n", *as_(X, "auditor"))["rule"] \
        == "ledger-withdrawn"


def test_a_judged_row_cannot_be_withdrawn(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, "ledger", "verdict", t, "1", "sound", "--note", "n", *as_(X, "auditor"))
    assert refusal(trk, "ledger", "withdraw", t, "1", "--reason", "r", "--as", "michael")["rule"] == "ledger-judged"


def test_the_builder_cannot_withdraw_an_auditors_row(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, "ledger", "add", t, "--kind", "fork", "--decided", "d", "--alternative", "a", "--why", "w",
       "--origin", "auditor", *as_(X, "auditor"))
    assert refusal(trk, "ledger", "withdraw", t, "2", "--reason", "r", *as_(W))["rule"] == "not-independent"
    ok(trk, "ledger", "withdraw", t, "2", "--reason", "r", *as_(X, "auditor"))


# --- an open ask on the ticket never stops its own builder (audit ledger-8) ---

def ask_on(trk, tmp_path, t):
    from gated import section_file

    return ok(trk, "ask", t, "--to", "engineering", "Which way?", "--question", f"@{section_file('question', 'Which?')}",
              "--as", "worker")


def test_an_open_ask_on_the_ticket_does_not_stop_a_fresh_session_claiming_or_writing(trk, world, tmp_path):
    """The RED-read ask is open on the ticket while its builder's lease lapses; a fresh session (after /clear)
    must claim and write again."""
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ask_on(trk, tmp_path, t)
    assert "open-ask" in " ".join(trk("show", t, "--json").json()["item"]["why_not"])
    ok(trk, "claim", "release", t, *as_(W))
    assert refusal(trk, *WRITES["ledger"](t), *as_(X))["rule"] == "not-claimed"  # the claim would be granted
    ok(trk, "claim", t, *as_(X))
    ok(trk, *WRITES["ledger"](t), *as_(X))
    ok(trk, *WRITES["pr"](t), *as_(X))


def test_the_same_session_reclaims_after_its_lease_lapsed_beside_an_ask_on_the_ticket(trk, world, tmp_path, monkeypatch):
    """UP-0010's exact shape: the builder's lease lapses (not released) while an ask is opened on the claimed ticket
    itself; the same session's re-claim is granted, not refused `not-ready: open-ask`."""
    t = new_ticket(trk, world)
    monkeypatch.setenv("TRK_NOW", "2026-10-24T10:00:00.000Z")
    ok(trk, "claim", t, *as_(W))
    ask_on(trk, tmp_path, t)
    monkeypatch.setenv("TRK_NOW", "2026-10-24T12:00:00.000Z")       # past the lease: lapsed, never released
    assert refusal(trk, *WRITES["ledger"](t), *as_(W))["rule"] == "not-claimed"
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))


def test_a_blocker_still_refuses_the_claim_beside_an_open_ask(trk, world, tmp_path):
    t, blocker = new_ticket(trk, world), new_ticket(trk, world, "B")
    ok(trk, "link", blocker, "blocks", t)
    ask_on(trk, tmp_path, t)
    error = refusal(trk, "claim", t, *as_(W))
    assert error["rule"] == "not-ready" and "blocked-by" in error["message"] and "open-ask" not in error["message"]


# --- the page lists a withdrawn row, marked (audit ledger-9) ---

def test_the_page_lists_a_withdrawn_row_marked_with_its_reason(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, "ledger", "withdraw", t, "1", "--reason", "written twice", "--as", "michael")
    page = trk("pages", "render", t, "--out", "-").out
    row = page[page.index('data-comment-id="ledger-1"'):]
    row = row[:row.index("</tr>")]
    assert "withdrawn" in row and "written twice" in row


# --- who may withdraw what (audit ledger-10) ---

def test_the_builder_withdraws_only_before_any_audit(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, "ledger", "withdraw", t, "1", "--reason", "mistake", *as_(W))
    ok(trk, *WRITES["pr"](t), *as_(W))  # an audit is recorded at the head
    ok(trk, "ledger", "audit", t, "--note", "audited", *as_(X, "auditor"))
    error = refusal(trk, "ledger", "withdraw", t, "2", "--reason", "dodge", *as_(W))
    assert error["rule"] == "ledger-audited"


def test_a_verdict_also_ends_the_builders_window(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    for _ in range(3):
        ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, "ledger", "verdict", t, "1", "sound", "--note", "n", *as_(X, "auditor"))
    assert refusal(trk, "ledger", "withdraw", t, "2", "--reason", "dodge", *as_(W))["rule"] == "ledger-audited"


def test_nobody_withdraws_a_row_another_row_corrects(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, *WRITES["ledger"](t), "--refers-to", "1", *as_(W))
    for who in (as_(W), ("--as", "michael")):
        assert refusal(trk, "ledger", "withdraw", t, "1", "--reason", "r", *who)["rule"] == "ledger-cited"
    ok(trk, "ledger", "withdraw", t, "2", "--reason", "r", *as_(W))


def test_an_auditor_withdraws_only_its_own_rows(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    add = ("ledger", "add", t, "--kind", "fork", "--decided", "d", "--alternative", "a", "--why", "w",
           "--origin", "auditor")
    ok(trk, *add, *as_(X, "auditor"))
    Y = "33333333-3333-3333-3333-333333333333"
    ok(trk, *add, *as_(Y, "auditor"))
    assert refusal(trk, "ledger", "withdraw", t, "1", "--reason", "r", *as_(X, "auditor"))["rule"] == "not-own-row"
    assert refusal(trk, "ledger", "withdraw", t, "3", "--reason", "r", *as_(X, "auditor"))["rule"] == "not-own-row"
    ok(trk, "ledger", "withdraw", t, "2", "--reason", "mine", *as_(X, "auditor"))


def test_the_driver_withdraws_any_unjudged_row_after_the_audit(trk, world):
    """A session that never built or audited the ticket: any row, builder's or auditor's, until it is judged."""
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, "ledger", "add", t, "--kind", "fork", "--decided", "d", "--alternative", "a", "--why", "w",
       "--origin", "auditor", *as_(X, "auditor"))
    ok(trk, *WRITES["pr"](t), *as_(W))  # an audit is recorded at the head
    ok(trk, "ledger", "audit", t, "--note", "audited", *as_(X, "auditor"))
    D = "44444444-4444-4444-4444-444444444444"
    ok(trk, "ledger", "withdraw", t, "1", "--reason", "stale", *as_(D))


# --- re-audit ledger-15 and ledger-16: the claimant is a builder; only an auditor's own session withdraws its row ---

def audited_after_built_by_w(trk, world):
    """W builds the ticket to Built with ledger-1, then releases; X audits it."""
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, *WRITES["pr"](t), *as_(W))
    ok(trk, *WRITES["test-run"](t), *as_(W))
    ok(trk, "advance", t, *as_(W))
    ok(trk, "claim", "release", t, *as_(W))
    return t


def test_a_fresh_session_holding_the_claim_at_built_is_a_builder_for_withdraw(trk, world):
    t = audited_after_built_by_w(trk, world)
    Z = "55555555-5555-5555-5555-555555555555"
    ok(trk, "claim", t, *as_(Z))
    ok(trk, "ledger", "audit", t, "--note", "audited", *as_(X, "auditor"))
    assert refusal(trk, "ledger", "withdraw", t, "1", "--reason", "dodge", *as_(Z))["rule"] == "ledger-audited"


def test_a_session_that_once_claimed_stays_a_builder_for_withdraw(trk, world):
    t = audited_after_built_by_w(trk, world)
    Z = "55555555-5555-5555-5555-555555555555"
    ok(trk, "claim", t, *as_(Z))
    ok(trk, "claim", "release", t, *as_(Z))
    ok(trk, "ledger", "audit", t, "--note", "audited", *as_(X, "auditor"))
    assert refusal(trk, "ledger", "withdraw", t, "1", "--reason", "dodge", *as_(Z))["rule"] == "ledger-audited"
    assert refusal(trk, "ledger", "withdraw", t, "1", "--reason", "dodge", *as_(Z, "auditor"))["rule"] \
        == "ledger-audited"


@pytest.mark.parametrize("kind", ("reward-hack", "fork"))
def test_only_the_auditor_or_a_person_withdraws_an_auditors_row(trk, world, kind):
    t = audited_after_built_by_w(trk, world)
    ok(trk, "ledger", "add", t, "--kind", kind, "--decided", "d", "--alternative", "a", "--why", "w",
       "--origin", "auditor", *as_(X, "auditor"))
    ok(trk, "ledger", "add", t, "--kind", kind, "--decided", "d", "--alternative", "a", "--why", "w",
       "--origin", "auditor", *as_(X, "auditor"))
    D = "44444444-4444-4444-4444-444444444444"
    for who in (as_(D), as_(W), as_(D, "auditor")):
        assert refusal(trk, "ledger", "withdraw", t, "2", "--reason", "r", *who)["rule"] in (
            "not-own-row", "not-independent")
    ok(trk, "ledger", "withdraw", t, "2", "--reason", "mine", *as_(X, "auditor"))
    ok(trk, "ledger", "withdraw", t, "3", "--reason", "a person", "--as", "michael")


def test_a_builder_withdraws_only_rows_its_own_session_wrote(trk, world):
    """Before any audit: W adds ledger-1 and releases; Z claims and may not withdraw W's row (verifier ledger-10)."""
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_(W))
    ok(trk, *WRITES["ledger"](t), *as_(W))
    ok(trk, "claim", "release", t, *as_(W))
    Z = "55555555-5555-5555-5555-555555555555"
    ok(trk, "claim", t, *as_(Z))
    ok(trk, *WRITES["ledger"](t), *as_(Z))
    assert refusal(trk, "ledger", "withdraw", t, "1", "--reason", "not mine", *as_(Z))["rule"] == "not-own-row"
    ok(trk, "ledger", "withdraw", t, "2", "--reason", "mine", *as_(Z))
    ok(trk, "ledger", "withdraw", t, "1", "--reason", "a person", "--as", "michael")


# --- a spec judgment comes from a session that wrote none of the spec (UP-0008) ---

def self_contained(trk, world, session):
    from gated import ticket

    return ok(trk, "new", "work-item", "S", "--form", "self-contained", "--parent", world, *ticket("self-contained"),
              *as_(session))


def test_the_session_that_wrote_a_ticket_cannot_judge_it_complete(trk, world):
    s = self_contained(trk, world, W)
    error = refusal(trk, "advance", s, "--judgment", "Complete.", *as_(W))
    assert error["rule"] == "not-independent" and W in error["message"]
    ok(trk, "advance", s, "--judgment", "Complete.", *as_(X))


def test_an_edit_of_the_spec_makes_that_session_a_writer_too(trk, world):
    from gated import section_file

    s = self_contained(trk, world, W)
    ok(trk, "section", "set", s, "desired-behaviour", "--file", str(section_file("desired-behaviour", "Edited.\n")),
       *as_(X))
    assert refusal(trk, "advance", s, "--judgment", "Complete.", *as_(X))["rule"] == "not-independent"


def test_an_agent_judging_a_spec_needs_a_session_and_a_person_does_not(trk, world):
    s = self_contained(trk, world, W)
    assert refusal(trk, "advance", s, "--judgment", "Complete.", "--as", "worker")["rule"] == "session-required"
    ok(trk, "advance", s, "--judgment", "Complete.", "--as", "michael")


def test_a_project_spec_is_accepted_only_by_a_session_that_wrote_none_of_it(trk):
    from gated import PROJECT_SECTIONS, section_file

    ok(trk, "init", "--me", "michael")
    ok(trk, "actor", "add", "worker", "--kind", "agent")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, *DEST)
    for handle in PROJECT_SECTIONS:
        ok(trk, "section", "set", j, handle, "--file", str(section_file(handle)), *as_(W))
    accept = ("evidence", "add", j, "--kind", "judgment", "--gate", "accept", "--verdict", "pass", "--note", "ok")
    assert refusal(trk, *accept, *as_(W))["rule"] == "not-independent"
    ok(trk, *accept, *as_(X))
