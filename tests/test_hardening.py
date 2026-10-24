"""Invariants over a store that writes every change kind Core, Asks, Handoffs and Stage gates have, AC 7 in full, and the lock timeout."""

import json
import os
import sqlite3
import subprocess
import time
from datetime import timedelta

import pytest

from feed_contract import CONTRACT, feed_validator
from installed_trk import MISSING, T0, TRK, Trk
from trk import store
from gated import DEST, accepting, resolving, ticket
from store_note import without_note

CORE_KINDS = {
    "item.created", "item.updated", "item.closed", "item.reopened", "item.moved", "owner.changed",
    "claim.taken", "claim.renewed", "claim.released", "claim.expired", "spec.changed",
    "edge.added", "edge.removed", "actor.added", "actor.updated",
}
ASKS_AND_OWNERS_KINDS = {
    "ask.opened", "ask.readdressed", "ask.answered", "ask.withdrawn", "ask.reopened", "role.set", "config.changed",
}
HANDOFF_KINDS = {"handoff.added"}
GATES_KINDS = {"stage.changed", "criterion.added", "criterion.edited", "criterion.retired", "evidence.added",
               "ledger.added", "ledger.verdict", "ledger.settled", "ledger.promoted", "ledger.withdrawn"}
# Written only by a publish or a comment pull, which need the in-process fake service the subprocess scenario
# can't reach: tests/test_pages_flush.py and tests/test_pages_replies.py write them, and the suite-wide feed
# check validates every row they write.
PAGES_KINDS = {"page.created", "ask.replied", "comment.routed", "comment.acknowledged"}


def journal_seqs(home):
    conn = sqlite3.connect(home / "store.db")
    try:
        return [seq for (seq,) in conn.execute("SELECT seq FROM journal ORDER BY seq")]
    finally:
        conn.close()


@pytest.fixture
def every_kind(trk_home, tmp_path):
    """Build a store through every change kind with the installed binary, checking feed.head = head after each write."""
    assert TRK, MISSING
    trk_bin = Trk()
    question = tmp_path / "question.md"
    question.write_text("Which way?\n")

    def at(minutes):
        trk_bin.now = T0 + timedelta(minutes=minutes)

    def write(*argv, stdin=None):
        out = trk_bin.ok(*argv, stdin=stdin)
        assert int((trk_home / "feed.head").read_text()) == journal_seqs(trk_home)[-1], argv
        return out

    at(0)
    write("init", "--me", "michael")
    write("actor", "add", "bob", "--kind", "agent")
    write("actor", "edit", "bob", "--display", "Bob")
    p = write("new", "program", "P", *DEST)
    q = write("new", "program", "Q", *DEST)
    j = write("new", "project", "J", "--parent", p)
    d = write("new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", j)
    e = write("new", "work-item", "E", "--form", "decision", "--subtype", "research", "--parent", j)
    t = write("new", "work-item", "T", "--form", "spec-bound", "--parent", j, *ticket("spec-bound"))
    write("criterion", "add", t, "It is fast.")
    write("criterion", "edit", t, "ac-2", "It is fast enough.")
    write("criterion", "retire", t, "ac-1", "--reason", "Folded into ac-2.")
    wrong = write("evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", "pr-1", "--sha", "abcdef1",
                  "--note", "Opened the PR.")
    write("evidence", "retract", t, wrong, "--note", "Wrong PR.")
    s = write("new", "work-item", "S", "--form", "self-contained", "--parent", j, *ticket("self-contained"))
    write("advance", s, "--judgment", "The repro is complete.")
    for argv in accepting(j):
        write(*argv)
    write("claim", t, "--session", "s0")
    write("ledger", "add", t, "--kind", "fork", "--decided", "Kept the cache", "--alternative", "Dropped it",
          "--why", "Reads are hot", "--session", "s0")
    write("ledger", "verdict", t, "1", "wrong", "--note", "It goes stale.", "--as", "bob", "--session", "s9")
    write("ledger", "settle", t, "1", "fixed", "--sha", "abcdef2", "--session", "s0")
    write("ledger", "promote", t, "1", "--adr", "docs/adr/0003-cache.md")
    write("ledger", "add", t, "--kind", "fork", "--decided", "Typed twice", "--alternative", "Once", "--why", "Slip",
          "--session", "s0")
    write("ledger", "withdraw", t, "2", "--reason", "Written twice by mistake.")
    write("config", "set", "template-parent", j)
    write("ledger", "add", s, "--kind", "fork", "--decided", "Guessed m3/h", "--alternative", "l/s", "--why", "Usual",
          "--origin", "auditor", "--repeat-of", f"{t}:1", "--topic", "units", "--as", "bob", "--session", "s9")
    write("edit", d, "--title", "D renamed")
    write("section", "set", d, "question", "--file", str(question))
    write("handoff", "add", d, "--session", "s0", stdin="Stopped after the question.\n")
    write("edit", d, "--owner", "bob")
    write("actor", "add", "kim", "--kind", "human")
    write("config", "set", "default-owner", "kim")
    write("roles", "set", "product", "kim")
    a = write("ask", d, "--to", "product", "Which unit?", "--question", "-", "--as", "bob", stdin="Which unit?")
    write("readdress", a, "--to", "engineering", "--as", "bob")
    write("answer", a, "--outcome", "m3/h", "--answer", "-", stdin="Cubic metres per hour.")
    write("reopen", a, "--reason", "Which flow, though?", "--as", "bob")
    write("withdraw", a, "--reason", "Settled elsewhere.", "--as", "bob")
    write("link", e, "blocks", d)
    write("unlink", e, "blocks", d)
    write("move", e, "--first")
    write("move", e, "--parent", q)
    write("claim", d, "--session", "s1")
    at(40)  # past half the 60-minute lease
    write("claim", "renew", "--session", "s1")
    at(200)  # past the renewed lease: this read sweeps it
    write("show", d)
    write("claim", d, "--session", "s2")
    write("claim", "release", "--session", "s2")
    for argv in resolving(e):
        write(*argv)
    write("close", e, "--resolution", "done", "--outcome", "Researched.")
    write("reopen", e)
    return {"P": p, "Q": q, "J": j, "D": d, "E": e, "T": t}


def test_the_store_writes_every_core_asks_handoffs_and_gates_change_kind(trk, every_kind):
    rows = trk("changes", "--since", "0", "--json").json()["changes"]
    assert CORE_KINDS | ASKS_AND_OWNERS_KINDS | HANDOFF_KINDS | GATES_KINDS <= {row["change"] for row in rows}


def test_the_kinds_the_scenario_must_write_include_every_kind_the_contract_shapes():
    contract = json.loads((CONTRACT / "changes.schema.json").read_text())
    matches = [branch["if"]["properties"]["change"] for branch in contract["$defs"]["kinds"]["allOf"]]
    shaped = {kind for match in matches for kind in match.get("enum", [match.get("const")])}
    assert shaped <= CORE_KINDS | ASKS_AND_OWNERS_KINDS | HANDOFF_KINDS | GATES_KINDS | PAGES_KINDS


def test_ac7_since_zero_replays_every_row_from_init_ascending_and_valid(trk_home, every_kind):
    trk_bin = Trk()
    replay = trk_bin.feed()
    seqs = [row["seq"] for row in replay["changes"]]
    assert seqs == journal_seqs(trk_home)  # init's rows included
    assert seqs == sorted(set(seqs))
    assert replay["head"] == replay["next"] == seqs[-1]
    errors = list(feed_validator().iter_errors(replay))
    assert not errors, [e.message for e in errors]

    paged, cursor = [], 0
    while True:
        page = trk_bin.feed(cursor, "--limit", "4")
        if not page["changes"]:
            break
        paged += page["changes"]
        cursor = page["next"]
    assert paged == replay["changes"]


@pytest.mark.parametrize("statement", ["DELETE FROM journal WHERE seq = 1", "UPDATE journal SET actor = 'bob'"])
def test_the_journal_refuses_update_and_delete(trk_home, every_kind, statement):
    before = journal_seqs(trk_home)
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="journal is append-only"):
            conn.execute(statement)
    finally:
        conn.close()
    assert journal_seqs(trk_home) == before


@pytest.fixture
def held_write_lock(trk, trk_home):
    """Another writer holds the store's write lock until the test ends."""
    trk("init", "--me", "michael")
    conn = sqlite3.connect(trk_home / "store.db", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    yield
    conn.execute("ROLLBACK")
    conn.close()


def test_a_write_past_the_busy_timeout_exits_4(held_write_lock):
    assert TRK, MISSING
    trk_bin = TRK
    env = {**os.environ, "TRK_BUSY_TIMEOUT_MS": "300"}
    start = time.perf_counter()
    done = without_note(subprocess.run([trk_bin, "new", "program", "X", "--json"], capture_output=True, text=True,
                                       env=env, check=False))
    elapsed = time.perf_counter() - start

    assert done.returncode == 4
    error = json.loads(done.stderr)["error"]
    assert (error["code"], error["rule"]) == ("conflict", "locked")
    assert "300 ms" in error["message"] and "write lock" in error["message"]
    assert 0.3 <= elapsed < 5

    plain = without_note(subprocess.run([trk_bin, "new", "program", "X"], capture_output=True, text=True, env=env,
                                        check=False))
    assert plain.returncode == 4
    assert plain.stderr.count("\n") == 1 and plain.stderr.startswith("trk: ") and "[locked]" in plain.stderr


def test_a_read_is_not_held_up_by_the_write_lock(trk, held_write_lock, monkeypatch):
    monkeypatch.setenv("TRK_BUSY_TIMEOUT_MS", "100")
    assert trk("actors", "--json").code == 0


def test_the_busy_timeout_override_needs_trk_test(monkeypatch):
    monkeypatch.setenv("TRK_BUSY_TIMEOUT_MS", "250")
    assert store.busy_timeout_ms() == 250
    monkeypatch.delenv("TRK_TEST")
    assert store.busy_timeout_ms() == store.BUSY_TIMEOUT_MS == 10_000


@pytest.mark.parametrize("value", ["0", "-5", "soon"])
def test_a_bad_busy_timeout_override_is_a_usage_error(trk, monkeypatch, value):
    trk("init", "--me", "michael")
    monkeypatch.setenv("TRK_BUSY_TIMEOUT_MS", value)
    result = trk("new", "program", "X", *DEST)
    assert result.code == 2 and "TRK_BUSY_TIMEOUT_MS" in result.err


def test_a_lock_timeout_outside_begin_immediate_still_exits_4(trk, monkeypatch):
    def gives_up():
        raise sqlite3.OperationalError("database is locked")

    trk("init", "--me", "michael")
    monkeypatch.setattr(store, "open_store", gives_up)
    result = trk("new", "program", "X", "--json", *DEST)
    assert result.code == 4 and result.error()["rule"] == "locked"


def test_another_operational_error_outside_a_write_is_refused_under_rule_sqlite(trk, monkeypatch):
    def fails():
        raise sqlite3.OperationalError("disk I/O error")

    trk("init", "--me", "michael")
    monkeypatch.setattr(store, "open_store", fails)
    result = trk("new", "program", "X", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "sqlite" and "disk I/O error" in result.error()["message"]
    human = trk("new", "program", "X")
    assert human.code == 1 and human.err.rstrip().endswith("[sqlite]") and "Traceback" not in human.err


@pytest.mark.feed_unchecked
def test_a_corrupt_store_is_refused_under_rule_sqlite_without_a_traceback(trk, trk_home):
    trk_home.mkdir()
    (trk_home / "store.db").write_bytes(b"not a sqlite database, just garbage " * 200)
    result = trk("ready", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "sqlite" and "trk backup" in result.error()["message"]
    human = trk("ready")
    assert human.code == 1 and human.err.rstrip().endswith("[sqlite]") and "Traceback" not in human.err
