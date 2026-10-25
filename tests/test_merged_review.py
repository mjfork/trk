"""The Merged gate additions (Stage gates, Merged gate additions; Ledger): for a YELLOW or RED ticket, an
independent `audit`, a latest independent verdict on every ledger row, every WRONG, RISKY and confirmed
reward-hacking row settled, and every contradiction settled through its answered `engineering` ask;
`ledger settle … deferred | accepted --ask [--adr] | overruled --ask` with rules `settle-needs-answer` and
`reward-hack-final`; the `fixed` re-check; audits and verdicts carried over a fix commit by ancestry.

Every test runs against a real throwaway git repo. Sessions: W builds, V reviews, X is another. Actors:
michael (human, engineering), worker (the shared agent), builder and reviewer (named agents)."""

import pytest

from gated import DEST, accept, commit, git, section_file, ticket
from test_blast_and_verified import feed, head, item, merge, ok, refusal, write_brief

YELLOW = "YELLOW src/**\n"
REVIEW = "https://github.com/o/r/pull/1#pullrequestreview-1"


def as_(actor, session=None):
    return ("--as", actor, *(("--session", session) if session else ()))


W, V = as_("builder", "W"), as_("reviewer", "V")


@pytest.fixture
def lane(trk, tmp_path):
    """A checkout L (rules `YELLOW src/**`, `RED contract/**`) on main, with a feature branch; the store
    with program P and accepted project J on L. Returns a dict with the path, J and a `ticket(files)`
    maker that commits `files` on the feature branch and takes a new ticket to Built there as builder in W."""
    r = tmp_path / "L"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, ".trk/blast-radius", YELLOW + "RED contract/**\n")
    git(r, "checkout", "-q", "-b", "feature")
    ok(trk, "init", "--me", "michael")
    for agent in ("worker", "builder", "reviewer"):
        ok(trk, "actor", "add", agent, "--kind", "agent")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", str(r))
    accept(trk, j)
    world = {"path": str(r), "J": j}

    def new(files=("src/a.py",), title="T"):
        for name in files:
            sha = commit(r, name)
        t = ok(trk, "new", "work-item", title, "--form", "spec-bound", "--parent", j, *ticket("spec-bound"))
        build_at(trk, t, sha)
        ok(trk, "advance", t, *W)
        return t, sha

    world["ticket"] = new
    return world


def build_at(trk, t, sha):
    """The claim, then pr and a passing test-run at `sha`, as builder in W (W keeps the claim)."""
    ok(trk, "claim", t, *W)
    ok(trk, "evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", "https://github.com/o/r/pull/1",
       "--sha", sha, "--note", "PR", *W)
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "run-1", "--sha", sha,
       "--verdict", "pass", "--note", "green", *W)


FORK = ("--kind", "fork", "--decided", "Kept the cache", "--alternative", "Dropped it", "--why", "Reads are hot")


def fork(trk, t, *flags):
    return ok(trk, "ledger", "add", t, *FORK, *(flags or W))


def flag(trk, t):
    return ok(trk, "ledger", "add", t, "--kind", "reward-hack", "--decided", "Regenerated the golden",
              "--why", "The golden changed with the code", "--origin", "auditor", *V)


def contradiction(trk, t):
    return ok(trk, "ledger", "add", t, "--kind", "contradiction", "--decided", "Two journal rows", "--why", "Simpler",
              "--conflicts-with", "spec:Evidence", *W)


def judge(trk, t, n, value, *flags, who=V):
    return ok(trk, "ledger", "verdict", t, n, value, "--note", f"Judged {value}", *flags, *who)


def settle(trk, t, n, settlement, *flags, who=V):
    return trk("ledger", "settle", t, n, settlement, *flags, *who, "--json")


def question():
    return ("--question", f"@{section_file('question', 'Which way?')}")


def tied_ask(trk, t, n, answer=True, to="engineering", answerer="michael"):
    ask = ok(trk, "ask", t, "--to", to, "--ledger", n, "Which wins?", *question(), "--as", "reviewer")
    if answer:
        ok(trk, "answer", ask, "--outcome", "Decided", "--answer", f"@{section_file('answer', 'This way.')}",
           "--as", answerer)
    return ask


def missing(trk, t):
    return item(trk, t)["gate"]["missing"]


def ledger_needs(trk, t):
    return [(m["need"], m["ledger"]) for m in missing(trk, t) if "ledger" in m]


def rows(trk, t):
    return trk("ledger", "list", t, "--json").json()["ledger"]


def fix_commit(trk, lane, t, name):
    """Commit `name` on the feature branch as a fix, record pr and test-run there and re-rate; returns its sha."""
    fix = commit(lane["path"], name)
    build_at(trk, t, fix)
    ok(trk, "blast-radius", t, *W)
    return fix


def reviewed(trk, t, sha):
    """Blast radius, the audit from V and the merge row at `sha`."""
    ok(trk, "blast-radius", t, *W)
    ok(trk, "ledger", "audit", t, "--note", "Read the diff", *V)
    merge(trk, t, sha)


# --- ticket ac-1: spec AC 9 ---

def test_ac9_a_wrong_verdict_blocks_merged_until_its_fix_is_re_checked_and_the_audit_carries_over(trk, lane):
    t5, sha = lane["ticket"]()
    assert ok(trk, "blast-radius", t5, *W) == "yellow"
    assert fork(trk, t5) == "ledger-1"
    for who in (W, as_("builder", "X")):
        result = trk("ledger", "verdict", t5, "1", "sound", "--note", "fine", *who, "--json")
        assert result.error()["rule"] == "not-independent"
    assert fork(trk, t5, "--origin", "auditor", *V) == "ledger-2"
    audit = ok(trk, "ledger", "audit", t5, "--note", "Read the diff", *V)
    judge(trk, t5, "1", "wrong")
    judge(trk, t5, "2", "sound")
    merge(trk, t5, sha)
    error = refusal(trk, "advance", t5)
    assert (error["rule"], error["gate"]) == ("gate-unmet", "merged")
    assert error["missing"] == [{"need": "ledger-settled", "ledger": "ledger-1"}]
    assert missing(trk, t5) == error["missing"]

    fix = commit(lane["path"], "src/b.py")
    build_at(trk, t5, fix)
    assert settle(trk, t5, "1", "fixed", "--sha", fix, who=W).code == 0
    ok(trk, "blast-radius", t5, *W)
    assert ledger_needs(trk, t5) == [("ledger-settled", "ledger-1")]  # fixed waits for its re-check
    judge(trk, t5, "1", "sound")
    git(lane["path"], "checkout", "-q", "main")
    git(lane["path"], "merge", "-q", "--no-ff", "feature", "-m", "merge")
    merge(trk, t5, git(lane["path"], "rev-parse", "HEAD"))
    ok(trk, "advance", t5)
    assert item(trk, t5)["stage"] == "merged"
    counted = {e["id"]: e for e in trk("evidence", t5, "--json").json()["evidence"]}
    assert (counted[audit]["sha"], counted[audit]["counts"]) == (sha, True)


# --- ticket ac-2: spec AC 10 ---

def test_ac10_a_confirmed_reward_hack_flag_is_only_fixed(trk, lane):
    t6, sha = lane["ticket"]()
    n = flag(trk, t6)
    judge(trk, t6, n, "confirmed")
    a = tied_ask(trk, t6, n)
    before = head(trk)
    for settlement in ("overruled", "accepted"):
        assert settle(trk, t6, n, settlement, "--ask", a).error()["rule"] == "reward-hack-final"
    assert settle(trk, t6, n, "deferred").error()["rule"] == "reward-hack-final"
    assert head(trk) == before
    reviewed(trk, t6, sha)
    assert ledger_needs(trk, t6) == [("reward-hack-fixed", n)]

    fix = fix_commit(trk, lane, t6, "tests/golden.txt")
    assert settle(trk, t6, n, "fixed", "--sha", fix, who=W).code == 0
    assert ledger_needs(trk, t6) == [("reward-hack-fixed", n)]
    judge(trk, t6, n, "refuted")
    assert ledger_needs(trk, t6) == []


def test_ac10_a_risky_row_is_not_accepted_through_an_open_ask(trk, lane):
    t6, _ = lane["ticket"]()
    n = fork(trk, t6)
    judge(trk, t6, n, "risky")
    b = tied_ask(trk, t6, n, answer=False)
    before = head(trk)
    assert settle(trk, t6, n, "accepted", "--ask", b).error()["rule"] == "settle-needs-answer"
    assert head(trk) == before


def test_ac10_deferring_a_risky_row_files_a_framed_self_contained_ticket_related_to_it(trk, lane):
    t6, sha = lane["ticket"]()
    n = fork(trk, t6, "--file", "src/a.py", *W)
    ok(trk, "ledger", "verdict", t6, n, "risky", "--note", "Invalidate the cache on every write", *V)
    since = head(trk)
    assert settle(trk, t6, n, "deferred").code == 0
    settlement = rows(trk, t6)[0]["settlement"]
    deferred = item(trk, settlement["ticket"])
    assert (deferred["form"], deferred["stage"], deferred["parent"], deferred["status"]) == (
        "self-contained", "framed", lane["J"], "open")
    assert deferred["title"] == "Deferred: Kept the cache"
    assert [(e["type"], e["id"]) for e in deferred["edges"]] == [("relates", t6)]
    current = ok(trk, "section", "get", deferred["id"], "current-behaviour")
    for text in ("fork", "Kept the cache", "Reads are hot", "src/a.py", n, t6):
        assert text in current
    assert ok(trk, "section", "get", deferred["id"], "desired-behaviour") == "Invalidate the cache on every write"
    assert [c["text"] for c in deferred["criteria"]] == [f"{n} of {t6} resolved as the reviewer describes"]
    changes = [r["change"] for r in feed(trk, since)]
    assert changes[0] == "item.created" and changes[-1] == "ledger.settled" and "edge.added" in changes
    settled = feed(trk, since)[-1]["data"]
    assert (settled["settlement"], settled["ticket"], settled["sha"], settled["ask"]) == (
        "deferred", deferred["id"], None, None)
    reviewed(trk, t6, sha)
    assert ledger_needs(trk, t6) == []


# --- ticket ac-3: spec AC 11 end to end ---

def test_ac11_a_red_ticket_merges_once_reviewed_audited_judged_and_approved(trk, lane):
    t7, sha = lane["ticket"](("contract/x.json",))
    assert ok(trk, "blast-radius", t7, "--as", "worker") == "red"
    asks = [c for c in trk("tree", t7, "--all", "--json").json()["items"] if c["form"] == "ask"]
    assert [(a["asker"], a["addressee_role"]) for a in asks] == [("worker", "engineering")]
    fork(trk, t7)
    judge(trk, t7, "1", "sound")
    write_brief(trk, t7)
    ok(trk, "ledger", "audit", t7, "--note", "Read the diff", *V)
    merge(trk, t7, sha)
    ok(trk, "review", "record", t7, "--ref", REVIEW, "--sha", sha, "changes-requested", "--note", "Fix x",
       "--as", "worker")
    error = refusal(trk, "advance", t7)
    assert [m["need"] for m in error["missing"]] == ["human-review"]
    ok(trk, "review", "record", t7, "--ref", REVIEW, "--sha", sha, "approved", "--note", "Good", "--as", "michael")
    ok(trk, "advance", t7)
    assert item(trk, t7)["stage"] == "merged"


# --- ticket ac-4: who may settle through an answer, and reopening a fix ---

@pytest.mark.parametrize("settlement", ["accepted", "overruled"])
@pytest.mark.parametrize("case", ["no ask", "tied to another row", "answered by an agent", "not engineering",
                                  "builder session", "builder actor"])
def test_settle_needs_an_answered_engineering_ask_tied_to_the_row_from_a_non_builder(trk, lane, settlement, case):
    t, _ = lane["ticket"]()
    n = fork(trk, t)
    other = fork(trk, t)
    judge(trk, t, n, "risky" if settlement == "accepted" else "wrong")
    who = V
    if case == "no ask":
        flags = ()
    elif case == "tied to another row":
        flags = ("--ask", tied_ask(trk, t, other))
    elif case == "answered by an agent":
        ok(trk, "roles", "set", "engineering", "worker")
        flags = ("--ask", tied_ask(trk, t, n, answerer="worker"))
    elif case == "not engineering":
        ok(trk, "roles", "set", "product", "michael")
        flags = ("--ask", tied_ask(trk, t, n, to="product"))
    else:
        flags = ("--ask", tied_ask(trk, t, n))
        who = as_("reviewer", "W") if case == "builder session" else as_("builder", "X")
    before = head(trk)
    assert settle(trk, t, n, settlement, *flags, who=who).error()["rule"] == "settle-needs-answer"
    assert head(trk) == before


def test_accepting_and_overruling_through_an_answered_ask_settle_the_row(trk, lane):
    t, sha = lane["ticket"]()
    risky, wrong = fork(trk, t), fork(trk, t)
    judge(trk, t, risky, "risky")
    judge(trk, t, wrong, "wrong")
    reviewed(trk, t, sha)
    assert ledger_needs(trk, t) == [("ledger-settled", risky), ("ledger-settled", wrong)]
    before = head(trk)
    for n, settlement in ((risky, "overruled"), (wrong, "accepted"), (wrong, "deferred")):
        assert settle(trk, t, n, settlement, *(("--ask", "tr-aaaaa") if settlement != "deferred" else ())).code == 2
    assert head(trk) == before
    a, b = tied_ask(trk, t, risky), tied_ask(trk, t, wrong)
    assert settle(trk, t, risky, "accepted", "--ask", a, "--adr", "docs/adr/0009-cache.md").code == 0
    assert settle(trk, t, wrong, "overruled", "--ask", b, "--note", "Michael overrules").code == 0
    assert ledger_needs(trk, t) == []
    first = rows(trk, t)[0]["settlement"]
    assert (first["settlement"], first["ask"], first["adr"]) == ("accepted", a, "docs/adr/0009-cache.md")
    ok(trk, "advance", t)


def test_a_later_risky_or_wrong_verdict_reopens_a_fixed_row(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    fix = fix_commit(trk, lane, t, "src/c.py")
    settle(trk, t, n, "fixed", "--sha", fix, who=W)
    judge(trk, t, n, "sound")
    assert ledger_needs(trk, t) == []
    judge(trk, t, n, "risky")
    assert ledger_needs(trk, t) == [("ledger-settled", n)]


def test_a_re_check_at_a_sha_not_containing_the_fix_does_not_settle_it(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    fix = fix_commit(trk, lane, t, "src/c.py")
    settle(trk, t, n, "fixed", "--sha", fix, who=W)
    judge(trk, t, n, "sound", "--sha", sha)
    assert ledger_needs(trk, t) == [("ledger-settled", n)]


def test_a_contradiction_is_settled_only_through_its_answered_engineering_ask(trk, lane):
    t, sha = lane["ticket"]()
    n = contradiction(trk, t)
    judge(trk, t, n, "sound")
    reviewed(trk, t, sha)
    assert ledger_needs(trk, t) == [("ledger-settled", n)]
    assert settle(trk, t, n, "accepted", "--ask", tied_ask(trk, t, n), "--adr", "docs/adr/0002.md").code == 0
    assert ledger_needs(trk, t) == []


def test_a_contradiction_fixed_through_its_ask_needs_the_re_check(trk, lane):
    t, sha = lane["ticket"]()
    n = contradiction(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    fix = fix_commit(trk, lane, t, "src/c.py")
    assert settle(trk, t, n, "fixed", "--sha", fix, "--ask", tied_ask(trk, t, n)).code == 0
    assert ledger_needs(trk, t) == [("ledger-settled", n)]
    judge(trk, t, n, "sound")
    assert ledger_needs(trk, t) == []


def test_every_row_needs_an_independent_verdict_at_the_head_or_an_ancestor(trk, lane):
    t, sha = lane["ticket"]()
    first, second = fork(trk, t), fork(trk, t)
    reviewed(trk, t, sha)
    assert ledger_needs(trk, t) == [("ledger-verdict", first), ("ledger-verdict", second)]
    elsewhere = git(lane["path"], "rev-parse", "main")
    judge(trk, t, first, "sound")
    git(lane["path"], "checkout", "-q", "-b", "side", elsewhere)
    side = commit(lane["path"], "side.txt")
    judge(trk, t, second, "sound", "--sha", side)
    assert ledger_needs(trk, t) == [("ledger-verdict", second)]


def test_an_audit_or_verdict_stops_counting_once_its_session_builds_the_ticket(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "sound")
    reviewed(trk, t, sha)
    assert missing(trk, t) == []
    ok(trk, "claim", "release", t, *W)
    ok(trk, "claim", t, *V)
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "run-2", "--sha", sha,
       "--verdict", "pass", "--note", "green", *V)
    assert [m["need"] for m in missing(trk, t)] == ["audit", "ledger-verdict"]


def test_settle_flags_fit_the_settlement(trk, lane):
    t, _ = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "risky")
    for flags in (("deferred", "--sha", "abcdef1"), ("deferred", "--ask", "tr-aaaaa"),
                  ("overruled", "--ask", "tr-aaaaa", "--adr", "x.md"), ("fixed", "--sha", "abcdef1", "--adr", "x.md"),
                  ("settled",)):
        assert settle(trk, t, n, *flags).code == 2, flags


def test_deferring_a_row_with_no_verdict_is_refused(trk, lane):
    t, _ = lane["ticket"]()
    n = fork(trk, t)
    before = head(trk)
    assert settle(trk, t, n, "deferred").code == 2
    assert head(trk) == before


def test_the_builder_never_defers_its_own_row(trk, lane):
    t, _ = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "risky")
    before = head(trk)
    for who in (W, as_("builder", "X"), as_("reviewer", "W")):
        assert settle(trk, t, n, "deferred", who=who).error()["rule"] == "not-independent"
    assert head(trk) == before


def test_a_confirmed_flag_judged_refuted_without_a_fix_stays_unfixed(trk, lane):
    t, sha = lane["ticket"]()
    n = flag(trk, t)
    judge(trk, t, n, "confirmed")
    judge(trk, t, n, "refuted")
    reviewed(trk, t, sha)
    assert ledger_needs(trk, t) == [("reward-hack-fixed", n)]


def test_a_flag_refuted_from_the_start_needs_no_fix(trk, lane):
    t, sha = lane["ticket"]()
    n = flag(trk, t)
    judge(trk, t, n, "refuted")
    reviewed(trk, t, sha)
    assert ledger_needs(trk, t) == []


def test_a_wrong_row_judged_sound_after_debate_needs_no_settlement(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    judge(trk, t, n, "sound")
    reviewed(trk, t, sha)
    assert ledger_needs(trk, t) == []


def test_a_wrong_contradiction_is_overruled_through_its_ask_as_the_code_winning(trk, lane):
    t, sha = lane["ticket"]()
    n = contradiction(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    assert settle(trk, t, n, "accepted", "--ask", "tr-aaaaa").code == 2
    assert settle(trk, t, n, "overruled", "--ask", tied_ask(trk, t, n)).code == 0
    assert ledger_needs(trk, t) == []


def test_a_settlement_after_a_fix_does_not_hide_its_re_check(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "risky")
    reviewed(trk, t, sha)
    fix = fix_commit(trk, lane, t, "src/c.py")
    settle(trk, t, n, "fixed", "--sha", fix, who=W)
    assert settle(trk, t, n, "deferred").code == 0
    judge(trk, t, n, "sound", "--sha", sha)
    assert ledger_needs(trk, t) == [("ledger-settled", n)]


def test_a_verdict_git_cannot_place_is_sha_unverifiable(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "sound")
    judge(trk, t, n, "sound", "--sha", "deadbeef1234567")
    reviewed(trk, t, sha)
    path = lane["path"]
    assert [m for m in missing(trk, t) if "ledger" in m] == [
        {"need": "sha-unverifiable", "ledger": n, "sha": "deadbeef1234567", "checkout": path,
         "hint": f"deadbeef1234567 is unknown to {path}: run `git fetch` there, then retry"}]


# --- ticket ac-5: the GREEN lane ---

def test_a_green_ticket_with_ledger_rows_and_no_verdicts_still_merges(trk, lane):
    t, sha = lane["ticket"](("README.md",))
    fork(trk, t)
    fork(trk, t)
    assert ok(trk, "blast-radius", t, *W) == "green"
    merge(trk, t, sha)
    ok(trk, "advance", t)
    assert item(trk, t)["stage"] == "merged"


def test_a_skipped_merged_needs_no_review_of_its_ledger_rows(trk, lane):
    t = ok(trk, "new", "work-item", "T", "--form", "spec-bound", "--parent", lane["J"], *ticket("spec-bound"))
    sha = git(lane["path"], "rev-parse", "HEAD")
    ok(trk, "claim", t, *W)
    ok(trk, "evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", "none", "--sha", sha, "--note", "No PR",
       *W)
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "run-1", "--sha", sha,
       "--verdict", "pass", "--note", "green", *W)
    ok(trk, "advance", t, *W)
    fork(trk, t)
    judge(trk, t, "1", "wrong")
    ok(trk, "skip", t, "merged", "--reason", "No repo change.", *W)
    shown = item(trk, t)
    assert (shown["stage"], shown["merged_skipped"]) == ("merged", True)


# --- settle chooses its verdict by the gate's rule ---

def test_settle_works_from_the_verdict_at_the_head_not_a_later_one_at_a_descendant(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "risky")
    reviewed(trk, t, sha)
    descendant = commit(lane["path"], "src/later.py")
    judge(trk, t, n, "sound", "--sha", descendant)  # the latest by time, outside the head
    assert ledger_needs(trk, t) == [("ledger-settled", n)]
    ask = tied_ask(trk, t, n)
    assert settle(trk, t, n, "accepted", "--ask", ask).code == 0
    assert ledger_needs(trk, t) == []


def test_settle_names_a_verdict_outside_the_head_and_the_head_when_none_is_at_it(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    reviewed(trk, t, sha)
    descendant = commit(lane["path"], "src/later.py")
    judge(trk, t, n, "risky", "--sha", descendant)
    before = head(trk)
    refused = settle(trk, t, n, "accepted", "--ask", tied_ask(trk, t, n))
    assert refused.code == 2
    assert sha in refused.err and descendant in refused.err and "outside the head" in refused.err


# --- a fix's ancestry that git cannot tell is sha-unverifiable, not ledger-settled ---

def test_a_ticket_with_no_checkout_reports_sha_unverifiable_naming_the_fix_not_ledger_settled(trk, lane):
    path = lane["path"]
    program = trk("show", lane["J"], "--json").json()["item"]["parent"]
    j2 = ok(trk, "new", "project", "J2", "--parent", program)
    accept(trk, j2)
    t = ok(trk, "new", "work-item", "T2", "--form", "spec-bound", "--parent", j2, *ticket("spec-bound"))
    sha = commit(path, "src/n.py")
    build_at(trk, t, sha)
    ok(trk, "advance", t, *W)
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    base = git(path, "rev-parse", "main")
    ok(trk, "blast-radius", t, "--repo", path, "--base", base, *W)
    ok(trk, "ledger", "audit", t, "--note", "Read the diff", *V)
    fix = commit(path, "src/m.py")
    assert settle(trk, t, n, "fixed", "--sha", fix, who=W).code == 0
    later = commit(path, "src/o.py")
    build_at(trk, t, later)
    ok(trk, "blast-radius", t, "--repo", path, "--base", base, *W)
    judge(trk, t, n, "sound", "--sha", later)
    (need,) = [m for m in missing(trk, t) if "ledger" in m]
    assert (need["need"], need["ledger"], need["sha"]) == ("sha-unverifiable", n, fix)
    assert f"trk edit {t} --add-repo" in need["hint"] and "checkout" not in need  # none to name
    assert need["sha"] == fix
    ok(trk, "edit", t, "--add-repo", path)  # with a checkout it settles as before
    assert ledger_needs(trk, t) == []


# --- a rebase keeps the settlement of a fix whose patch is still in the head ---

def rebase_onto_new_main(lane):
    """Add a commit to main and rebase the feature branch onto it; returns the new head."""
    git(lane["path"], "checkout", "-q", "main")
    commit(lane["path"], "docs/new.md")
    git(lane["path"], "checkout", "-q", "feature")
    git(lane["path"], "rebase", "-q", "main")
    return git(lane["path"], "rev-parse", "HEAD")


def test_ac1_a_fix_settled_before_a_rebase_still_settles_the_row_when_its_patch_is_in_the_head(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    fix = fix_commit(trk, lane, t, "src/c.py")
    assert settle(trk, t, n, "fixed", "--sha", fix, who=W).code == 0
    new_head = rebase_onto_new_main(lane)
    assert new_head != fix
    build_at(trk, t, new_head)
    ok(trk, "blast-radius", t, *W)
    judge(trk, t, n, "sound", "--sha", new_head)
    assert ledger_needs(trk, t) == []


def test_ac2_a_fix_whose_patch_is_not_in_the_head_names_the_stranded_sha_and_the_re_settle_step(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    fix = fix_commit(trk, lane, t, "src/c.py")
    settle(trk, t, n, "fixed", "--sha", fix, who=W)
    judge(trk, t, n, "sound", "--sha", sha)
    need = [m for m in missing(trk, t) if m.get("ledger") == n]
    assert [m["need"] for m in need] == ["ledger-settled"]
    assert fix in need[0]["hint"] and "re-settle at its rebased copy and re-verdict" in need[0]["hint"]


# --- the patch-id check reads git as bytes: binary files and non-UTF-8 text in the range cannot crash it (tr-73das) ---

def commit_bytes(lane, name, data):
    """Commit `name` holding raw `data`; returns the new head sha."""
    path = lane["path"]
    with open(f"{path}/{name}", "wb") as handle:
        handle.write(data)
    git(path, "add", name)
    git(path, "commit", "-q", "-m", name)
    return git(path, "rev-parse", "HEAD")


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8
LATIN1 = "caf\xe9 \xfcber\n".encode("latin-1")


def test_ac1_ac2_a_rebased_fix_in_a_range_with_a_binary_and_a_non_utf8_file_still_settles(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    commit_bytes(lane, "src/logo.png", PNG)
    commit_bytes(lane, "src/legacy.txt", LATIN1)
    fix = fix_commit(trk, lane, t, "src/c.py")
    assert settle(trk, t, n, "fixed", "--sha", fix, who=W).code == 0
    new_head = rebase_onto_new_main(lane)
    assert new_head != fix
    build_at(trk, t, new_head)
    ok(trk, "blast-radius", t, *W)
    judge(trk, t, n, "sound", "--sha", new_head)
    assert ledger_needs(trk, t) == []
    assert trk("show", t, "--json").code == 0


def test_a_fix_that_is_itself_binary_is_matched_by_patch_id_after_a_rebase(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    fix = commit_bytes(lane, "src/icon.png", PNG)
    build_at(trk, t, fix)
    ok(trk, "blast-radius", t, *W)
    assert settle(trk, t, n, "fixed", "--sha", fix, who=W).code == 0
    new_head = rebase_onto_new_main(lane)
    build_at(trk, t, new_head)
    ok(trk, "blast-radius", t, *W)
    judge(trk, t, n, "sound", "--sha", new_head)
    assert ledger_needs(trk, t) == []


def test_a_fix_whose_patch_is_missing_is_still_stranded_with_binary_content_in_the_range(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    commit_bytes(lane, "src/logo.png", PNG)
    fix = fix_commit(trk, lane, t, "src/c.py")
    settle(trk, t, n, "fixed", "--sha", fix, who=W)
    judge(trk, t, n, "sound", "--sha", sha)
    need = [m for m in missing(trk, t) if m.get("ledger") == n]
    assert [m["need"] for m in need] == ["ledger-settled"]


def test_a_git_failure_on_the_patches_is_sha_unverifiable_not_a_crash(trk, lane, monkeypatch):
    from trk import gitrepo

    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    fix = fix_commit(trk, lane, t, "src/c.py")
    settle(trk, t, n, "fixed", "--sha", fix, who=W)
    new_head = rebase_onto_new_main(lane)
    build_at(trk, t, new_head)
    ok(trk, "blast-radius", t, *W)
    judge(trk, t, n, "sound", "--sha", new_head)
    monkeypatch.setattr(gitrepo, "_patch_ids", lambda repo, *argv: None)
    (need,) = [m for m in missing(trk, t) if "ledger" in m]
    assert (need["need"], need["ledger"], need["sha"]) == ("sha-unverifiable", n, fix)


def test_same_patch_in_answers_none_when_git_cannot_run(lane, monkeypatch):
    from trk import gitrepo

    head_sha = git(lane["path"], "rev-parse", "HEAD")
    first = commit(lane["path"], "src/x.py")

    real = gitrepo.subprocess.Popen

    def broken(argv, **kwargs):
        if "merge-base" in argv:
            return real(argv, **kwargs)
        raise OSError("no git")

    monkeypatch.setattr(gitrepo.subprocess, "Popen", broken)
    assert gitrepo.same_patch_in(lane["path"], first, head_sha) is None


# --- the gate names a verdict above the head, and a fix settled after the latest verdict (tr-7qtyt) ---

def test_ac1_a_verdict_above_the_head_is_named_with_the_head_and_the_fix(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    ok(trk, "blast-radius", t, *W)
    ok(trk, "ledger", "audit", t, "--note", "Read the diff", *V)
    above = commit(lane["path"], "src/later.py")  # the branch moved; only blast-radius would follow
    judge(trk, t, n, "sound", "--sha", above)
    (need,) = [m for m in missing(trk, t) if m.get("ledger") == n]
    assert (need["need"], need["sha"]) == ("ledger-verdict", above)
    assert sha in need["hint"] and above in need["hint"] and "pr and a passing test-run" in need["hint"]


def test_ac2_a_row_with_no_verdict_keeps_the_plain_ledger_verdict_need(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    ok(trk, "blast-radius", t, *W)
    assert [m for m in missing(trk, t) if m.get("ledger") == n] == [{"need": "ledger-verdict", "ledger": n}]


def test_ac3_a_row_re_settled_fixed_after_its_sound_verdict_says_the_auditor_must_re_verdict(trk, lane):
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    fix = fix_commit(trk, lane, t, "src/c.py")
    settle(trk, t, n, "fixed", "--sha", fix, who=W)
    judge(trk, t, n, "sound", "--sha", fix)
    assert ledger_needs(trk, t) == []
    again = fix_commit(trk, lane, t, "src/d.py")
    settle(trk, t, n, "fixed", "--sha", again, who=W)
    (need,) = [m for m in missing(trk, t) if m.get("ledger") == n]
    assert (need["need"], need["sha"]) == ("ledger-settled", again)
    assert "re-verdict after the new settlement" in need["hint"] and again in need["hint"]
    judge(trk, t, n, "sound", "--sha", again)
    assert ledger_needs(trk, t) == []


def test_a_re_verdict_above_the_head_names_the_builders_missing_pr_not_the_auditor(trk, lane):
    """UP-0009: the auditor re-verdicted at the new fix, but no pr row moved the head there: the builder acts."""
    t, sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    reviewed(trk, t, sha)
    fix = fix_commit(trk, lane, t, "src/c.py")
    settle(trk, t, n, "fixed", "--sha", fix, who=W)
    judge(trk, t, n, "sound", "--sha", fix)
    again = commit(lane["path"], "src/d.py")              # fixed again, with no pr row at it
    settle(trk, t, n, "fixed", "--sha", again, who=W)
    judge(trk, t, n, "sound", "--sha", again)
    (need,) = [m for m in missing(trk, t) if m.get("ledger") == n]
    assert need["sha"] == again and "no pr evidence at" in need["hint"] and "the builder records pr" in need["hint"]
    assert "the auditor must" not in need["hint"]


def test_a_test_run_off_the_head_says_why_it_does_not_count(trk, lane):
    t, sha = lane["ticket"]()
    above = commit(lane["path"], "src/e.py")
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "none", "--sha", above,
       "--verdict", "pass", "--note", "tests", *W)
    (row,) = [r for r in trk("evidence", t, "--json").json()["evidence"] if r["sha"] == above]
    assert row["counts"] is False and row["reason"] == "wrong-sha"
    assert sha[:7] in row["why"] and "the builder records pr there" in row["why"]
    assert "not counted (wrong-sha: its sha is not the head" in trk("evidence", t).out
