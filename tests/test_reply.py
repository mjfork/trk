"""`trk reply`: a person's reply to an ask with no page behind it (tr-zb4rn). It is a proposed answer: the
ask stays open, and the asker confirms it with `trk answer --from-comment reply-<n>`."""

import pytest

from test_answer import body, store  # noqa: F401 (fixtures)
from test_asks import _stdin, ask, decision, journal, new, refusal, show

TEXT = "Did Codex review approve? It's not clear from here.\n"


@pytest.fixture
def asked(store, body):
    """Program P, decision D under it, ask A from worker to product (kim, a human)."""
    store("roles", "set", "product", "kim")
    p = new(store, "program", "P")
    d = decision(store, "D", "--parent", p)
    return ask(store, d, "product", "Q?", "--question", body["question"], "--as", "worker")["id"]


def reply(trk, monkeypatch, a, text=TEXT, actor="kim"):
    monkeypatch.setattr("sys.stdin", _stdin(text))
    return trk("reply", a, "--body", "-", "--as", actor, "--json")


def test_ac1_a_reply_leaves_the_ask_open_and_is_listed_with_a_reply_id(store, trk_home, asked, monkeypatch):
    since = journal(trk_home)[-1]["seq"]
    result = reply(store, monkeypatch, asked)
    assert result.code == 0
    (row,) = [r for r in journal(trk_home) if r["seq"] > since]
    assert row["change"] == "ask.replied" and row["actor"] == "kim" and row["item"] == asked
    assert row["data"]["comment_id"] == f"reply-{row['seq']}" and row["data"]["slug"] == "cli"
    assert row["status_old"] is None and row["status_new"] is None

    assert show(store, asked)["status"] == "open"
    expected = [{"comment": f"reply-{row['seq']}", "actor": "kim", "at": row["at"],
                 "excerpt": "Did Codex review approve? It's not clear from here."}]
    assert show(store, asked)["replies"] == expected
    (waiting,) = [w for w in store("waiting", "--me", "kim", "--json").json()["asks"] if w["id"] == asked]
    assert waiting["replies"] == expected
    assert "replied" in store("show", asked).out


def test_ac1_the_asker_confirms_the_reply_as_the_answer(store, trk_home, asked, monkeypatch):
    reply(store, monkeypatch, asked, "Metres.\n")
    comment = show(store, asked)["replies"][0]["comment"]
    done = store("answer", asked, "--from-comment", comment, "--outcome", "Metres", "--as", "worker", "--json")
    assert done.code == 0
    closed = show(store, asked)
    assert closed["status"] == "closed" and closed["resolution"] == "done"
    assert store("section", "get", asked, "answer", "--json").json()["body"].strip() == "Metres."
    answered = [r for r in journal(trk_home) if r["change"] == "ask.answered"][-1]
    assert answered["actor"] == "kim" and answered["data"]["proxy"] is False
    assert answered["data"]["via"] == "cli" and answered["data"]["comment_id"] == comment
    assert answered["data"]["confirmed_by"] == "worker" and "slug" not in answered["data"]


def test_ac1_a_reply_by_the_asker_is_listed_but_never_the_answer(store, asked, monkeypatch):
    assert reply(store, monkeypatch, asked, "Which ones?\n", actor="worker").code == 0
    (listed,) = show(store, asked)["replies"]
    assert listed["actor"] == "worker"
    refused = store("answer", asked, "--from-comment", listed["comment"], "--outcome", "x", "--as", "worker", "--json")
    assert refusal(refused) == (1, "own-reply")
    assert show(store, asked)["status"] == "open"


def test_a_plain_answer_still_closes_after_replies_and_tells_no_page(store, trk_home, asked, monkeypatch):
    reply(store, monkeypatch, asked)
    monkeypatch.setattr("sys.stdin", _stdin("Yes.\n"))
    assert store("answer", asked, "--outcome", "Yes", "--answer", "-", "--as", "kim").code == 0
    assert show(store, asked)["status"] == "closed"
    conn = __import__("sqlite3").connect(trk_home / "store.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM page_replies").fetchone()[0] == 0
    finally:
        conn.close()


def test_withdrawing_after_a_reply_writes_no_page_outbox_row(store, trk_home, asked, monkeypatch):
    reply(store, monkeypatch, asked)
    assert store("withdraw", asked, "--reason", "no longer needed", "--as", "worker").code == 0
    conn = __import__("sqlite3").connect(trk_home / "store.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM page_replies").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_asker_never_confirms_an_unknown_or_empty_reply(store, asked, monkeypatch):
    assert refusal(store("answer", asked, "--from-comment", "reply-999", "--outcome", "x", "--as", "worker", "--json")) == \
        (1, "no-reply")
    assert refusal(store("answer", asked, "--from-comment", "c-1", "--outcome", "x", "--as", "worker", "--json")) == \
        (1, "no-reply")


def test_a_reply_on_another_ask_is_no_reply_here(store, body, asked, monkeypatch):
    d = decision(store, "D2")
    other = ask(store, d, "product", "Q2?", "--question", body["question"], "--as", "worker")["id"]
    reply(store, monkeypatch, asked)
    comment = show(store, asked)["replies"][0]["comment"]
    assert refusal(store("answer", other, "--from-comment", comment, "--outcome", "x", "--as", "worker", "--json")) == \
        (1, "no-reply")


def test_reply_permission_and_body_rules(store, asked, monkeypatch):
    store("actor", "add", "bot", "--kind", "agent")
    denied = reply(store, monkeypatch, asked, actor="bot")
    assert refusal(denied) == (1, "ask-permission")
    assert reply(store, monkeypatch, asked, "  \n").code == 2
    assert show(store, asked)["replies"] == []


def test_replying_to_a_closed_or_missing_ask_is_refused(store, asked, monkeypatch):
    store("withdraw", asked, "--reason", "gone", "--as", "worker")
    assert refusal(reply(store, monkeypatch, asked)) == (1, "closed")
    assert reply(store, monkeypatch, "tr-nosuch").code == 3
