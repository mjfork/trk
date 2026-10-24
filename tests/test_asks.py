"""Opening an ask (spec 02: Roles' resolve(), Opening an ask, Blocking and readiness, Reads, Feed rows),
with AC 3 and AC 4 as written."""

import io
import json
import sqlite3

import pytest

from trk import asks
from gated import DEST, new_flags, prove_close, ticket


@pytest.fixture
def store(trk):
    """The spec's acceptance store: init --me michael, then kim (human) and worker (agent)."""
    trk("init", "--me", "michael", "--display", "Michael Fork")
    trk("actor", "add", "kim", "--kind", "human", "--display", "Kim")
    trk("actor", "add", "worker", "--kind", "agent")
    return trk


@pytest.fixture
def question(tmp_path):
    path = tmp_path / "question.md"
    path.write_text("Which **unit** for flow?\n")
    return f"@{path}"


def _stdin(text):
    return io.StringIO(text)


def journal(trk_home):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM journal ORDER BY seq")]
    finally:
        conn.close()
    for row in rows:
        row["data"] = json.loads(row["data"])
    return rows


def new(trk, kind, title, *argv):
    result = trk("new", kind, title, *argv, *new_flags((kind, title, *argv)), "--json")
    assert result.code == 0, result.err
    return result.json()["item"]["id"]


def decision(trk, title, *argv):
    return new(trk, "work-item", title, "--form", "decision", "--subtype", "task", *argv)


def ask(trk, parent, role, title, *argv):
    result = trk("ask", parent, "--to", role, title, *argv, "--json")
    assert result.code == 0, result.err
    return result.json()["item"]


def show(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


def refusal(result):
    return result.code, result.error()["rule"]


def ready(trk, *argv):
    return [item["id"] for item in trk("ready", *argv, "--json").json()["items"]]


# --- AC 3 and AC 4, as written ------------------------------------------------------


def test_ac3_an_ask_takes_its_parent_off_the_frontier_and_journals_ask_opened(store, trk_home, monkeypatch):
    store("roles", "set", "product", "kim")  # AC 1 left product with kim
    p = new(store, "program", "P")
    j = new(store, "project", "J", "--parent", p)
    d = decision(store, "D", "--parent", j)
    assert store("claim", d, "--session", "s-1").code == 0
    monkeypatch.setattr("sys.stdin", _stdin("Which unit for flow, m³/h or l/s?\n"))
    opened = store("ask", d, "--to", "product", "Q?", "--question", "-", "--as", "worker", "--json")
    assert opened.code == 0, opened.err
    a = opened.json()["item"]["id"]

    shown = show(store, d)
    assert shown["claim"]["session"] == "s-1"
    assert f"open-ask:{a}" in shown["why_not"]
    assert shown["open_asks"] == [
        {"id": a, "title": "Q?", "addressee_role": "product", "addressee": "kim", "urgent": False},
    ]
    assert j in ready(store, "--root", p)

    rows = [row for row in journal(trk_home) if row["item"] == a]
    assert [row["change"] for row in rows] == ["ask.opened"]
    row = rows[0]
    assert (row["addressee"], row["owner"], row["actor"]) == ("kim", "michael", "worker")
    assert (row["status_old"], row["status_new"]) == (None, "open")
    assert row["data"]["addressee_kind"] == "human"
    assert row["data"]["question"] == "Which unit for flow, m³/h or l/s?"
    assert row["data"]["asker"] == "worker"


def test_ac4_refusals(store, trk_home, question, tmp_path):
    p = new(store, "program", "P")
    d = decision(store, "D", "--parent", p)
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "worker")["id"]
    result = store("ask", d, "--to", "engineering", "Mine?", "--question", question, "--as", "michael", "--json")
    assert refusal(result) == (1, "self-addressed")
    result = store("ask", a, "--to", "product", "Deeper?", "--question", question, "--json")
    assert refusal(result) == (1, "ask-leaf")
    result = store("ask", d, "--to", "finance", "Money?", "--question", question, "--json")
    assert refusal(result) == (1, "unknown-role")
    assert store("new", "work-item", "Q", "--form", "ask", "--parent", d).code == 2


# --- resolve() ------------------------------------------------------------------------


def addressee(trk, ask_id):
    return show(trk, ask_id)["addressee"]


def test_a_function_role_resolves_through_the_role_table_when_read(store, question):
    d = decision(store, "D")
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "worker")["id"]
    assert addressee(store, a) == "michael"
    store("roles", "set", "product", "kim")
    assert addressee(store, a) == "kim"


def test_parent_resolves_to_the_owner_above_the_blocked_item(store, question):
    p = new(store, "program", "P", "--owner", "kim")
    j = new(store, "project", "J", "--parent", p)
    t = decision(store, "T", "--parent", j, "--owner", "worker")
    nested = ask(store, t, "parent", "Nested?", "--question", question)["id"]
    assert addressee(store, nested) == "kim"  # T's parent J inherits P's owner

    top = decision(store, "Top", "--owner", "kim")
    on_top = ask(store, top, "parent", "Top?", "--question", question)["id"]
    assert addressee(store, on_top) == "kim"  # top-level: the blocked item's own owner

    store("edit", p, "--owner", "michael")
    assert addressee(store, nested) == "michael"
    store("edit", j, "--owner", "worker")
    assert addressee(store, nested) == "worker"
    store("move", t, "--parent", p)
    assert addressee(store, nested) == "michael"


def test_resolve_is_the_one_function_every_read_goes_through(store, trk_home, question, monkeypatch):
    d = decision(store, "D")
    monkeypatch.setattr(asks, "resolve", lambda conn, row: "kim")
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "worker")["id"]
    assert journal(trk_home)[-1]["addressee"] == "kim"
    assert show(store, a)["addressee"] == "kim"
    assert show(store, a)["addressee_kind"] == "human"
    assert show(store, d)["open_asks"][0]["addressee"] == "kim"


# --- opening an ask -------------------------------------------------------------------


def test_an_ask_stores_role_and_asker_and_its_question(store, trk_home, question):
    d = decision(store, "D")
    item = ask(store, d, "product", "Which unit?", "--question", question, "--as", "worker")
    assert (item["kind"], item["form"], item["subtype"]) == ("work-item", "ask", None)
    assert (item["parent"], item["owner"], item["effective_owner"]) == (d, None, "michael")
    assert (item["addressee_role"], item["addressee"], item["addressee_kind"], item["asker"]) == (
        "product", "michael", "human", "worker",
    )
    assert [s["handle"] for s in item["sections"]] == ["question", "answer"]
    assert item["sections"][0]["body"] == "Which **unit** for flow?\n"
    assert item["sections"][1]["present"] is False
    assert item["ready"] is False
    assert "kind-never" in item["why_not"]


def test_other_items_carry_no_ask_keys(store):
    item = store("new", "program", "P", *DEST, "--json").json()["item"]
    assert not {"addressee_role", "addressee", "addressee_kind", "asker"} & set(item)
    assert item["open_asks"] == []


@pytest.mark.parametrize("argv", [(), ("--question", "@{empty}"), ("--question", "-")])
def test_the_question_is_required_and_non_empty(store, tmp_path, monkeypatch, argv):
    empty = tmp_path / "empty.md"
    empty.write_text("  \n")
    monkeypatch.setattr("sys.stdin", _stdin(""))
    d = decision(store, "D")
    argv = [arg.format(empty=empty) for arg in argv]
    result = store("ask", d, "--to", "product", "Q?", *argv, "--as", "worker")
    assert result.code == 2
    assert not [row for row in journal_rows(store) if row["change"] == "ask.opened"]


def journal_rows(trk):
    return trk("changes", "--since", "0", "--json").json()["changes"]


def test_the_question_flag_takes_a_file_or_stdin(store):
    d = decision(store, "D")
    result = store("ask", d, "--to", "product", "Q?", "--question", "inline text", "--as", "worker")
    assert result.code == 2


def test_urgent_and_placement_flags(store, question):
    d = decision(store, "D")
    first = ask(store, d, "product", "First", "--question", question, "--as", "worker")["id"]
    second = ask(store, d, "product", "Second", "--question", question, "--as", "worker", "--first", "--urgent")
    assert second["urgent"] is True
    third = ask(store, d, "product", "Third", "--question", question, "--as", "worker", "--after", second["id"])
    assert [o["id"] for o in show(store, d)["open_asks"]] == [second["id"], third["id"], first]


def test_new_with_form_ask_points_at_trk_ask(store):
    result = store("new", "work-item", "Q", "--form", "ask")
    assert result.code == 2
    assert "trk ask" in result.err


def test_refusal_order_unknown_parent_first(store, question):
    result = store("ask", "tr-zzzzz", "--to", "finance", "Q?", "--question", question, "--json")
    assert result.code == 3


def test_refusal_order_ask_leaf_before_the_role_and_parent_closed_first(store, question):
    d = decision(store, "D")
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "worker")["id"]
    # The ask is open, so it refuses as a leaf before the role is even looked at.
    result = store("ask", a, "--to", "finance", "Q?", "--question", question, "--as", "worker", "--json")
    assert refusal(result) == (1, "ask-leaf")
    closed = decision(store, "Closed")
    prove_close(store, closed)
    store("close", closed, "--resolution", "done", "--outcome", "Done.")
    result = store("ask", closed, "--to", "finance", "Q?", "--question", question, "--json")
    assert refusal(result) == (1, "parent-closed")


def test_refusal_order_unknown_role_before_self_addressed(store, question):
    d = decision(store, "D")
    result = store("ask", d, "--to", "parent", "Q?", "--question", question, "--as", "michael", "--json")
    assert refusal(result) == (1, "self-addressed")
    result = store("ask", d, "--to", "nobody", "Q?", "--question", question, "--as", "michael", "--json")
    assert refusal(result) == (1, "unknown-role")


def test_an_ask_may_sit_under_any_open_item_programs_included(store, question):
    p = new(store, "program", "P")
    j = new(store, "project", "J", "--parent", p)
    for parent in (p, j):
        assert ask(store, parent, "product", "Q?", "--question", question, "--as", "worker")["parent"] == parent


def test_new_under_an_ask_is_refused_ask_leaf(store, question):
    d = decision(store, "D")
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "worker")["id"]
    result = store("new", "work-item", "Sub", "--form", "self-contained", "--parent", a, *ticket("self-contained"), "--json")
    assert refusal(result) == (1, "ask-leaf")


def test_move_under_an_ask_is_refused_ask_leaf(store, question):
    d = decision(store, "D")
    other = decision(store, "Other")
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "worker")["id"]
    result = store("move", other, "--parent", a, "--json")
    assert refusal(result) == (1, "ask-leaf")


def test_an_ask_never_changes_parent_but_reorders_among_siblings(store, question):
    d = decision(store, "D")
    e = decision(store, "E")
    a = ask(store, d, "product", "A", "--question", question, "--as", "worker")["id"]
    b = ask(store, d, "product", "B", "--question", question, "--as", "worker")["id"]
    for argv in (("--parent", e), ("--top",), ("--parent", "tr-zzzzz")):
        result = store("move", a, *argv, "--json")
        assert refusal(result) == (1, "ask-parent-fixed")
    assert store("move", a, "--after", b).code == 0
    assert store("move", b, "--parent", d, "--last").code == 0
    assert [o["id"] for o in show(store, d)["open_asks"]] == [a, b]


def test_edit_owner_on_an_ask_is_refused(store, question):
    d = decision(store, "D")
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "worker")["id"]
    result = store("edit", a, "--owner", "kim", "--json")
    assert refusal(result) == (1, "ask-no-owner")
    assert store("edit", a, "--title", "Better?").code == 0


def test_asks_are_never_ready_or_claimable(store, question):
    d = decision(store, "D")
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "worker")["id"]
    assert a not in ready(store)
    result = store("claim", a, "--session", "s-1", "--json")
    assert refusal(result) == (1, "unclaimable")


# --- the ask.opened row ---------------------------------------------------------------


def test_ask_opened_carries_item_created_keys_with_an_asks_fixed_values(store, trk_home, question):
    d = decision(store, "D")
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "worker", "--urgent")
    row = journal(trk_home)[-1]
    assert row["change"] == "ask.opened"
    assert (row["item"], row["kind"], row["form"], row["parent"], row["depth"]) == (a["id"], "work-item", "ask", d, 1)
    assert row["data"] == {
        "subtype": None, "rank": a["rank"], "urgent": True, "jira_url": None, "repos": [], "sections": ["question"],
        "owner": None, "addressee_role": "product", "addressee_kind": "human", "question": "Which unit for flow?",
        "asker": "worker", "ledger": None,
    }


def test_ask_opened_to_parent_names_the_resolved_actor(store, trk_home, question):
    p = new(store, "program", "P", "--owner", "kim")
    d = decision(store, "D", "--parent", p, "--owner", "worker")
    ask(store, d, "parent", "Q?", "--question", question, "--as", "worker")
    row = journal(trk_home)[-1]
    assert (row["addressee"], row["owner"], row["data"]["addressee_role"]) == ("kim", "worker", "parent")


# --- the plain-text excerpt -------------------------------------------------------------


@pytest.mark.parametrize(
    "markdown, text",
    [
        ("# Heading\n\nBody", "Heading Body"),
        ("### Deep heading", "Deep heading"),
        ("- one\n* two\n+ three\n1. four\n2) five", "one two three four five"),
        ("> quoted\n> > twice\n> - listed", "quoted twice listed"),
        ("before\n```python\ncode()\n```\nafter", "before code() after"),
        ("~~~\nx\n~~~", "x"),
        ("**bold** _em_ `code` *it*", "bold em code it"),
        ("see [the docs](https://x.test/a_b) and ![a chart](c.png)", "see the docs and a chart"),
        ("  lots   of\t\twhite\n\n\nspace  ", "lots of white space"),
        ("#hashtag and -dash", "#hashtag and -dash"),
        ("", ""),
    ],
)
def test_excerpt_rules(markdown, text):
    assert asks.excerpt(markdown) == text


def test_excerpt_cuts_at_240_characters_including_the_ellipsis():
    assert asks.excerpt("x" * 240) == "x" * 240
    cut = asks.excerpt("y" * 241)
    assert len(cut) == 240
    assert cut == "y" * 239 + "…"
    words = asks.excerpt("word " * 100)
    assert len(words) <= 240 and words.endswith("…") and not words.endswith(" …")


# --- show -------------------------------------------------------------------------------


def test_show_lists_open_asks_in_rank_order_and_leaves_closed_ones_out(store, trk_home, question):
    d = decision(store, "D")
    a = ask(store, d, "product", "A", "--question", question, "--as", "worker")["id"]
    b = ask(store, d, "parent", "B", "--question", question, "--as", "worker", "--first", "--urgent")["id"]
    assert show(store, d)["open_asks"] == [
        {"id": b, "title": "B", "addressee_role": "parent", "addressee": "michael", "urgent": True},
        {"id": a, "title": "A", "addressee_role": "product", "addressee": "michael", "urgent": False},
    ]
    conn = sqlite3.connect(trk_home / "store.db")
    conn.execute("UPDATE items SET status = 'closed', resolution = 'done', outcome = 'x' WHERE id = ?", (b,))
    conn.commit()
    conn.close()
    assert [o["id"] for o in show(store, d)["open_asks"]] == [a]


def test_show_human_names_the_addressee_and_open_asks(store, question):
    d = decision(store, "D")
    a = ask(store, d, "product", "Which unit?", "--question", question, "--as", "worker", "--urgent")["id"]
    shown = store("show", d).out.splitlines()
    assert "  open asks:" in shown
    assert f"    {a}  ! → product (michael)  Which unit?" in shown
    shown = store("show", a).out.splitlines()
    assert "  to: product (michael)" in shown
    assert "  asker: worker" in shown
