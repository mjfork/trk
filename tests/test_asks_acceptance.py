"""Spec 02 (Asks and owners) acceptance criteria 1-9, end to end through the `trk` on PATH, each against a fresh store.

Every store starts as the spec says: `trk init --me michael`, then `kim` (human) and `worker` (agent).
AC 4 and AC 5 continue from AC 3's store, AC 6 from AC 5's, AC 7 from AC 6's and AC 8 from AC 7's, as
the spec stages them; each test rebuilds that chain on its own fresh store. AC 10 is herdr-extension's.
"""

import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from feed_contract import feed_validator
from installed_trk import MISSING, T0, TRK, Trk
from gated import DEST, resolving
from trk import store

pytestmark = pytest.mark.skipif(not TRK, reason=MISSING)

FIXTURE = Path(__file__).parent / "fixtures" / "schema1"
FIXTURE_OPENED_AT = "2026-10-10T14:30:00.000Z"  # after the fixture's sweep, inside its live lease


@pytest.fixture
def trk_bin(trk_home):
    return Trk()


def fresh(trk_bin):
    trk_bin.ok("init", "--me", "michael")
    trk_bin.ok("actor", "add", "kim", "--kind", "human")
    trk_bin.ok("actor", "add", "worker", "--kind", "agent")


def frontier(trk_bin, root):
    return [item["id"] for item in trk_bin.json("ready", "--root", root)["items"]]


def rows_of(trk_bin, since, change):
    return [row for row in trk_bin.feed(since)["changes"] if row["change"] == change]


def ask(trk_bin, parent, role, title, *flags, actor="worker", question="Which unit for flow?"):
    return trk_bin.ok("ask", parent, "--to", role, title, "--question", "-", "--as", actor, *flags, stdin=question)


def test_ac1_roles_are_seeded_set_and_parent_is_reserved(trk_bin):
    fresh(trk_bin)
    assert [(r["role"], r["actor"]) for r in trk_bin.json("roles")["roles"]] == [
        ("engineering", "michael"), ("product", "michael"), ("publisher", "michael"),
    ]

    head = trk_bin.feed()["head"]
    trk_bin.ok("roles", "set", "product", "kim")
    assert [r["data"] for r in rows_of(trk_bin, head, "role.set")] == [{"role": "product", "old": "michael", "new": "kim"}]

    code, error = trk_bin.error("roles", "set", "parent", "kim")
    assert (code, error["rule"]) == (1, "reserved-role")


def test_ac2_top_level_items_take_the_default_owner(trk_bin):
    fresh(trk_bin)
    p = trk_bin.ok("new", "program", "P", *DEST)
    assert trk_bin.json("show", p)["item"]["owner"] == "michael"

    code, error = trk_bin.error("edit", p, "--no-owner")
    assert (code, error["rule"]) == (1, "owner-required")

    trk_bin.ok("config", "set", "default-owner", "kim")
    assert trk_bin.json("show", trk_bin.ok("new", "program", "P2", *DEST))["item"]["owner"] == "kim"


def ac3(trk_bin):
    """AC 3's store: P over draft project J over decision D, claimed by S1; worker asks product (kim) about D."""
    fresh(trk_bin)
    trk_bin.ok("roles", "set", "product", "kim")
    ids = {"P": trk_bin.ok("new", "program", "P", *DEST)}
    ids["J"] = trk_bin.ok("new", "project", "J", "--parent", ids["P"])
    ids["D"] = trk_bin.decision("D", ids["J"])
    trk_bin.ok("claim", ids["D"], "--session", "S1")
    ids["head"] = trk_bin.feed()["head"]
    ids["ask"] = ask(trk_bin, ids["D"], "product", "Q?")
    return ids


def test_ac3_an_ask_takes_its_parent_off_the_frontier_and_leaves_its_claim(trk_bin):
    ids = ac3(trk_bin)
    d, a = ids["D"], ids["ask"]

    assert [(c["item"], c["session"]) for c in trk_bin.json("claims")["claims"]] == [(d, "S1")]  # live claims only
    shown = trk_bin.json("show", d)["item"]
    assert f"open-ask:{a}" in shown["why_not"]
    assert [x["id"] for x in shown["open_asks"]] == [a]
    ready = frontier(trk_bin, ids["P"])
    assert ids["J"] in ready and d not in ready

    rows = trk_bin.feed(ids["head"])["changes"]
    assert [r["change"] for r in rows] == ["ask.opened"]  # no item.created for the ask
    opened = rows[0]
    assert (opened["item"], opened["addressee"], opened["owner"], opened["actor"]) == (a, "kim", "michael", "worker")
    assert opened["data"]["addressee_kind"] == "human"
    assert (opened["data"]["question"], opened["data"]["asker"]) == ("Which unit for flow?", "worker")


def test_ac4_opening_an_ask_is_refused_when_self_addressed_under_an_ask_or_to_an_unknown_role(trk_bin):
    ids = ac3(trk_bin)
    for parent, role, rule in [(ids["D"], "engineering", "self-addressed"), (ids["ask"], "product", "ask-leaf"),
                               (ids["D"], "finance", "unknown-role")]:
        code, error = trk_bin.error("ask", parent, "--to", role, "Q2?", "--question", "-", "--as", "michael",
                                    stdin="Why?")
        assert (code, error["rule"]) == (1, rule)

    done = trk_bin("new", "work-item", "Q3?", "--form", "ask", "--parent", ids["D"])
    assert done.returncode == 2 and "trk ask" in done.stderr


def waiting(trk_bin, *flags):
    return [a["id"] for a in trk_bin.json("waiting", *flags)["asks"]]


def ac5(trk_bin):
    """AC 5 from AC 3: product moves from kim to michael, re-routing the open ask."""
    ids = ac3(trk_bin)
    assert waiting(trk_bin, "--me", "kim") == [ids["ask"]]
    head = trk_bin.feed()["head"]
    trk_bin.ok("roles", "set", "product", "michael")
    ids["readdressed"] = rows_of(trk_bin, head, "ask.readdressed")
    return ids


def test_ac5_a_role_set_re_routes_the_open_ask(trk_bin):
    ids = ac5(trk_bin)
    [row] = ids["readdressed"]
    assert (row["item"], row["data"]["cause"], row["data"]["old_addressee"], row["addressee"]) == (
        ids["ask"], "role-set", "kim", "michael",
    )
    assert waiting(trk_bin, "--me", "kim") == []
    assert waiting(trk_bin, "--me", "michael") == [ids["ask"]]


def ac6(trk_bin):
    """AC 6 from AC 5: past S1's lease, worker may not answer; kim answers as a human proxy."""
    ids = ac5(trk_bin)
    trk_bin.now = T0 + timedelta(minutes=61)
    code, error = trk_bin.error("answer", ids["ask"], "--outcome", "Use m³/h", "--answer", "-", "--as", "worker",
                                stdin="Cubic metres per hour.")
    assert (code, error["rule"]) == (1, "ask-permission")

    head = trk_bin.feed()["head"]
    trk_bin.ok("answer", ids["ask"], "--outcome", "Use m³/h", "--answer", "-", "--as", "kim",
               stdin="Cubic metres per hour.")
    ids["answered"] = rows_of(trk_bin, head, "ask.answered")
    return ids


def test_ac6_a_human_proxy_answers_and_the_parent_returns_to_the_frontier(trk_bin):
    ids = ac6(trk_bin)
    shown = trk_bin.json("show", ids["ask"])["item"]
    assert (shown["status"], shown["resolution"], shown["outcome"]) == ("closed", "done", "Use m³/h")

    [row] = ids["answered"]
    assert (row["actor"], row["addressee"], row["data"]["proxy"]) == ("kim", "michael", True)
    assert (row["data"]["parent_claim_session"], row["data"]["parent_claim_live"]) == ("S1", False)
    assert ids["D"] in frontier(trk_bin, ids["P"])


def ac7_asks(trk_bin):
    """AC 7 from AC 6: worker opens two asks under D, the second urgent; D is ready to close."""
    ids = ac6(trk_bin)
    ids["A2"] = ask(trk_bin, ids["D"], "product", "Plain?")
    ids["A3"] = ask(trk_bin, ids["D"], "product", "Urgent?", "--urgent")
    assert waiting(trk_bin, "--all") == [ids["A3"], ids["A2"]]

    for argv in resolving(ids["D"]):
        trk_bin.ok(*argv)
    return ids


def ac7(trk_bin):
    """AC 7 as amended 2026-10-22 (tr-jwbg9): the asks are withdrawn on purpose, then michael closes D."""
    ids = ac7_asks(trk_bin)
    for a in (ids["A2"], ids["A3"]):
        trk_bin.ok("withdraw", a, "--reason", "not needed", "--as", "worker")
    trk_bin.ok("close", ids["D"], "--resolution", "done", "--outcome", "Settled.")
    return ids


def test_ac7_closing_the_parent_is_refused_while_its_asks_are_open(trk_bin):
    ids = ac7_asks(trk_bin)
    head = trk_bin.feed()["head"]
    code, error = trk_bin.error("close", ids["D"], "--resolution", "done", "--outcome", "Settled.")
    assert (code, error["rule"]) == (1, "open-asks")
    assert ids["A2"] in error["message"] and ids["A3"] in error["message"]
    assert trk_bin.feed(head)["changes"] == []
    for a in (ids["A2"], ids["A3"]):
        assert trk_bin.json("show", a)["item"]["status"] == "open"

    for a in (ids["A2"], ids["A3"]):
        trk_bin.ok("withdraw", a, "--reason", "not needed", "--as", "worker")
    head = trk_bin.feed()["head"]
    trk_bin.ok("close", ids["D"], "--resolution", "done", "--outcome", "Settled.")
    changes = [(r["change"], r["item"]) for r in trk_bin.feed(head)["changes"]]
    assert ("ask.withdrawn", ids["A2"]) not in changes and ("item.closed", ids["D"]) in changes


def test_ac8_final_asks_fixed_parents_and_who_may_withdraw(trk_bin):
    ids = ac7(trk_bin)
    e = trk_bin.decision("E", ids["J"])
    a1 = ask(trk_bin, e, "product", "A1?")

    # Since spec 04's amendment an answered ask may reopen with a reason, but not under a closed parent:
    # AC 7 closed D, so the answered ask from AC 6 is final either way.
    for argv, rule in [(("reopen", ids["ask"], "--reason", "Unsettled."), "ask-final"),
                       (("move", a1, "--parent", ids["J"]), "ask-parent-fixed"),
                       (("withdraw", a1, "--reason", "Not mine.", "--as", "herdr"), "ask-permission")]:
        code, error = trk_bin.error(*argv)
        assert (code, error["rule"]) == (1, rule), argv

    head = trk_bin.feed()["head"]
    trk_bin.ok("withdraw", a1, "--reason", "not needed", "--as", "worker")
    assert trk_bin.json("show", a1)["item"]["outcome"] == "withdrawn: not needed"
    [row] = rows_of(trk_bin, head, "ask.withdrawn")
    assert (row["item"], row["data"]["reason"]) == (a1, "not needed")


def test_ac8_as_amended_an_answered_ask_reopens_and_a_withdrawn_one_stays_final(trk_bin):
    """Spec 04's amendment to AC 8, under an open parent."""
    ids = ac3(trk_bin)
    withdrawn = ask(trk_bin, ids["D"], "product", "Drop me?")
    trk_bin.ok("withdraw", withdrawn, "--reason", "not needed", "--as", "worker")
    code, error = trk_bin.error("reopen", withdrawn, "--reason", "Needed after all.", "--as", "worker")
    assert (code, error["rule"]) == (1, "ask-final")

    trk_bin.ok("answer", ids["ask"], "--outcome", "Use m³/h", "--answer", "-", "--as", "kim", stdin="m³/h.")
    head = trk_bin.feed()["head"]
    trk_bin.ok("reopen", ids["ask"], "--reason", "Which flow, though?", "--as", "worker")
    [row] = trk_bin.feed(head)["changes"]
    assert (row["change"], row["item"], row["data"]["reason"]) == ("ask.reopened", ids["ask"], "Which flow, though?")
    assert waiting(trk_bin, "--me", "kim") == [ids["ask"]]


def journal(home, columns="*"):
    conn = sqlite3.connect(home / "store.db")
    try:
        return conn.execute(f"SELECT {columns} FROM journal ORDER BY seq").fetchall()
    finally:
        conn.close()


def test_ac9_a_schema_1_store_opens_under_the_installed_binary_and_replays(trk_bin, trk_home):
    """The migration test's case, rerun through the installed binary on a copy of the Core-built fixture."""
    trk_home.mkdir()
    for name in ("store.db", "feed.head"):
        shutil.copyfile(FIXTURE / name, trk_home / name)
    old_rows = journal(trk_home, "seq, at, change, item, actor, data")
    trk_bin.now = datetime.fromisoformat(FIXTURE_OPENED_AT)

    replay = trk_bin.feed()  # the first open migrates

    conn = sqlite3.connect(trk_home / "store.db")
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        roles = conn.execute("SELECT role, actor FROM roles ORDER BY role").fetchall()
        ownerless = conn.execute("SELECT count(*) FROM items WHERE parent_id IS NULL AND owner IS NULL").fetchone()
    finally:
        conn.close()
    assert int(meta["schema_version"]) == store.SCHEMA_VERSION >= 2
    assert meta["default_owner"] == "michael"
    assert roles == [("engineering", "michael"), ("product", "michael"), ("publisher", "michael")]
    assert ownerless == (0,)

    assert journal(trk_home, "seq, at, change, item, actor, data")[:len(old_rows)] == old_rows
    new_kinds = [r["change"] for r in replay["changes"][len(old_rows):]]
    assert new_kinds[:4] == ["config.changed", "role.set", "role.set", "owner.changed"]
    seqs = [r["seq"] for r in replay["changes"]]
    assert seqs == list(range(1, len(seqs) + 1)) and replay["head"] == seqs[-1]
    assert list(feed_validator().iter_errors(replay)) == []

    # The migrated store takes the new verbs through the binary, and still replays valid.
    p = next(r["item"] for r in replay["changes"] if r["change"] == "owner.changed")
    trk_bin.ok("actor", "add", "kim", "--kind", "human")
    trk_bin.ok("roles", "set", "product", "kim")
    a = ask(trk_bin, p, "product", "After migration?", actor="michael")
    trk_bin.ok("answer", a, "--outcome", "Yes.", "--answer", "-", "--as", "kim", stdin="Yes.")
    assert list(feed_validator().iter_errors(trk_bin.feed())) == []


def test_an_ask_cannot_replace_a_non_ask_but_may_replace_another_ask(trk_bin):
    """Spec 02's amendment of 2026-10-11 (tr-2ee1r), through the binary."""
    ids = ac3(trk_bin)
    d, a = ids["D"], ids["ask"]
    other = trk_bin.decision("Other", ids["J"])
    elsewhere = ask(trk_bin, other, "product", "Elsewhere?")
    head = trk_bin.feed()["head"]
    for flags in (("superseded", "--by", a), ("duplicate", "--of", a), ("superseded", "--by", elsewhere)):
        code, error = trk_bin.error("close", d, "--resolution", *flags, "--outcome", "Replaced.")
        assert (code, error["rule"]) == (1, "ask-cannot-replace"), flags
    assert trk_bin.feed(head)["changes"] == []

    trk_bin.ok("close", a, "--resolution", "duplicate", "--of", elsewhere, "--outcome", "Asked elsewhere.", "--as",
               "worker")
    [row] = rows_of(trk_bin, head, "ask.withdrawn")
    assert (row["item"], row["data"]["resolution"], row["data"]["of"]) == (a, "duplicate", elsewhere)
