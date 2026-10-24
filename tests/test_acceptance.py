"""The spec's acceptance criteria 1-9, end to end through the `trk` on PATH, each against a fresh store.

AC 5 and AC 6 continue from AC 4's store, and AC 7 from AC 6's, as the spec states them; each test
rebuilds that chain on its own fresh store.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from feed_contract import feed_validator
from installed_trk import MISSING, T0, TRK, Trk
from gated import DEST, resolving
from timing import assert_fast

WORKER = Path(__file__).with_name("concurrency_worker.py")

pytestmark = pytest.mark.skipif(not TRK, reason=MISSING)


@pytest.fixture
def trk_bin(trk_home):
    return Trk()


def frontier(trk_bin, root):
    return [item["id"] for item in trk_bin.json("ready", "--root", root)["items"]]


def test_ac1_init_registers_michael_and_herdr_and_refuses_a_second_init(trk_bin):
    trk_bin.ok("init", "--me", "michael")
    assert [(a["id"], a["kind"]) for a in trk_bin.json("actors")["actors"]] == [("michael", "human"), ("herdr", "agent"),
                                                                                   ("publisher", "agent")]

    code, error = trk_bin.error("init", "--me", "michael")
    assert (code, error["rule"]) == (1, "store-exists")


def test_ac2_nesting_cycle_and_parent_closed_refusals(trk_bin):
    trk_bin.ok("init", "--me", "michael")
    program = trk_bin.ok("new", "program", "P", *DEST)
    project = trk_bin.ok("new", "project", "J", "--parent", program)
    work_item = trk_bin.decision("W", project)

    code, error = trk_bin.error("new", "project", "X", "--parent", work_item)
    assert (code, error["rule"]) == (1, "nesting")

    sub_program = trk_bin.ok("new", "program", "P2", "--parent", program, *DEST)
    code, error = trk_bin.error("move", program, "--parent", sub_program)
    assert (code, error["rule"]) == (1, "cycle")

    for argv in resolving(work_item):
        trk_bin.ok(*argv)
    trk_bin.ok("close", work_item, "--resolution", "done", "--outcome", "Settled.")
    code, error = trk_bin.error("new", "work-item", "Y", "--form", "decision", "--subtype", "task", "--parent", work_item)
    assert (code, error["rule"], error["item"]) == (1, "parent-closed", work_item)


def test_ac3_a_blocks_cycle_is_refused(trk_bin):
    trk_bin.ok("init", "--me", "michael")
    program = trk_bin.ok("new", "program", "P", *DEST)
    a, b, c = (trk_bin.decision(title, program) for title in "ABC")
    trk_bin.ok("link", a, "blocks", b)
    trk_bin.ok("link", b, "blocks", c)

    code, error = trk_bin.error("link", c, "blocks", a)
    assert (code, error["rule"]) == (1, "cycle")


def ac4(trk_bin):
    """AC 4's store: program P over project J, decision D blocked by decision E, and urgent decision U."""
    trk_bin.ok("init", "--me", "michael")
    p = trk_bin.ok("new", "program", "P", *DEST)
    j = trk_bin.ok("new", "project", "J", "--parent", p, "--last")
    d = trk_bin.decision("D", p, "--last")
    e = trk_bin.decision("E", p, "--last")
    u = trk_bin.decision("U", p, "--last", "--urgent")
    trk_bin.ok("link", e, "blocks", d)
    return {"P": p, "J": j, "D": d, "E": e, "U": u}


def test_ac4_ready_is_urgent_first_then_tree_order(trk_bin):
    ids = ac4(trk_bin)
    assert frontier(trk_bin, ids["P"]) == [ids["U"], ids["J"], ids["E"]]

    trk_bin.ok("close", ids["E"], "--resolution", "wont-do", "--outcome", "Not needed after all.")
    assert frontier(trk_bin, ids["P"]) == [ids["U"], ids["J"], ids["D"]]


def ac5(trk_bin):
    """AC 5 from AC 4: S1's claim on D lapses and S2 takes it."""
    ids = ac4(trk_bin)
    trk_bin.ok("close", ids["E"], "--resolution", "wont-do", "--outcome", "Not needed after all.")
    lease_until = trk_bin.json("claim", ids["D"], "--session", "S1")["claimed"]["claim"]["lease_until"]

    code, error = trk_bin.error("claim", ids["D"], "--session", "S2")
    assert (code, error["rule"]) == (4, "claimed")

    head = trk_bin.feed()["head"]
    trk_bin.now = T0 + timedelta(minutes=61)
    trk_bin.ok("show", ids["P"])  # any command sweeps
    expired = [row for row in trk_bin.feed(head)["changes"] if row["change"] == "claim.expired"]
    assert [(row["item"], row["session"], row["at"]) for row in expired] == [(ids["D"], "S1", lease_until)]

    trk_bin.ok("claim", ids["D"], "--session", "S2")
    return ids


def test_ac5_a_lapsed_claim_is_swept_and_another_session_claims(trk_bin):
    ids = ac5(trk_bin)
    assert [(c["item"], c["session"]) for c in trk_bin.json("claims")["claims"]] == [(ids["D"], "S2")]


def ac6(trk_bin):
    """AC 6 from AC 5: refusals on close, then D superseded by D2 while D blocks X."""
    ids = ac5(trk_bin)
    assert trk_bin("close", ids["D"], "--resolution", "done").returncode == 2

    code, error = trk_bin.error("close", ids["P"], "--resolution", "done", "--outcome", "All done.")
    assert (code, error["rule"], error["item"]) == (1, "open-children", ids["P"])
    for open_child in ("J", "D", "U"):
        assert ids[open_child] in error["message"]

    ids["D2"] = trk_bin.decision("D2", ids["P"])
    ids["X"] = trk_bin.decision("X", ids["P"])
    trk_bin.ok("link", ids["D"], "blocks", ids["X"])
    head = trk_bin.feed()["head"]
    trk_bin.ok("close", ids["D"], "--resolution", "superseded", "--by", ids["D2"], "--outcome", "Replaced by D2.")
    return ids, trk_bin.feed(head)["changes"]


def test_ac6_close_refusals_and_supersede_moves_edges_and_ends_the_claim(trk_bin):
    ids, rows = ac6(trk_bin)
    d, d2, x = ids["D"], ids["D2"], ids["X"]

    assert trk_bin.json("show", x)["item"]["why_not"] == [f"blocked-by:{d2}"]
    assert {"dir": "out", "type": "supersedes", "id": d} in [
        {k: e[k] for k in ("dir", "type", "id")} for e in trk_bin.json("show", d2)["item"]["edges"]
    ]
    assert [(r["change"], r["data"].get("type"), r["data"].get("src"), r["data"].get("dst")) for r in rows
            if r["change"].startswith("edge.")] == [
        ("edge.added", "supersedes", d2, d),
        ("edge.removed", "blocks", d, x),
        ("edge.added", "blocks", d2, x),
    ]
    released = [r for r in rows if r["change"] == "claim.released"]
    assert [(r["item"], r["session"], r["data"]["reason"]) for r in released] == [(d, "S2", "closed")]


def test_ac7_the_feed_replays_everything_and_filters_by_root(trk_bin, trk_home):
    ids, _ = ac6(trk_bin)
    trk_bin.decision("Q1", trk_bin.ok("new", "program", "Q", *DEST))

    replay = trk_bin.feed()
    seqs = [row["seq"] for row in replay["changes"]]
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        journal_seqs = [seq for (seq,) in conn.execute("SELECT seq FROM journal ORDER BY seq")]
    finally:
        conn.close()
    assert seqs == journal_seqs and seqs == sorted(set(seqs))
    assert list(feed_validator().iter_errors(replay)) == []

    head = replay["head"]
    assert head == seqs[-1]
    caught_up = trk_bin.feed(head)
    assert (caught_up["changes"], caught_up["next"]) == ([], head)

    rooted = trk_bin.feed(0, "--root", ids["P"])["changes"]
    assert rooted and all(row["root"] == ids["P"] for row in rooted)  # so none of Q's


def test_ac8_the_journal_refuses_a_delete(trk_bin, trk_home):
    trk_bin.ok("init", "--me", "michael")
    db = trk_home / "store.db"
    if shutil.which("sqlite3"):
        done = subprocess.run(["sqlite3", str(db), "delete from journal"], capture_output=True, text=True, check=False)
        assert done.returncode != 0 and "journal is append-only" in done.stderr
    else:  # the sqlite3 shell is not installed here; the same trigger through Python's sqlite3
        conn = sqlite3.connect(db)
        with pytest.raises(sqlite3.IntegrityError, match="journal is append-only"):
            conn.execute("delete from journal")
        conn.close()
    assert trk_bin.feed()["changes"]


WORKERS, WRITES = 8, 50


@pytest.mark.parametrize("mode", ["binary", "main"])
def test_ac9_eight_processes_of_fifty_mixed_writes_lose_nothing(trk_bin, trk_home, tmp_path, mode):
    """Half of AC 9, with asks (spec 02's end-to-end ticket): odd writers open, readdress and answer asks.

    `binary` runs each write as an installed `trk` process; `main` loops in-process, contending harder.
    """
    trk_bin.ok("init", "--me", "michael")
    trk_bin.ok("actor", "add", "bot", "--kind", "agent")
    program = trk_bin.ok("new", "program", "P", *DEST)
    setup_head = trk_bin.feed()["head"]
    section = tmp_path / "question.md"
    section.write_text("What does this worker settle?\n")

    env = {k: v for k, v in os.environ.items() if k != "TRK_NOW"}  # real clock: leases stay live
    workers = [
        subprocess.Popen([sys.executable, str(WORKER), mode, str(n), program, str(section)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for n in range(WORKERS)
    ]
    outcomes = [(w.wait(timeout=300), w.stderr.read()) for w in workers]
    for w in workers:
        w.stdout.close()
        w.stderr.close()
    assert outcomes == [(0, "")] * WORKERS  # no write failed, `database is locked` included

    rows = trk_bin.feed(setup_head, "--limit", "1000")["changes"]
    seqs = [row["seq"] for row in rows]
    assert len(rows) == WORKERS * WRITES
    assert seqs == sorted(set(seqs))
    assert trk_bin.feed()["head"] == seqs[-1] == int((trk_home / "feed.head").read_text())

    decision_cycle = ["item.created", "item.updated", "item.updated", "edge.added", "claim.taken"]
    ask_cycle = ["item.created", "ask.opened", "ask.readdressed", "ask.answered", "claim.taken"]
    assert len(ask_cycle) == len(decision_cycle)
    cycles = WRITES // len(decision_cycle)
    for n in range(WORKERS):
        mine = [row for row in rows if row["session"] == f"w{n}"]
        assert [row["change"] for row in mine] == (ask_cycle if n % 2 else decision_cycle) * cycles  # in order

    children = trk_bin.json("show", program)["item"]["children"]
    assert sorted(child["title"] for child in children) == sorted(
        f"w{n}-{c} {'asked' if n % 2 else 'edited'}" for n in range(WORKERS) for c in range(cycles)
    )
    assert len(trk_bin.json("claims")["claims"]) == len(children)
    edited = next(child["id"] for child in children if child["title"].endswith("edited"))
    shown = trk_bin.json("show", edited)["item"]
    assert [s["body"] for s in shown["sections"] if s["handle"] == "question"] == ["What does this worker settle?\n"]

    # Every ask was opened by bot, readdressed to engineering and answered by michael, and none is left open.
    answered = [row for row in rows if row["change"] == "ask.answered"]
    assert len(answered) == WORKERS // 2 * cycles
    assert {(r["actor"], r["addressee"], r["data"]["addressee_role"], r["data"]["proxy"]) for r in answered} == {
        ("michael", "michael", "engineering", False),
    }
    assert trk_bin.json("waiting", "--all")["asks"] == []
    for child in children:
        if child["title"].endswith("asked"):
            item = trk_bin.json("show", child["id"])["item"]
            assert item["open_asks"] == [] and not any(w.startswith("open-ask:") for w in item["why_not"])
            assert [(c["form"], c["status"]) for c in item["children"]] == [("ask", "closed")]


def _store_of(count):
    """A store of `count` items, bulk-inserted: a program over projects of 100 decisions each."""
    from trk import store

    st = store.create("tr", "michael", None)
    now = "2026-10-10T12:00:00.000Z"
    rows = [("tr-p0000", "program", None, None, None, None)]
    project = None
    for n in range(1, count):
        if n % 101 == 1:
            project = f"tr-j{n:04d}"
            rows.append((project, "project", None, None, "tr-p0000", "draft"))
        else:
            rows.append((f"tr-d{n:04d}", "work_item", "decision", "task", project, None))
    try:
        with st.write_lock():
            st.conn.executemany(
                "INSERT INTO items (id, kind, form, subtype, title, parent_id, rank, spec_state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(i, kind, form, sub, i, parent, f"V{n:05d}", spec, now, now)
                 for n, (i, kind, form, sub, parent, spec) in enumerate(rows)],
            )
    finally:
        st.close()


def test_ac9_renew_for_an_unknown_session_is_fast_on_5000_items(trk_bin):
    """Half of AC 9: the per-turn hook's renew stays under 150 ms wall clock on a 5 000-item store.

    On a loaded host, renew may instead take at most 1.25 times as long as `trk actors` on the same store (timing.py).
    """
    _store_of(5000)

    def renew():
        result = trk_bin("claim", "renew", "--session", "no-such-session")
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")

    def actors():
        assert trk_bin("actors", "--json").returncode == 0

    assert_fast(renew, actors, 0.150)
