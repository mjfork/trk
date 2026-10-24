"""An auditor settles a contradiction row it raised once its re-verdict at the fix is sound, without an engineering
ask (tr-w40vn; spec 03, Ledger, "Amended 2026-10-22"). Michael, 2026-10-22: "the reviewer here should sign offf -
they found the mistkae?"

Every test runs against a real throwaway git repo (the `lane` fixture of the Merged gate tests). Sessions: W builds,
A and B are two auditor sessions writing as the actor `auditor`, V is a reviewer session writing as `reviewer`.
"""

import pytest

from gated import commit, git
from test_blast_and_verified import head, item, ok
from test_merged_review import (  # noqa: F401  (lane is a fixture)
    W, V, as_, contradiction, feed, fix_commit, fork, judge, lane, ledger_needs, reviewed, rows, settle, tied_ask)

A = as_("auditor", "A")
B = as_("auditor", "B")


@pytest.fixture(autouse=True)
def auditor_actor(trk, lane):
    ok(trk, "actor", "add", "auditor", "--kind", "agent")


def raised(trk, t, *who):
    """A contradiction row an auditor raised."""
    return ok(trk, "ledger", "add", t, "--kind", "contradiction", "--decided", "Two journal rows", "--why", "Simpler",
              "--conflicts-with", "spec:Evidence", "--origin", "auditor", *(who or A))


def signed_off(trk, lane, t):
    """An auditor row judged wrong at the head, fixed in a new commit and re-verdicted sound there by A.
    Returns (row, fix sha, the re-verdict's seq)."""
    n = raised(trk, t)
    judge(trk, t, n, "wrong", who=A)
    fix = fix_commit(trk, lane, t, "src/c.py")
    judge(trk, t, n, "sound", "--sha", fix, who=A)
    return n, fix, head(trk)


def last_settled(trk, since):
    (settled,) = [c for c in feed(trk, since) if c["change"] == "ledger.settled"]
    return settled["data"]


# --- ac-1: the auditor settles with no ask, and the settle cites the verdict ---

def test_ac1_an_auditor_settles_the_contradiction_it_raised_with_no_ask_and_cites_the_verdict(trk, lane):
    t, _sha = lane["ticket"]()
    n, fix, verdict_seq = signed_off(trk, lane, t)
    since = head(trk)
    assert settle(trk, t, n, "fixed", "--sha", fix, who=B).code == 0
    data = last_settled(trk, since)
    assert (data["settlement"], data["sha"], data["ask"]) == ("fixed", fix, None)
    assert (data["verdict_seq"], data["verdict_sha"]) == (verdict_seq, fix)
    assert rows(trk, t)[0]["settlement"]["ask"] is None


# --- ac-2: what is refused, naming the first missing condition and the --ask route ---

def refused_with(trk, t, n, fix, who, rule, *words):
    before = head(trk)
    error = settle(trk, t, n, "fixed", "--sha", fix, who=who).error()
    assert error["rule"] == rule, error
    for word in words:
        assert word in error["message"], (word, error["message"])
    assert head(trk) == before  # nothing written


def test_ac2_a_settle_by_anyone_but_the_actor_auditor_is_refused(trk, lane):
    t, _sha = lane["ticket"]()
    n, fix, _seq = signed_off(trk, lane, t)
    refused_with(trk, t, n, fix, as_("worker", "X"), "settle-needs-answer", "auditor", "--ask")
    refused_with(trk, t, n, fix, V, "settle-needs-answer", "auditor", "--ask")


def test_ac2_a_builder_never_settles_its_own_contradiction(trk, lane):
    t, _sha = lane["ticket"]()
    n, fix, _seq = signed_off(trk, lane, t)
    refused_with(trk, t, n, fix, as_("auditor", "W"), "not-independent", "built")
    refused_with(trk, t, n, fix, as_("builder", "W"), "settle-needs-answer", "auditor", "--ask")


def test_ac2_the_settle_needs_a_session(trk, lane):
    t, _sha = lane["ticket"]()
    n, fix, _seq = signed_off(trk, lane, t)
    refused_with(trk, t, n, fix, ("--as", "auditor"), "session-required")


def test_ac2_a_contradiction_a_builder_logged_still_needs_its_ask(trk, lane):
    t, _sha = lane["ticket"]()
    n = contradiction(trk, t)
    judge(trk, t, n, "wrong", who=A)
    fix = fix_commit(trk, lane, t, "src/c.py")
    judge(trk, t, n, "sound", "--sha", fix, who=A)
    refused_with(trk, t, n, fix, B, "settle-needs-answer", "auditor", "--ask")


def test_ac2_no_sound_verdict_at_the_fix_is_refused_naming_what_is_missing(trk, lane):
    t, _sha = lane["ticket"]()
    n = raised(trk, t)
    judge(trk, t, n, "wrong", who=A)
    fix = fix_commit(trk, lane, t, "src/c.py")
    refused_with(trk, t, n, fix, B, "settle-needs-answer", "sound", "--ask")


def test_ac2_a_later_risky_or_wrong_verdict_blocks_the_settle(trk, lane):
    t, _sha = lane["ticket"]()
    n, fix, _seq = signed_off(trk, lane, t)
    judge(trk, t, n, "risky", "--sha", fix, who=A)
    refused_with(trk, t, n, fix, B, "settle-needs-answer", "sound", "--ask")


def test_ac2_a_sound_verdict_that_does_not_hold_the_fix_is_refused(trk, lane):
    t, sha = lane["ticket"]()
    n = raised(trk, t)
    judge(trk, t, n, "sound", "--sha", sha, who=A)
    fix = fix_commit(trk, lane, t, "src/c.py")
    refused_with(trk, t, n, fix, B, "settle-needs-answer", fix[:7], "--ask")


def test_ac2_a_sound_verdict_by_another_actor_does_not_count(trk, lane):
    t, _sha = lane["ticket"]()
    n = raised(trk, t)
    judge(trk, t, n, "wrong", who=A)
    fix = fix_commit(trk, lane, t, "src/c.py")
    judge(trk, t, n, "sound", "--sha", fix, who=V)
    refused_with(trk, t, n, fix, B, "settle-needs-answer", "auditor", "--ask")


def test_ac2_a_fix_sha_git_cannot_place_is_sha_unverifiable(trk, lane):
    t, _sha = lane["ticket"]()
    n, _fix, _seq = signed_off(trk, lane, t)
    refused_with(trk, t, n, "0123456789abcdef0123456789abcdef01234567", B, "sha-unverifiable")


# --- ac-3: asks and fork rows behave as before ---

def test_ac3_an_answered_engineering_ask_still_settles_a_contradiction_and_the_cite_is_null(trk, lane):
    t, _sha = lane["ticket"]()
    n = raised(trk, t)
    judge(trk, t, n, "wrong", who=A)
    fix = fix_commit(trk, lane, t, "src/c.py")
    since = head(trk)
    assert settle(trk, t, n, "fixed", "--sha", fix, "--ask", tied_ask(trk, t, n), who=B).code == 0
    data = last_settled(trk, since)
    assert (data["ask"] is not None, data["verdict_seq"], data["verdict_sha"]) == (True, None, None)


def test_ac3_a_fork_row_is_settled_as_before_and_the_cite_is_null(trk, lane):
    t, _sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong")
    since = head(trk)
    assert settle(trk, t, n, "fixed", "--sha", "fedcba7654321", who=W).code == 0
    data = last_settled(trk, since)
    assert (data["settlement"], data["ask"], data["verdict_seq"], data["verdict_sha"]) == ("fixed", None, None, None)


def test_ac3_a_fork_row_needs_no_ask_even_when_an_auditor_settles_it(trk, lane):
    t, _sha = lane["ticket"]()
    n = fork(trk, t)
    judge(trk, t, n, "wrong", who=A)
    assert settle(trk, t, n, "fixed", "--sha", "fedcba7654321", who=B).code == 0


# --- ac-5: the Merged gate ---

def test_ac5_a_settled_contradiction_passes_merged_without_a_further_verdict(trk, lane):
    t, _sha = lane["ticket"]()
    n, fix, _seq = signed_off(trk, lane, t)
    reviewed(trk, t, fix)
    assert ledger_needs(trk, t) == [("ledger-settled", n)]
    assert settle(trk, t, n, "fixed", "--sha", fix, who=B).code == 0
    assert ledger_needs(trk, t) == []
    git(lane["path"], "checkout", "-q", "main")
    git(lane["path"], "merge", "-q", "--no-ff", "feature", "-m", "merge")
    ok(trk, "advance", t, *W)
    assert item(trk, t)["stage"] == "merged"


def test_ac5_a_later_risky_or_wrong_verdict_blocks_merged_again(trk, lane):
    t, _sha = lane["ticket"]()
    n, fix, _seq = signed_off(trk, lane, t)
    reviewed(trk, t, fix)
    assert settle(trk, t, n, "fixed", "--sha", fix, who=B).code == 0
    assert ledger_needs(trk, t) == []
    judge(trk, t, n, "wrong", "--sha", fix, who=A)
    assert ledger_needs(trk, t) == [("ledger-settled", n)]


def test_ac5_a_settle_through_an_ask_still_waits_for_its_re_check(trk, lane):
    t, _sha = lane["ticket"]()
    n = raised(trk, t)
    judge(trk, t, n, "wrong", who=A)
    reviewed(trk, t, _sha)
    fix = fix_commit(trk, lane, t, "src/c.py")
    assert settle(trk, t, n, "fixed", "--sha", fix, "--ask", tied_ask(trk, t, n), who=B).code == 0
    assert ledger_needs(trk, t) == [("ledger-settled", n)]
    judge(trk, t, n, "sound", "--sha", fix, who=A)
    assert ledger_needs(trk, t) == []
