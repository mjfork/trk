"""An ask its addressee has replied to waits on its asker, not on them (UP-0036 from the fleet: "an answered
ask stays in needs-you"). While `awaiting` is `asker` the ask leaves the addressee's `trk waiting` — and with
it their Needs-you page and any wake a consumer makes from `waiting --all --json` — and shows for its asker,
marked as awaiting their confirmation. A reply back from the asker returns it. Throughout, the ask stays open
and keeps its parent off the frontier: nothing here closes or hides work, it only routes it.

The Needs-you and program pages are pinned in test_pages_needs_you.py, where the page harness lives."""

import pytest

from test_answer import body, store  # noqa: F401 (fixtures)
from test_asks import _stdin, ask, decision, new, ready, show

APPROVE = "Approve.\n"


@pytest.fixture
def asked(store, body):
    """Program P, decision D under it, ask A from worker (an agent) to product (kim, a human): (d, a)."""
    store("roles", "set", "product", "kim")
    p = new(store, "program", "P")
    d = decision(store, "D", "--parent", p)
    return d, ask(store, d, "product", "Q?", "--question", body["question"], "--as", "worker")["id"]


def reply(trk, monkeypatch, a, text, actor):
    """`trk reply`: the same `ask.replied` row a page comment writes, without needing a page."""
    monkeypatch.setattr("sys.stdin", _stdin(text))
    result = trk("reply", a, "--body", "-", "--as", actor, "--json")
    assert result.code == 0, result.err
    return result


def waiting(trk, *argv):
    result = trk("waiting", *argv, "--json")
    assert result.code == 0, result.err
    return [row["id"] for row in result.json()["asks"]]


def line(trk, *argv):
    result = trk("waiting", *argv)
    assert result.code == 0, result.err
    return result.out


# --- it leaves the addressee ------------------------------------------------------------


def test_a_reply_from_the_addressee_takes_the_ask_off_their_waiting_list(store, asked, monkeypatch):
    _, a = asked
    assert waiting(store, "--me", "kim") == [a]
    reply(store, monkeypatch, a, APPROVE, "kim")
    assert show(store, a)["awaiting"] == "asker"
    assert waiting(store, "--me", "kim") == []
    assert a not in line(store, "--me", "kim")


def test_it_waits_on_its_asker_marked_awaiting_their_confirmation(store, asked, monkeypatch):
    _, a = asked
    assert waiting(store, "--me", "worker") == []  # the asker is not waiting on themselves yet
    reply(store, monkeypatch, a, APPROVE, "kim")
    assert waiting(store, "--me", "worker") == [a]
    shown = line(store, "--me", "worker")
    assert a in shown and "[replied: awaiting worker's confirmation]" in shown


def test_a_second_reply_from_the_addressee_keeps_it_with_the_asker(store, asked, monkeypatch):
    """The incident behind UP-0036: a person answered on day 1 and again on day 3, and the ask was still theirs."""
    _, a = asked
    reply(store, monkeypatch, a, APPROVE, "kim")
    reply(store, monkeypatch, a, "Approve, if it ships behind a flag.\n", "kim")
    assert len(show(store, a)["replies"]) == 2 and show(store, a)["awaiting"] == "asker"
    assert waiting(store, "--me", "kim") == [] and waiting(store, "--me", "worker") == [a]


def test_a_reply_from_any_other_person_also_sends_it_to_the_asker(store, asked, monkeypatch):
    """`awaiting` is the rule, not "the addressee replied": any reply but the asker's is theirs to read."""
    _, a = asked
    reply(store, monkeypatch, a, APPROVE, "michael")
    assert waiting(store, "--me", "kim") == [] and waiting(store, "--me", "worker") == [a]


def test_the_ask_stays_open_and_still_holds_up_its_parent(store, asked, monkeypatch):
    _, a = asked
    d = asked[0]
    reply(store, monkeypatch, a, APPROVE, "kim")
    item = show(store, a)
    assert item["status"] == "open" and item["resolution"] is None
    parent = show(store, d)
    assert [o["id"] for o in parent["open_asks"]] == [a]
    assert parent["ready"] is False and f"open-ask:{a}" in parent["why_not"]
    assert d not in ready(store)


# --- no consumer wakes the addressee ---------------------------------------------------


def test_json_for_every_actor_leaves_it_out_while_the_text_list_shows_it_marked(store, asked, monkeypatch):
    """hx routes each ask `waiting --all --json` lists to its addressee, so an answered one must not be there."""
    _, a = asked
    assert waiting(store, "--all") == [a]
    reply(store, monkeypatch, a, APPROVE, "kim")
    assert waiting(store, "--all") == []
    shown = line(store, "--all")
    assert a in shown and "[replied: awaiting worker's confirmation]" in shown


def test_show_names_the_confirmation_only_while_it_is_the_askers_move(store, asked, monkeypatch):
    _, a = asked
    reply(store, monkeypatch, a, APPROVE, "kim")
    assert "awaiting confirmation: trk answer" in store("show", a).out
    reply(store, monkeypatch, a, "Approve what, exactly?\n", "worker")
    out = store("show", a).out
    assert "replied: 2" in out and "awaiting confirmation" not in out


# --- and it comes back -----------------------------------------------------------------


def test_a_reply_back_from_the_asker_returns_it_to_the_addressee(store, asked, monkeypatch):
    _, a = asked
    reply(store, monkeypatch, a, APPROVE, "kim")
    reply(store, monkeypatch, a, "Approve which option?\n", "worker")
    assert show(store, a)["awaiting"] == "addressee"
    assert waiting(store, "--me", "kim") == [a] and waiting(store, "--me", "worker") == []
    assert waiting(store, "--all") == [a]
    assert "[replied:" not in line(store, "--me", "kim")


def test_confirming_the_reply_closes_the_ask_and_empties_both_lists(store, asked, monkeypatch):
    _, a = asked
    reply(store, monkeypatch, a, APPROVE, "kim")
    comment = show(store, a)["replies"][-1]["comment"]
    done = store("answer", a, "--from-comment", comment, "--outcome", "Approved", "--as", "worker", "--json")
    assert done.code == 0, done.err
    closed = show(store, a)
    assert closed["status"] == "closed" and closed["resolution"] == "done" and closed["awaiting"] is None
    assert waiting(store, "--me", "kim") == [] and waiting(store, "--me", "worker") == []
