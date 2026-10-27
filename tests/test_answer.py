"""Answering, withdrawing and the other ask closes (spec 02: Answering, Withdrawing and other closes,
Feed rows), with reopening an answered ask as spec 04's Amendments to Asks and owners have it, and
AC 6 and AC 8 as written (AC 8's `reopen` step as amended)."""

import pytest

from gated import prove_close

from test_asks import _stdin, ask, decision, journal, new, ready, refusal, show

LEASE_LAPSED = "2026-10-11T09:00:00.000Z"


@pytest.fixture
def store(trk):
    """The spec's acceptance store: init --me michael, then kim (human) and worker (agent)."""
    trk("init", "--me", "michael")
    trk("actor", "add", "kim", "--kind", "human")
    return trk


@pytest.fixture
def body(tmp_path):
    """`@file` paths for a question and an answer."""
    question, answer = tmp_path / "question.md", tmp_path / "answer.md"
    question.write_text("Which unit?\n")
    answer.write_text("Use **m³/h**, see [the table](https://x/t).\n")
    return {"question": f"@{question}", "answer": f"@{answer}"}


@pytest.fixture
def asked(store, body):
    """Program P (michael), decision D under it, and ask A from worker to product (kim). Returns (d, a)."""
    store("roles", "set", "product", "kim")
    p = new(store, "program", "P")
    d = decision(store, "D", "--parent", p)
    a = ask(store, d, "product", "Q?", "--question", body["question"], "--as", "worker")["id"]
    return d, a


def answer(trk, a, *argv, outcome="Use m³/h"):
    return trk("answer", a, "--outcome", outcome, *argv, "--json")


def rows_for(trk_home, item_id):
    return [row for row in journal(trk_home) if row["item"] == item_id]


# --- AC 6 and AC 8, as written --------------------------------------------------------


def _ac3_store(store, monkeypatch):
    """AC 3's store, then AC 5's `roles set product michael` (its waiting and readdressed parts are 03's)."""
    store("roles", "set", "product", "kim")
    p = new(store, "program", "P")
    j = new(store, "project", "J", "--parent", p)
    d = decision(store, "D", "--parent", j)
    monkeypatch.setenv("TRK_NOW", "2026-10-11T08:00:00.000Z")
    assert store("claim", d, "--session", "s-1").code == 0
    monkeypatch.setattr("sys.stdin", _stdin("Which unit for flow?\n"))
    a = store("ask", d, "--to", "product", "Q?", "--question", "-", "--as", "worker", "--json").json()["item"]["id"]
    assert store("roles", "set", "product", "michael").code == 0
    return p, j, d, a


def test_ac6_a_human_proxy_answers_and_the_parent_returns_to_the_frontier(store, trk_home, monkeypatch):
    p, _, d, a = _ac3_store(store, monkeypatch)
    monkeypatch.setenv("TRK_NOW", LEASE_LAPSED)
    monkeypatch.setattr("sys.stdin", _stdin("m³/h throughout.\n"))
    assert refusal(store("answer", a, "--outcome", "Use m³/h", "--answer", "-", "--as", "worker", "--json")) == (
        1, "ask-permission")
    monkeypatch.setattr("sys.stdin", _stdin("m³/h throughout.\n"))
    result = store("answer", a, "--outcome", "Use m³/h", "--answer", "-", "--as", "kim", "--json")
    assert result.code == 0, result.err
    item = result.json()["item"]
    assert (item["status"], item["resolution"], item["outcome"]) == ("closed", "done", "Use m³/h")
    row = journal(trk_home)[-1]
    assert row["change"] == "ask.answered"
    assert (row["actor"], row["addressee"]) == ("kim", "michael")
    assert row["data"]["proxy"] is True
    assert (row["data"]["parent_claim_session"], row["data"]["parent_claim_live"]) == ("s-1", False)
    assert d in ready(store, "--root", p)


def test_ac8_reopen_move_and_withdraw_refusals_then_the_asker_withdraws(store, trk_home, monkeypatch, body):
    p, j, d, a = _ac3_store(store, monkeypatch)
    monkeypatch.setattr("sys.stdin", _stdin("m³/h.\n"))
    assert store("answer", a, "--outcome", "Use m³/h", "--answer", "-", "--as", "kim").code == 0
    e = decision(store, "E", "--parent", j)
    a1 = ask(store, e, "product", "Need E?", "--question", body["question"], "--as", "worker")["id"]
    # Amended by spec 04: an answered ask reopens with a reason; it is a withdrawn one that is final.
    assert store("reopen", a, "--json").code == 2
    assert refusal(store("move", a1, "--parent", j, "--json")) == (1, "ask-parent-fixed")
    assert refusal(store("withdraw", a1, "--reason", "nah", "--as", "herdr", "--json")) == (1, "ask-permission")
    result = store("withdraw", a1, "--reason", "not needed", "--as", "worker", "--json")
    assert result.code == 0, result.err
    assert (result.json()["item"]["resolution"], result.json()["item"]["outcome"]) == ("wont-do", "withdrawn: not needed")
    row = journal(trk_home)[-1]
    assert (row["change"], row["data"]["reason"]) == ("ask.withdrawn", "not needed")
    assert refusal(store("reopen", a1, "--as", "worker", "--json")) == (1, "ask-final")  # spec 04 AC 8: no reason


# --- answering ------------------------------------------------------------------------


def test_answer_sets_the_section_closes_done_and_journals_one_ask_answered(store, trk_home, asked, body):
    d, a = asked
    result = answer(store, a, "--answer", body["answer"], "--as", "kim")
    assert result.code == 0, result.err
    shown = show(store, a)
    assert (shown["status"], shown["resolution"]) == ("closed", "done")
    assert store("section", "get", a, "answer").out == "Use **m³/h**, see [the table](https://x/t).\n"
    rows = rows_for(trk_home, a)
    assert [row["change"] for row in rows] == ["ask.opened", "ask.answered"]
    row = rows[-1]
    assert (row["status_old"], row["status_new"], row["owner"], row["addressee"]) == ("open", "closed", "michael", "kim")
    assert row["data"] == {
        "resolution": "done", "outcome": "Use m³/h", "addressee_role": "product", "owner_kind": "human",
        "parent_claim_session": None, "parent_claim_live": False, "answer": "Use m³/h, see the table.",
        "proxy": False, "via": "cli",
    }
    assert show(store, d)["why_not"] == []


def test_answer_reads_stdin(store, asked, monkeypatch):
    _, a = asked
    monkeypatch.setattr("sys.stdin", _stdin("From stdin.\n"))
    assert answer(store, a, "--answer", "-", "--as", "kim").code == 0
    assert store("section", "get", a, "answer").out == "From stdin.\n"


@pytest.mark.parametrize("argv", [(), ("--answer", "-")], ids=["no-answer", "empty-answer"])
def test_the_answer_is_required_and_non_empty(store, asked, monkeypatch, argv):
    _, a = asked
    monkeypatch.setattr("sys.stdin", _stdin("  \n"))
    assert answer(store, a, *argv, "--as", "kim").code == 2
    assert show(store, a)["status"] == "open"


def test_answer_needs_an_outcome(store, asked, body):
    _, a = asked
    assert answer(store, a, "--answer", body["answer"], "--as", "kim", outcome=" ").code == 2


def test_answer_and_withdraw_take_asks_only(store, asked, body):
    d, _ = asked
    assert refusal(answer(store, d, "--answer", body["answer"])) == (1, "not-ask")
    assert refusal(store("withdraw", d, "--reason", "x", "--json")) == (1, "not-ask")


def test_answering_a_closed_ask_is_refused(store, asked, body):
    _, a = asked
    assert answer(store, a, "--answer", body["answer"], "--as", "kim").code == 0
    assert refusal(answer(store, a, "--answer", body["answer"], "--as", "kim")) == (1, "closed")


def test_the_parent_claim_named_is_the_most_recent_and_live_while_its_lease_runs(store, trk_home, body, monkeypatch):
    store("roles", "set", "product", "kim")
    monkeypatch.setenv("TRK_NOW", "2026-10-11T08:00:00.000Z")
    named = {}
    for title, sessions in (("Held", ("s-1", "s-2")), ("Released", ("s-3",))):
        d = decision(store, title)
        for session in sessions:
            assert store("claim", d, "--session", session).code == 0
            if session != "s-2":
                assert store("claim", "release", "--session", session).code == 0
        a = ask(store, d, "product", f"{title}?", "--question", body["question"], "--as", "worker")["id"]
        assert answer(store, a, "--answer", body["answer"], "--as", "kim").code == 0
        data = journal(trk_home)[-1]["data"]
        named[title] = (data["parent_claim_session"], data["parent_claim_live"])
    assert named == {"Held": ("s-2", True), "Released": ("s-3", False)}


def test_an_agent_answers_an_ask_that_resolves_to_itself(store, trk_home, body):
    store("actor", "add", "bot", "--kind", "agent")
    store("roles", "set", "engineering", "bot")
    d = decision(store, "D")
    a = ask(store, d, "engineering", "Q?", "--question", body["question"], "--as", "worker")["id"]
    assert answer(store, a, "--answer", body["answer"], "--as", "bot").code == 0
    row = journal(trk_home)[-1]
    assert (row["addressee"], row["data"]["proxy"]) == ("bot", False)


def test_ask_permission_names_who_may(store, asked, body):
    _, a = asked
    message = answer(store, a, "--answer", body["answer"], "--as", "worker").error()["message"]
    assert "kim" in message and "human" in message
    message = store("withdraw", a, "--reason", "x", "--as", "kim", "--json").error()["message"]
    assert "worker" in message and "michael" in message


# --- withdrawing and the other closes ---------------------------------------------------


def test_withdraw_closes_wont_do_and_journals_ask_withdrawn(store, trk_home, asked):
    d, a = asked
    result = store("withdraw", a, "--reason", "moot now", "--as", "michael", "--json")
    assert result.code == 0, result.err
    rows = rows_for(trk_home, a)
    assert [row["change"] for row in rows] == ["ask.opened", "ask.withdrawn"]
    row = rows[-1]
    assert (row["actor"], row["status_old"], row["status_new"], row["owner"], row["addressee"]) == (
        "michael", "open", "closed", "michael", "kim")
    assert row["data"] == {
        "resolution": "wont-do", "outcome": "withdrawn: moot now", "addressee_role": "product", "owner_kind": "human",
        "parent_claim_session": None, "parent_claim_live": False, "reason": "moot now",
    }
    assert show(store, d)["why_not"] == []


@pytest.mark.parametrize("reason", ["", "  ", "two\nlines", "x" * 290], ids=["empty", "blank", "newline", "long"])
def test_withdraw_needs_a_one_line_reason(store, asked, reason):
    _, a = asked
    assert store("withdraw", a, "--reason", reason, "--as", "worker", "--json").code == 2


@pytest.mark.parametrize("resolution", ["done", "wont-do", "out-of-scope"])
def test_close_on_an_ask_points_at_answer_or_withdraw(store, asked, resolution):
    _, a = asked
    result = store("close", a, "--resolution", resolution, "--outcome", "x", "--json")
    assert result.code == 2
    assert "answer" in result.err and "withdraw" in result.err


def test_close_duplicate_withdraws_the_ask_with_its_edge(store, trk_home, asked, body):
    d, a = asked
    original = ask(store, d, "product", "Q again?", "--question", body["question"], "--as", "worker")["id"]
    result = store("close", a, "--resolution", "duplicate", "--of", original, "--outcome", "Asked twice", "--as",
                   "worker", "--json")
    assert result.code == 0, result.err
    withdrawn, edge = journal(trk_home)[-2:]
    assert (withdrawn["change"], withdrawn["item"]) == ("ask.withdrawn", a)
    data = withdrawn["data"]
    assert (data["resolution"], data["of"], data["reason"], data["outcome"]) == (
        "duplicate", original, None, "Asked twice")
    assert (edge["change"], edge["data"]["type"], {edge["data"]["src"], edge["data"]["dst"]}) == (
        "edge.added", "relates", {a, original})


def test_close_superseded_withdraws_the_ask_with_its_edges(store, trk_home, asked, body):
    d, a = asked
    successor = ask(store, d, "product", "Better Q?", "--question", body["question"], "--as", "worker")["id"]
    result = store("close", a, "--resolution", "superseded", "--by", successor, "--outcome", "Reworded", "--as",
                   "michael", "--json")
    assert result.code == 0, result.err
    rows = journal(trk_home)
    withdrawn, edge = rows[-2:]
    assert (withdrawn["change"], withdrawn["item"]) == ("ask.withdrawn", a)
    assert (withdrawn["data"]["by"], withdrawn["data"]["reason"]) == (successor, None)
    assert (edge["change"], edge["data"]) == ("edge.added", {"type": "supersedes", "src": successor, "dst": a})


def test_duplicate_and_superseded_need_withdraws_permission(store, asked, body):
    d, a = asked
    other = ask(store, d, "product", "Other?", "--question", body["question"], "--as", "worker")["id"]
    for who in ("kim", "herdr"):
        result = store("close", a, "--resolution", "duplicate", "--of", other, "--outcome", "x", "--as", who, "--json")
        assert refusal(result) == (1, "ask-permission")
        result = store("close", a, "--resolution", "superseded", "--by", other, "--outcome", "x", "--as", who,
                       "--json")
        assert refusal(result) == (1, "ask-permission")


def test_closing_a_non_ask_is_unchanged(store, trk_home):
    d = decision(store, "D")
    prove_close(store, d)
    assert store("close", d, "--resolution", "done", "--outcome", "Done").code == 0
    assert journal(trk_home)[-1]["change"] == "item.closed"


# --- reopening an answered ask (spec 04's amendment) ------------------------------------


def test_the_asker_reopens_an_answered_ask(store, trk_home, asked, body):
    d, a = asked
    assert answer(store, a, "--answer", body["answer"], "--as", "kim").code == 0
    result = store("reopen", a, "--reason", "need units", "--as", "worker", "--json")
    assert result.code == 0, result.err
    shown = show(store, a)
    assert (shown["status"], shown["resolution"], shown["outcome"]) == ("open", None, None)
    assert store("section", "get", a, "answer").code == 0
    assert f"open-ask:{a}" in show(store, d)["why_not"]
    row = journal(trk_home)[-1]
    assert row["change"] == "ask.reopened"
    assert (row["status_old"], row["status_new"], row["actor"], row["owner"], row["addressee"]) == (
        "closed", "open", "worker", "michael", "kim")
    data = row["data"]
    assert (data["reason"], data["reopened_by"], data["asker"], data["addressee_role"], data["addressee_kind"]) == (
        "need units", "worker", "worker", "product", "human")
    assert (data["question"], data["sections"], data["owner"], data["subtype"]) == (
        "Which unit?", ["question", "answer"], None, None)
    assert not any(r["change"] == "item.reopened" for r in rows_for(trk_home, a))


def test_a_reopened_ask_answers_again_and_overwrites_the_answer(store, asked, body, monkeypatch):
    _, a = asked
    assert answer(store, a, "--answer", body["answer"], "--as", "kim").code == 0
    assert store("reopen", a, "--reason", "units?", "--as", "worker").code == 0
    monkeypatch.setattr("sys.stdin", _stdin("Second answer.\n"))
    assert answer(store, a, "--answer", "-", "--as", "kim", outcome="m³/h, SI").code == 0
    assert store("section", "get", a, "answer").out == "Second answer.\n"


@pytest.mark.parametrize("who, allowed", [
    ("worker", True), ("michael", True), ("kim", True), ("herdr", False),
], ids=["asker", "parent-owner", "any-human", "other-agent"])
def test_who_may_reopen(store, asked, body, who, allowed):
    _, a = asked
    assert answer(store, a, "--answer", body["answer"], "--as", "kim").code == 0
    result = store("reopen", a, "--reason", "again", "--as", who, "--json")
    if allowed:
        assert result.code == 0, result.err
    else:
        assert refusal(result) == (1, "ask-permission")


def test_a_withdrawn_ask_is_final(store, asked):
    _, a = asked
    assert store("withdraw", a, "--reason", "moot", "--as", "worker").code == 0
    assert refusal(store("reopen", a, "--reason", "after all", "--as", "worker", "--json")) == (1, "ask-final")


def test_an_answered_ask_under_a_closed_parent_is_final(store, asked, body):
    d, a = asked
    assert answer(store, a, "--answer", body["answer"], "--as", "kim").code == 0
    prove_close(store, d)
    assert store("close", d, "--resolution", "done", "--outcome", "Done").code == 0
    assert refusal(store("reopen", a, "--reason", "again", "--as", "worker", "--json")) == (1, "ask-final")


def test_reopen_of_an_open_ask_is_cores_not_closed(store, asked):
    _, a = asked
    assert refusal(store("reopen", a, "--json")) == (1, "not-closed")


def test_reopen_reason_goes_with_asks_only(store):
    d = decision(store, "D")
    prove_close(store, d)
    assert store("close", d, "--resolution", "done", "--outcome", "Done").code == 0
    assert store("reopen", d, "--reason", "x", "--json").code == 2
    assert store("reopen", d, "--json").code == 0
