"""Ledger rows, verdicts and independence (Stage gates, Ledger): `trk ledger add|list|verdict|audit|promote`,
`ledger settle … fixed`, `trk ask … --ledger`, the `shared-agent` setting and the `ledger.*` feed rows.

Sessions: W builds, V reviews, X and Y are others. Actors: michael (human), worker (the shared agent),
builder and reviewer (named agents)."""

import sqlite3

import pytest

from gated import DEST, accepting, section_file, ticket, walk_to_merged


def question():
    return ("--question", f"@{section_file('question', 'Which way?')}")


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def refusal(trk, *argv, code=1):
    result = trk(*argv, "--json")
    assert result.code == code, (argv, result.out, result.err)
    return result.error()


def head(trk):
    return trk("changes", "--since", "0", "--json").json()["head"]


def feed(trk, since=0):
    return trk("changes", "--since", str(since), "--json").json()["changes"]


def item(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


def rows(trk, t):
    return trk("ledger", "list", t, "--json").json()["ledger"]


@pytest.fixture
def world(trk, tmp_path):
    ok(trk, "init", "--me", "michael")
    for agent in ("worker", "builder", "reviewer"):
        ok(trk, "actor", "add", agent, "--kind", "agent")
    ok(trk, "roles", "set", "engineering", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p)
    for argv in accepting(j):
        ok(trk, *argv)
    return {"P": p, "J": j}


HEAD = "abcdef1234567"
FIX = "fedcba7654321"


def new_ticket(trk, world):
    return ok(trk, "new", "work-item", "T", "--form", "spec-bound", "--parent", world["J"], *ticket("spec-bound"))


def as_(actor, session=None):
    return ("--as", actor, *(("--session", session) if session else ()))


def build(trk, t, actor="builder", session="W"):
    """The claim, pr and a passing test-run at HEAD as `actor` in `session`, then advance to Built and let go."""
    ok(trk, "claim", t, *as_(actor, session))
    ok(trk, "evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", "pr-1", "--sha", HEAD, "--note", "PR",
       *as_(actor, session))
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "run-1", "--sha", HEAD,
       "--verdict", "pass", "--note", "green", *as_(actor, session))
    ok(trk, "advance", t, *as_(actor, session))
    ok(trk, "claim", "release", t, *as_(actor, session))


FORK = ("--kind", "fork", "--decided", "Kept the cache", "--alternative", "Dropped it", "--why", "Reads are hot")


def fork(trk, t, *flags):
    """A builder ledger row; a writer with a session holds the ticket's claim while it writes it."""
    held = "--session" in flags and "--origin" not in flags
    if held:
        ok(trk, "claim", t, *flags)
    try:
        return ok(trk, "ledger", "add", t, *FORK, *flags)
    finally:
        if held:
            ok(trk, "claim", "release", t, *flags)


@pytest.fixture
def built(trk, world):
    """A ticket at Built by builder in W, with builder row ledger-1."""
    t = new_ticket(trk, world)
    build(trk, t)
    assert fork(trk, t, *as_("builder", "W")) == "ledger-1"
    return t


# --- ac-1: what `ledger add` refuses, handles and corrections ---

@pytest.mark.parametrize("flags", [
    ("--kind", "fork", "--decided", "d", "--why", "w"),
    ("--kind", "fork", "--decided", "d", "--alternative", "a"),
    ("--kind", "fork", "--alternative", "a", "--why", "w"),
    ("--kind", "contradiction", "--decided", "d", "--why", "w"),
    ("--kind", "fork", "--decided", "d", "--alternative", "a", "--why", "w", "--noise"),
    ("--kind", "fork", "--decided", "d", "--alternative", "a", "--why", "w", "--repeat-of", "tr-aaaaa:1",
     "--topic", "units"),
    ("--kind", "fork", "--decided", "d", "--alternative", "a", "--why", "w", "--origin", "auditor", "--session", "V",
     "--repeat-of", "tr-aaaaa:1"),
    ("--kind", "fork", "--decided", "two\nlines", "--alternative", "a", "--why", "w"),
    ("--kind", "fork", "--decided", "d", "--alternative", "a", "--why", "w", "--sha", "xyz"),
    ("--kind", "fork", "--decided", "d", "--alternative", "a", "--why", "w", "--refers-to", "7"),
    ("--kind", "vibe", "--decided", "d", "--alternative", "a", "--why", "w"),
])
def test_ledger_add_refuses_an_incomplete_row_as_a_usage_error_writing_nothing(trk, built, flags):
    before = head(trk)
    refusal(trk, "ledger", "add", built, *flags, code=2)
    assert head(trk) == before


def test_rows_are_numbered_per_ticket_and_a_correction_refers_to_the_row_it_corrects(trk, world, built):
    other = new_ticket(trk, world)
    assert fork(trk, other) == "ledger-1"
    since = head(trk)
    ok(trk, "claim", built, *as_("builder", "W"))
    assert ok(trk, "ledger", "add", built, "--kind", "contradiction", "--decided", "Wrote to the journal twice",
              "--why", "Simpler", "--conflicts-with", "docs/adr/0002-herdr-contract.md", "--file", "src/a.py",
              "--file", "src/b.py", "--sha", HEAD.upper(), *as_("builder", "W")) == "ledger-2"
    assert ok(trk, "ledger", "add", built, *FORK, "--refers-to", "ledger-1", "--would-have-asked", "Hot enough?",
              *as_("builder", "W")) == "ledger-3"
    listed = rows(trk, built)
    assert [r["handle"] for r in listed] == ["ledger-1", "ledger-2", "ledger-3"]
    second, third = listed[1], listed[2]
    assert (second["kind"], second["conflicts_with"], second["files"], second["sha"], second["origin"]) == (
        "contradiction", "docs/adr/0002-herdr-contract.md", ["src/a.py", "src/b.py"], HEAD.lower(), "builder")
    assert (third["refers_to"], third["alternatives"], third["would_have_asked"]) == (1, ["Dropped it"], "Hot enough?")
    added = [r for r in feed(trk, since) if r["change"] == "ledger.added"]
    assert [r["data"]["ledger"] for r in added] == ["ledger-2", "ledger-3"]
    assert added[0]["data"]["conflicts_with"] == "docs/adr/0002-herdr-contract.md"
    assert (added[0]["actor"], added[0]["session"], added[0]["item"]) == ("builder", "W", built)


@pytest.mark.parametrize("target", ["program", "project", "decision", "ask"])
def test_ledger_rows_belong_to_tickets_only(trk, world, built, target):
    if target == "program":
        other = world["P"]
    elif target == "project":
        other = world["J"]
    elif target == "decision":
        other = ok(trk, "new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", world["J"])
    else:
        other = ok(trk, "ask", built, "--to", "engineering", "Q?", *question(), "--as", "worker")
    assert refusal(trk, "ledger", "add", other, *FORK)["rule"] == "ledger-tickets-only"


def test_no_row_is_added_once_the_ticket_is_at_merged_and_one_is_again_once_it_is_back(trk, world):
    t = new_ticket(trk, world)
    walk_to_merged(lambda *argv: ok(trk, *argv), t)
    error = refusal(trk, "ledger", "add", t, *FORK)
    assert (error["rule"], error["item"]) == ("ledger-closed", t)
    ok(trk, "reopen-stage", t, "built", "--reason", "The merge broke it.")
    assert fork(trk, t) == "ledger-1"


def test_no_row_is_added_to_a_closed_ticket(trk, built):
    ok(trk, "close", built, "--resolution", "wont-do", "--outcome", "Dropped.")
    assert refusal(trk, "ledger", "add", built, *FORK)["rule"] == "closed"


def test_an_unknown_ticket_or_row_exits_3(trk, built):
    assert trk("ledger", "add", "tr-zzzzz", *FORK).code == 3
    assert trk("ledger", "verdict", built, "9", "sound", "--note", "n", "--session", "V").code == 3
    assert trk("ledger", "list", "tr-zzzzz").code == 3


# --- ac-2: the independence matrix ---

def verdict(trk, t, actor, session):
    return trk("ledger", "verdict", t, "1", "sound", "--note", "Fine", *as_(actor, session), "--json")


def auditor_row(trk, t, actor, session):
    return trk("ledger", "add", t, *FORK, "--origin", "auditor", *as_(actor, session), "--json")


def audit(trk, t, actor, session):
    return trk("ledger", "audit", t, "--note", "Read the diff", *as_(actor, session), "--json")


ACTS = {"verdict": verdict, "auditor row": auditor_row, "audit": audit}


def by_ledger_row(trk, world):
    t = new_ticket(trk, world)
    build(trk, t, "michael", "M")
    fork(trk, t, *as_("worker", "X"))
    return t


def by_pr(trk, world):
    t = new_ticket(trk, world)
    build(trk, t, "michael", "M")
    fork(trk, t, *as_("michael", "M"))
    ok(trk, "claim", t, *as_("worker", "X"))
    ok(trk, "evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", "pr-1", "--sha", HEAD, "--note", "PR",
       *as_("worker", "X"))
    ok(trk, "claim", "release", t, *as_("worker", "X"))
    return t


def by_test_run(trk, world):
    t = new_ticket(trk, world)
    build(trk, t, "michael", "M")
    fork(trk, t, *as_("michael", "M"))
    ok(trk, "claim", t, *as_("worker", "X"))
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "run-2", "--sha", HEAD,
       "--verdict", "pass", "--note", "green", *as_("worker", "X"))
    ok(trk, "claim", "release", t, *as_("worker", "X"))
    return t


def by_pre_built_claim(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_("worker", "X"))
    ok(trk, "claim", "release", *as_("worker", "X"))
    build(trk, t, "michael", "M")
    fork(trk, t, *as_("michael", "M"))
    return t


@pytest.mark.parametrize("act", ACTS)
@pytest.mark.parametrize("setup", [by_ledger_row, by_pr, by_test_run, by_pre_built_claim])
def test_a_builder_session_is_not_independent_but_the_shared_agent_in_another_session_is(trk, world, act, setup):
    t = setup(trk, world)
    before = head(trk)
    result = ACTS[act](trk, t, "worker", "X")
    assert result.code == 1, result.out
    assert (result.error()["rule"], result.error()["item"]) == ("not-independent", t)
    assert head(trk) == before
    assert ACTS[act](trk, t, "worker", "Y").code == 0


@pytest.mark.parametrize("act", ACTS)
def test_the_builder_named_agent_is_not_independent_under_another_session(trk, built, act):
    result = ACTS[act](trk, built, "builder", "Z")
    assert result.code == 1
    assert result.error()["rule"] == "not-independent"


@pytest.mark.parametrize("act", ACTS)
def test_a_fresh_session_is_independent(trk, built, act):
    assert ACTS[act](trk, built, "reviewer", "V").code == 0


@pytest.mark.parametrize("act", ACTS)
def test_independence_is_checked_by_session_so_one_is_required(trk, built, act):
    result = ACTS[act](trk, built, "reviewer", None)
    assert (result.code, result.error()["rule"]) == (1, "session-required")


@pytest.mark.parametrize("act", ACTS)
def test_the_session_comes_from_trk_session_too(trk, built, act, monkeypatch):
    monkeypatch.setenv("TRK_SESSION", "W")
    assert ACTS[act](trk, built, "reviewer", None).error()["rule"] == "not-independent"
    monkeypatch.setenv("TRK_SESSION", "V")
    assert ACTS[act](trk, built, "reviewer", None).code == 0


def test_a_claim_taken_after_built_does_not_make_a_builder(trk, built):
    ok(trk, "claim", built, *as_("reviewer", "V"))
    assert verdict(trk, built, "reviewer", "V").code == 0


def return_below_built(trk, t, at, monkeypatch):
    """Move the ticket back to specd at `at`."""
    monkeypatch.setenv("TRK_NOW", at)
    ok(trk, "reopen-stage", t, "specd", "--reason", "Rebuild it.")


def test_a_claim_held_across_a_return_below_built_makes_a_builder(trk, built, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-11T12:00:00Z")
    ok(trk, "claim", built, *as_("worker", "X"))
    return_below_built(trk, built, "2026-10-11T12:10:00Z", monkeypatch)
    assert verdict(trk, built, "worker", "X").error()["rule"] == "not-independent"


def test_a_named_agent_that_claimed_before_built_is_a_builder_under_any_session(trk, world):
    t = new_ticket(trk, world)
    ok(trk, "claim", t, *as_("builder", "X"))
    ok(trk, "claim", "release", *as_("builder", "X"))
    build(trk, t, "michael", "M")
    fork(trk, t, *as_("michael", "M"))
    assert verdict(trk, t, "builder", "Y").error()["rule"] == "not-independent"


def test_a_claim_that_lapsed_before_a_return_below_built_does_not_make_a_builder(trk, built, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-11T12:00:00Z")
    ok(trk, "claim", built, *as_("reviewer", "V"), "--lease", "30m")
    # lapsed at 12:30, and no write has swept it before the move back
    return_below_built(trk, built, "2026-10-11T13:00:00Z", monkeypatch)
    monkeypatch.setenv("TRK_NOW", "2026-10-11T13:01:00Z")
    assert verdict(trk, built, "reviewer", "V").code == 0


def test_a_human_builder_is_checked_by_session_only(trk, world):
    t = new_ticket(trk, world)
    build(trk, t, "michael", "M")
    fork(trk, t, *as_("michael", "M"))
    assert verdict(trk, t, "michael", "M").error()["rule"] == "not-independent"
    assert verdict(trk, t, "michael", "H").code == 0


# --- ac-5: the shared agent is a setting ---

def test_the_shared_agent_defaults_to_worker_and_config_set_changes_it(trk, world):
    assert trk("config", "--json").json()["config"]["shared-agent"] == "worker"
    t = new_ticket(trk, world)
    build(trk, t, "worker", "W")
    fork(trk, t, *as_("worker", "W"))
    assert verdict(trk, t, "worker", "V").code == 0
    since = head(trk)
    ok(trk, "config", "set", "shared-agent", "builder")
    assert trk("config", "--json").json()["config"]["shared-agent"] == "builder"
    (changed,) = feed(trk, since)
    assert (changed["change"], changed["data"]) == ("config.changed",
                                                     {"key": "shared-agent", "old": "worker", "new": "builder"})
    assert verdict(trk, t, "worker", "Y").error()["rule"] == "not-independent"


def test_the_shared_agent_must_be_a_registered_actor(trk, world):
    assert refusal(trk, "config", "set", "shared-agent", "ghost")["rule"] == "unknown-actor"


def test_shared_agent_may_name_several_actors_each_checked_by_session_only(trk, world):
    ok(trk, "actor", "add", "auditor", "--kind", "agent")
    assert refusal(trk, "config", "set", "shared-agent", "worker,ghost")["rule"] == "unknown-actor"
    ok(trk, "config", "set", "shared-agent", "worker, auditor,worker")
    assert trk("config", "--json").json()["config"]["shared-agent"] == "worker,auditor"
    t = new_ticket(trk, world)
    build(trk, t, "auditor", "W")
    fork(trk, t, *as_("auditor", "W"))
    assert verdict(trk, t, "auditor", "W").error()["rule"] == "not-independent"
    assert verdict(trk, t, "auditor", "X").code == 0


# --- ac-3: verdicts by row kind, and the audit row ---

def flag(trk, t):
    return ok(trk, "ledger", "add", t, "--kind", "reward-hack", "--decided", "Regenerated the golden file",
              "--why", "The test matches whatever the code prints", "--origin", "auditor", *as_("reviewer", "V"))


@pytest.mark.parametrize("kind, value, code", [
    ("fork", "sound", 0), ("fork", "risky", 0), ("fork", "wrong", 0), ("fork", "confirmed", 2), ("fork", "refuted", 2),
    ("reward-hack", "confirmed", 0), ("reward-hack", "refuted", 0), ("reward-hack", "sound", 2),
    ("reward-hack", "wrong", 2), ("fork", "maybe", 2),
])
def test_verdicts_fit_the_row_kind(trk, built, kind, value, code):
    n = "1" if kind == "fork" else flag(trk, built)
    before = head(trk)
    result = trk("ledger", "verdict", built, n, value, "--note", "Judged", *as_("reviewer", "V"))
    assert result.code == code, result.err
    if code:
        assert head(trk) == before


def test_a_verdict_defaults_to_the_head_sha_and_its_feed_row_carries_it(trk, built):
    since = head(trk)
    ok(trk, "ledger", "verdict", built, "ledger-1", "risky", "--note", "Cache can go stale", *as_("reviewer", "V"))
    ok(trk, "ledger", "verdict", built, "1", "sound", "--note", "Fine after all", "--sha", FIX, *as_("reviewer", "V"))
    first, second = feed(trk, since)
    assert (first["change"], first["data"]) == ("ledger.verdict", {
        "ledger": "ledger-1", "n": 1, "kind": "fork", "verdict": "risky", "note": "Cache can go stale", "sha": HEAD})
    assert second["data"]["sha"] == FIX
    latest = rows(trk, built)[0]["verdict"]
    assert (latest["verdict"], latest["note"], latest["actor"], latest["session"]) == ("sound", "Fine after all",
                                                                                     "reviewer", "V")


def test_a_verdict_needs_a_note(trk, built):
    assert trk("ledger", "verdict", built, "1", "sound", *as_("reviewer", "V")).code == 2
    assert trk("ledger", "verdict", built, "1", "sound", "--note", " ", *as_("reviewer", "V")).code == 2


def test_ledger_audit_records_an_audit_row_at_the_head_sha(trk, built):
    since = head(trk)
    evidence_id = ok(trk, "ledger", "audit", built, "--note", "Ledger complete", *as_("reviewer", "V"))
    (row,) = [r for r in trk("evidence", built, "--json").json()["evidence"] if r["kind"] == "audit"]
    assert (row["id"], row["gate"], row["sha"], row["actor"], row["session"], row["counts"]) == (
        evidence_id, "merged", HEAD, "reviewer", "V", True)
    (added,) = feed(trk, since)
    assert (added["change"], added["data"]["kind"]) == ("evidence.added", "audit")


def test_ledger_audit_needs_a_head_sha(trk, world):
    t = new_ticket(trk, world)
    error = refusal(trk, "ledger", "audit", t, "--note", "n", *as_("reviewer", "V"))
    assert (error["rule"], error["missing"]) == ("gate-unmet", [{"need": "pr"}])


def test_verdicts_and_audits_are_refused_on_a_closed_ticket(trk, built):
    ok(trk, "close", built, "--resolution", "wont-do", "--outcome", "Dropped.")
    assert verdict(trk, built, "reviewer", "V").error()["rule"] == "closed"
    assert audit(trk, built, "reviewer", "V").error()["rule"] == "closed"


# --- settling fixed ---

def test_settle_fixed_records_the_fix_sha_and_the_builder_may_write_it(trk, built):
    ok(trk, "ledger", "verdict", built, "1", "wrong", "--note", "Stale reads", *as_("reviewer", "V"))
    since = head(trk)
    ok(trk, "ledger", "settle", built, "1", "fixed", "--sha", FIX, "--note", "Invalidate on write",
       *as_("builder", "W"))
    (settled,) = feed(trk, since)
    assert (settled["change"], settled["data"]) == ("ledger.settled", {
        "ledger": "ledger-1", "n": 1, "kind": "fork", "settlement": "fixed", "sha": FIX, "ask": None, "ticket": None,
        "adr": None, "note": "Invalidate on write", "verdict_seq": None, "verdict_sha": None})
    assert rows(trk, built)[0]["settlement"]["settlement"] == "fixed"


def test_settle_fixed_needs_a_sha(trk, built):
    assert trk("ledger", "settle", built, "1", "fixed", *as_("builder", "W")).code == 2


def contradiction(trk, t):
    ok(trk, "claim", t, *as_("builder", "W"))
    return ok(trk, "ledger", "add", t, "--kind", "contradiction", "--decided", "Two journal rows", "--why", "Simpler",
              "--conflicts-with", "spec:Evidence", *as_("builder", "W"))


def engineering_ask(trk, t, n, answer=True, asker="reviewer", answerer="michael"):
    ask = ok(trk, "ask", t, "--to", "engineering", "--ledger", n, "Which wins?", *question(), "--as", asker)
    if answer:
        ok(trk, "answer", ask, "--outcome", "The spec wins", "--answer", f"@{section_file('answer', 'The spec.')}",
           "--as", answerer)
    return ask


def test_a_contradiction_is_fixed_only_through_its_answered_engineering_ask(trk, built):
    n = contradiction(trk, built)
    other = engineering_ask(trk, built, "1")
    still_open = engineering_ask(trk, built, n, answer=False)

    def settle(*flags, actor="reviewer", session="V"):
        return trk("ledger", "settle", built, n, "fixed", "--sha", FIX, *flags, *as_(actor, session), "--json")

    assert settle().error()["rule"] == "settle-needs-answer"
    assert settle("--ask", other).error()["rule"] == "settle-needs-answer"
    assert settle("--ask", still_open).error()["rule"] == "settle-needs-answer"
    answered = engineering_ask(trk, built, n)
    assert settle("--ask", answered, actor="builder", session="W").error()["rule"] == "settle-needs-answer"
    assert settle("--ask", answered).code == 0
    assert rows(trk, built)[1]["settlement"]["ask"] == answered


def test_the_builder_records_a_forks_fix_with_a_tied_answered_ask(trk, built):
    ask = engineering_ask(trk, built, "1")
    ok(trk, "ledger", "settle", built, "1", "fixed", "--sha", FIX, "--ask", ask, *as_("builder", "W"))
    assert rows(trk, built)[0]["settlement"]["ask"] == ask


def test_a_contradiction_ask_answered_by_an_agent_does_not_settle_it(trk, built):
    n = contradiction(trk, built)
    ok(trk, "roles", "set", "engineering", "worker")
    ask = engineering_ask(trk, built, n, asker="reviewer", answerer="worker")
    result = trk("ledger", "settle", built, n, "fixed", "--sha", FIX, "--ask", ask, *as_("reviewer", "V"), "--json")
    assert result.error()["rule"] == "settle-needs-answer"


# --- ac-4: list, show and asks tied to a row ---

def test_ask_ledger_ties_the_ask_to_the_row_both_ways(trk, built):
    since = head(trk)
    ask = ok(trk, "ask", built, "--to", "engineering", "--ledger", "ledger-1", "Promote to an ADR?", *question(),
             "--as", "reviewer")
    (opened,) = feed(trk, since)
    assert opened["data"]["ledger"] == "ledger-1"
    assert item(trk, ask)["ledger"] == "ledger-1"
    assert [a["id"] for a in rows(trk, built)[0]["asks"]] == [ask]
    plain = ok(trk, "ask", built, "--to", "engineering", "Other?", *question(), "--as", "reviewer")
    assert item(trk, plain)["ledger"] is None
    assert feed(trk, head(trk) - 1)[0]["data"]["ledger"] is None


@pytest.mark.parametrize("which", ["no such row", "not a ticket", "bad handle"])
def test_ask_ledger_on_a_parent_without_that_row_is_a_usage_error(trk, world, built, which):
    parent, n = {"no such row": (built, "2"), "not a ticket": (world["J"], "1"), "bad handle": (built, "one")}[which]
    before = head(trk)
    assert trk("ask", parent, "--to", "engineering", "--ledger", n, "Q?", *question(), "--as", "reviewer").code == 2
    assert head(trk) == before


def test_ledger_list_and_show_carry_each_rows_latest_verdict_settlement_promotion_and_asks(trk, built):
    ok(trk, "ledger", "verdict", built, "1", "wrong", "--note", "Stale reads", *as_("reviewer", "V"))
    ok(trk, "ledger", "settle", built, "1", "fixed", "--sha", FIX, *as_("builder", "W"))
    ok(trk, "ledger", "promote", built, "1", "--adr", "docs/adr/0003-cache.md")
    fork(trk, built, *as_("builder", "W"))
    ask = engineering_ask(trk, built, "1", answer=False)
    first, second = rows(trk, built)
    assert (first["verdict"]["verdict"], first["settlement"]["sha"], first["promotion"]["adr"]) == (
        "wrong", FIX, "docs/adr/0003-cache.md")
    assert first["asks"] == [{"id": ask, "title": "Which wins?", "status": "open", "resolution": None}]
    assert (second["verdict"], second["settlement"], second["promotion"], second["asks"]) == (None, None, None, [])
    shown = item(trk, built)
    assert shown["ledger_rows"] == [first, second]
    assert item(trk, ask)["ledger_rows"] == []
    text = ok(trk, "show", built)
    assert "ledger-1  fork  builder  Kept the cache" in text
    assert "verdict wrong" in text and "settled fixed" in text and f"asks {ask}" in text
    listed = ok(trk, "ledger", "list", built)
    assert listed.splitlines()[0].startswith("ledger-1  fork  builder  Kept the cache")


def test_promotion_is_for_humans_only(trk, built):
    since = head(trk)
    error = refusal(trk, "ledger", "promote", built, "1", "--adr", "docs/adr/0003.md", "--as", "reviewer")
    assert error["rule"] == "human-only"
    assert head(trk) == since
    ok(trk, "ledger", "promote", built, "1", "--adr", "docs/adr/0003.md")
    (promoted,) = feed(trk, since)
    assert (promoted["change"], promoted["data"]) == ("ledger.promoted", {
        "ledger": "ledger-1", "n": 1, "kind": "fork", "adr": "docs/adr/0003.md"})


# --- auditor rows ---

def test_an_auditor_marks_noise_and_a_repeated_guess(trk, world, built):
    other = new_ticket(trk, world)
    fork(trk, other)
    noise = ok(trk, "ledger", "add", built, *FORK, "--origin", "auditor", "--noise", *as_("reviewer", "V"))
    repeat = ok(trk, "ledger", "add", built, *FORK, "--origin", "auditor", "--repeat-of", f"{other}:1",
                "--topic", "units", *as_("reviewer", "V"))
    listed = {r["handle"]: r for r in rows(trk, built)}
    assert (listed[noise]["noise"], listed[noise]["origin"]) == (True, "auditor")
    assert (listed[repeat]["repeat_of"], listed[repeat]["topic"]) == (f"{other}:1", "units")
    assert refusal(trk, "ledger", "add", built, *FORK, "--origin", "auditor", "--repeat-of", f"{other}:5",
                   "--topic", "units", *as_("reviewer", "V"), code=2)


# --- the tables stay append-only ---

@pytest.mark.parametrize("statement", [
    "UPDATE ledger SET why = 'x'", "DELETE FROM ledger", "UPDATE ledger_verdicts SET note = 'x'",
    "DELETE FROM ledger_verdicts", "DELETE FROM ledger_settlements", "DELETE FROM ledger_promotions",
])
def test_ledger_tables_refuse_update_and_delete(trk, built, trk_home, statement):
    ok(trk, "ledger", "verdict", built, "1", "wrong", "--note", "n", *as_("reviewer", "V"))
    ok(trk, "ledger", "settle", built, "1", "fixed", "--sha", FIX, *as_("builder", "W"))
    ok(trk, "ledger", "promote", built, "1", "--adr", "docs/adr/0003.md")
    conn = sqlite3.connect(trk_home / "store.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(statement)
    conn.close()


# --- repeated guesses file template tickets ---

TEMPLATE = "Ticket template: add a field for units"


def repeat(trk, t, target, *flags, topic="units"):
    return ok(trk, "ledger", "add", t, *FORK, "--origin", "auditor", "--repeat-of", target,
              *(("--topic", topic) if topic is not None else ()), *as_("reviewer", "V"), *flags)


def related(trk, item_id):
    return {e["id"] for e in item(trk, item_id)["edges"] if e["type"] == "relates"}


def templates(trk, parent):
    return [c for c in trk("tree", parent, "--json").json()["items"] if c["title"].startswith("Ticket template:")]


def test_a_repeated_guess_files_a_template_ticket_under_the_template_parent_and_a_second_repeat_relates_to_it(
        trk, world, built):
    """Spec AC 12."""
    w = ok(trk, "new", "project", "W", "--parent", world["P"])
    ok(trk, "config", "set", "template-parent", w)
    assert trk("config", "--json").json()["config"]["template-parent"] == w
    t5, t6 = built, new_ticket(trk, world)
    fork(trk, t6)
    repeat(trk, t5, f"{t6}:1")
    [filed] = templates(trk, w)
    assert (filed["title"], filed["parent"], filed["form"], filed["stage"]) == (TEMPLATE, w, "self-contained", "framed")
    assert related(trk, filed["id"]) == {t5, t6}

    t7 = new_ticket(trk, world)
    fork(trk, t7)
    repeat(trk, t7, f"{t6}:1")
    assert [c["id"] for c in templates(trk, w)] == [filed["id"]]
    assert related(trk, filed["id"]) == {t5, t6, t7}


def test_without_a_template_parent_the_ticket_is_filed_under_the_reviewed_tickets_root_citing_both_rows(
        trk, world, built):
    other = new_ticket(trk, world)
    fork(trk, other)
    since = head(trk)
    handle = repeat(trk, built, f"{other}:1")
    [filed] = templates(trk, world["P"])
    assert filed["parent"] == world["P"]
    bodies = {s["handle"]: s["body"] for s in item(trk, filed["id"])["sections"]}
    current = bodies["current-behaviour"]
    assert f"{built} {handle}" in current and f"{other} ledger-1" in current
    assert "units" in bodies["desired-behaviour"]
    assert len(trk("criteria", filed["id"], "--json").json()["criteria"]) == 1
    assert [c["change"] for c in feed(trk, since)] == [
        "ledger.added", "item.created", "criterion.added", "stage.changed", "edge.added", "edge.added"]


def test_a_template_ticket_closed_or_outside_the_template_parent_is_not_reused(trk, world, built):
    other = new_ticket(trk, world)
    fork(trk, other)
    repeat(trk, built, f"{other}:1")
    [first] = templates(trk, world["P"])
    elsewhere = ok(trk, "new", "work-item", TEMPLATE, "--form", "self-contained", "--parent", world["J"],
                   *ticket("self-contained"))
    ok(trk, "close", first["id"], "--resolution", "wont-do", "--outcome", "Not now")
    repeat(trk, built, f"{other}:1")
    filed = [c["id"] for c in templates(trk, world["P"]) if c["status"] == "open" and c["parent"] == world["P"]]
    assert len(filed) == 1 and filed[0] not in (first["id"], elsewhere)
    assert related(trk, elsewhere) == set()


def test_a_template_parent_closed_after_it_was_set_refuses_the_auditor_row_writing_nothing(trk, world, built):
    w = ok(trk, "new", "project", "W", "--parent", world["P"])
    ok(trk, "config", "set", "template-parent", w)
    ok(trk, "close", w, "--resolution", "wont-do", "--outcome", "Dropped")
    other = new_ticket(trk, world)
    fork(trk, other)
    before = head(trk)
    assert refusal(trk, "ledger", "add", built, *FORK, "--origin", "auditor", "--repeat-of", f"{other}:1",
                   "--topic", "units", *as_("reviewer", "V"))["rule"] == "parent-closed"
    assert head(trk) == before


@pytest.mark.parametrize("target, topic", [("{other}:5", "units"), ("{other}:1", None)])
def test_a_repeat_naming_a_missing_row_or_without_a_topic_is_refused_writing_nothing(trk, world, built, target, topic):
    other = new_ticket(trk, world)
    fork(trk, other)
    before = head(trk)
    argv = ["ledger", "add", built, *FORK, "--origin", "auditor", "--repeat-of", target.format(other=other),
            *(("--topic", topic) if topic else ()), *as_("reviewer", "V")]
    refusal(trk, *argv, code=2)
    assert head(trk) == before


def test_the_template_parent_must_be_an_open_item_that_takes_a_ticket(trk, world, built):
    assert trk("config", "--json").json()["config"]["template-parent"] is None
    assert refusal(trk, "config", "set", "template-parent", "tr-zzzzz", code=3)["code"] == "not-found"
    ask = ok(trk, "ask", built, "--to", "engineering", "Which way?", *question(), *as_("builder", "W"))
    assert refusal(trk, "config", "set", "template-parent", ask)["rule"] == "ask-leaf"
    closed = ok(trk, "new", "project", "Old", "--parent", world["P"])
    ok(trk, "close", closed, "--resolution", "wont-do", "--outcome", "Dropped")
    assert refusal(trk, "config", "set", "template-parent", closed)["rule"] == "parent-closed"
    before = head(trk)
    ok(trk, "config", "set", "template-parent", world["J"])
    assert feed(trk, before)[-1]["data"] == {"key": "template-parent", "old": None, "new": world["J"]}
