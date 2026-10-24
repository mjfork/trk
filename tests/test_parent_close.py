"""Closing an item with an open ask is refused until each ask is answered or withdrawn (spec 02: Parent
closes with an ask open; tr-jwbg9, Michael's ruling tr-29kgp of 2026-10-22). Before it, the close withdrew
the asks quietly (`parent_closed`)."""

import pytest

from gated import prove_close

from test_answer import _ac3_store
from test_asks import _stdin, ask, decision, journal, new, refusal, show


@pytest.fixture
def store(trk):
    """The spec's acceptance store: init --me michael, then kim (human) and worker (agent)."""
    trk("init", "--me", "michael")
    trk("actor", "add", "kim", "--kind", "human")
    trk("actor", "add", "worker", "--kind", "agent")
    return trk


@pytest.fixture
def body(tmp_path):
    """An `@file` path for a question."""
    question = tmp_path / "question.md"
    question.write_text("Which unit?\n")
    return {"question": f"@{question}"}


def close(trk, item_id, resolution="done", *argv, outcome="Settled"):
    if resolution == "done":
        prove_close(trk, item_id)
    return trk("close", item_id, "--resolution", resolution, "--outcome", outcome, *argv, "--json")


def waiting_all(trk):
    return [item["id"] for item in trk("waiting", "--all", "--json").json()["asks"]]


@pytest.fixture
def asked_twice(store, body):
    """Program P, decision D under it claimed by session s-1, and two asks from worker to product (kim)
    under D. Returns (p, d, a1, a2)."""
    store("roles", "set", "product", "kim")
    p = new(store, "program", "P")
    d = decision(store, "D", "--parent", p)
    assert store("claim", d, "--session", "s-1", "--as", "worker").code == 0  # before the asks take D off the frontier
    a1 = ask(store, d, "product", "Q1?", "--question", body["question"], "--as", "worker")["id"]
    a2 = ask(store, d, "product", "Q2?", "--question", body["question"], "--as", "worker")["id"]
    return p, d, a1, a2


# --- the refusal ---------------------------------------------------------------------


def test_close_is_refused_naming_each_ask_and_both_routes_and_writes_nothing(store, trk_home, asked_twice):
    _, d, a1, a2 = asked_twice
    prove_close(store, d)
    before = len(journal(trk_home))
    result = store("close", d, "--resolution", "done", "--outcome", "Settled", "--json")
    assert refusal(result) == (1, "open-asks")
    message = result.error()["message"]
    assert a1 in message and a2 in message
    assert f"trk answer {a1}" in message and f"trk withdraw {a1}" in message
    assert len(journal(trk_home)) == before
    assert {show(store, x)["status"] for x in (d, a1, a2)} == {"open"}


@pytest.mark.parametrize("resolution, key", [("wont-do", None), ("superseded", "by"), ("duplicate", "of")])
def test_it_holds_whatever_the_resolution(store, trk_home, asked_twice, resolution, key):
    p, d, a1, a2 = asked_twice
    other = decision(store, "D2", "--parent", p)
    result = close(store, d, resolution, *([f"--{key}", other] if key else []))
    assert refusal(result) == (1, "open-asks")
    assert {show(store, x)["status"] for x in (d, a1, a2)} == {"open"}


def test_an_open_ask_on_a_descendant_refuses_too_and_is_named(store, trk_home, body):
    p = new(store, "program", "P")
    d = decision(store, "D", "--parent", p)
    sub = decision(store, "Sub", "--parent", d)
    a = ask(store, sub, "product", "Q?", "--question", body["question"], "--as", "worker")["id"]
    result = close(store, d, "wont-do")
    assert refusal(result) == (1, "open-asks")
    assert a in result.error()["message"]
    assert {show(store, x)["status"] for x in (d, sub, a)} == {"open"}


def test_after_an_answer_or_a_withdrawal_the_close_succeeds_and_the_asks_stay_as_they_were(store, trk_home,
                                                                                         asked_twice):
    _, d, a1, a2 = asked_twice
    assert store("withdraw", a1, "--reason", "not needed", "--as", "worker").code == 0
    assert refusal(close(store, d)) == (1, "open-asks")
    assert store("withdraw", a2, "--reason", "also not needed", "--as", "worker").code == 0
    assert close(store, d).code == 0
    assert show(store, a1)["outcome"] == "withdrawn: not needed"
    assert show(store, a2)["outcome"] == "withdrawn: also not needed"
    reasons = [row["data"]["reason"] for row in journal(trk_home) if row["change"] == "ask.withdrawn"]
    assert reasons == ["not needed", "also not needed"]


def test_an_answered_ask_does_not_block_the_close(store, trk_home, monkeypatch, body):
    _, _, d, a = _ac3_store(store, monkeypatch)
    monkeypatch.setenv("TRK_NOW", "2026-10-11T09:00:00.000Z")
    monkeypatch.setattr("sys.stdin", _stdin("m³/h throughout.\n"))
    assert store("answer", a, "--outcome", "Use m³/h", "--answer", "-", "--as", "kim").code == 0
    assert close(store, d, outcome="Decided").code == 0


def test_a_refused_close_leaves_the_ask_on_the_parents_open_asks(store, asked_twice):
    _, d, a1, a2 = asked_twice
    assert refusal(close(store, d)) == (1, "open-asks")
    assert show(store, d)["open_asks"] != []


# --- an ask cannot replace a non-ask item (spec 02 amendment, 2026-10-11) ----------------


@pytest.mark.parametrize("resolution, key", [("superseded", "by"), ("duplicate", "of")])
def test_closing_an_item_as_replaced_by_its_own_ask_is_refused(store, trk_home, asked_twice, resolution, key):
    _, d, a1, a2 = asked_twice
    before = len(journal(trk_home))
    result = close(store, d, resolution, f"--{key}", a1)
    assert refusal(result) == (1, "ask-cannot-replace")
    assert a1 in result.error()["message"]
    assert len(journal(trk_home)) == before
    assert {show(store, x)["status"] for x in (d, a1, a2)} == {"open"}


@pytest.mark.parametrize("resolution, key", [("superseded", "by"), ("duplicate", "of")])
def test_closing_an_item_as_replaced_by_another_items_ask_is_refused(store, trk_home, asked_twice, resolution, key):
    p, d, a1, _ = asked_twice
    other = decision(store, "D2", "--parent", p)
    before = len(journal(trk_home))
    result = close(store, other, resolution, f"--{key}", a1)
    assert refusal(result) == (1, "ask-cannot-replace")
    assert len(journal(trk_home)) == before
    assert show(store, other)["status"] == "open"


@pytest.mark.parametrize("resolution, key", [("superseded", "by"), ("duplicate", "of")])
def test_an_ask_replaced_by_another_ask_still_closes(store, trk_home, asked_twice, resolution, key):
    _, d, a1, a2 = asked_twice
    result = close(store, a1, resolution, f"--{key}", a2, "--as", "worker")
    assert result.code == 0, result.err
    assert (show(store, a1)["status"], show(store, a2)["status"]) == ("closed", "open")
    withdrawn = [row for row in journal(trk_home) if (row["change"], row["item"]) == ("ask.withdrawn", a1)]
    assert [(row["data"]["resolution"], row["data"][key]) for row in withdrawn] == [(resolution, a2)]
