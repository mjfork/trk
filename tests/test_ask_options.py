"""Asks carry structured options (tr-dsxdq): `trk ask --option/--recommend` stores an `options` section,
`trk answer --option N` answers with it, and the page shows it as a numbered list."""

import json

import pytest

from test_asks import ask, decision, new, refusal, show


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
    trk("actor", "add", "kim", "--kind", "human")
    trk("actor", "add", "worker", "--kind", "agent")
    trk("roles", "set", "product", "kim")
    return trk


@pytest.fixture
def question(tmp_path):
    path = tmp_path / "question.md"
    path.write_text("Which unit for flow?\n")
    return f"@{path}"


@pytest.fixture
def parent(store):
    return decision(store, "D", "--parent", new(store, "program", "P"))


def options_ask(store, parent, question, *flags):
    return ask(store, parent, "product", "Q?", "--question", question, "--as", "worker", *flags)["id"]


def stored(store, a):
    return json.loads(store("section", "get", a, "options", "--json").json()["body"])


def test_options_and_recommend_are_stored_as_an_options_section(store, parent, question):
    a = options_ask(store, parent, question, "--option", "m³/h", "--option", "L/s", "--recommend", "2")
    assert stored(store, a) == {"options": [{"n": 1, "label": "m³/h"}, {"n": 2, "label": "L/s"}], "recommended": 2}
    assert [s["handle"] for s in show(store, a)["sections"] if s["present"]] == ["question", "options"]


def test_options_without_recommend_store_null(store, parent, question):
    a = options_ask(store, parent, question, "--option", "yes", "--option", "no")
    assert stored(store, a)["recommended"] is None


def test_an_ask_without_options_has_no_options_section(store, parent, question):
    a = options_ask(store, parent, question)
    assert store("section", "get", a, "options", "--json").code == 3


@pytest.mark.parametrize("flags", [("--recommend", "1"), ("--option", "a", "--recommend", "2"),
                                   ("--option", "a", "--recommend", "0"), ("--option", " ")])
def test_bad_option_flags_are_refused_and_open_nothing(store, parent, question, flags):
    result = store("ask", parent, "--to", "product", "Q?", "--question", question, "--as", "worker", *flags, "--json")
    assert result.code == 2
    assert show(store, parent)["open_asks"] == []


def test_answer_option_closes_the_ask_with_the_options_label(store, parent, question):
    a = options_ask(store, parent, question, "--option", "m³/h", "--option", "L/s", "--recommend", "2")
    result = store("answer", a, "--option", "2", "--as", "kim", "--json")
    assert result.code == 0, result.err
    item = result.json()["item"]
    assert (item["status"], item["resolution"], item["outcome"]) == ("closed", "done", "Option 2: L/s")
    assert store("section", "get", a, "answer", "--json").json()["body"] == "L/s"


def test_answer_option_takes_a_body(store, parent, question, tmp_path):
    a = options_ask(store, parent, question, "--option", "m³/h", "--option", "L/s")
    body = tmp_path / "answer.md"
    body.write_text("L/s, because of the pump curves.\n")
    assert store("answer", a, "--option", "2", "--answer", f"@{body}", "--as", "kim", "--json").code == 0
    assert store("section", "get", a, "answer", "--json").json()["body"] == "L/s, because of the pump curves.\n"


def test_answer_option_on_an_ask_without_options_is_refused(store, parent, question):
    a = options_ask(store, parent, question)
    assert refusal(store("answer", a, "--option", "1", "--as", "kim", "--json")) == (1, "no-options")
    assert show(store, a)["status"] == "open"


@pytest.mark.parametrize("number", ["0", "3", "-1"])
def test_answer_option_out_of_range_is_refused(store, parent, question, number):
    a = options_ask(store, parent, question, "--option", "a", "--option", "b")
    assert refusal(store("answer", a, f"--option={number}", "--as", "kim", "--json")) == (1, "bad-option")
    assert show(store, a)["status"] == "open"


def test_answer_option_cannot_combine_with_outcome(store, parent, question):
    a = options_ask(store, parent, question, "--option", "a")
    assert store("answer", a, "--option", "1", "--outcome", "x", "--as", "kim").code == 2
    assert show(store, a)["status"] == "open"


def test_answer_option_keeps_the_ask_permission(store, parent, question):
    a = options_ask(store, parent, question, "--option", "a")
    assert refusal(store("answer", a, "--option", "1", "--as", "worker", "--json")) == (1, "ask-permission")


def test_free_text_answers_still_work_on_an_ask_with_options(store, parent, question, tmp_path):
    a = options_ask(store, parent, question, "--option", "a", "--option", "b")
    body = tmp_path / "answer.md"
    body.write_text("Neither: cubic feet.\n")
    assert store("answer", a, "--outcome", "cubic feet", "--answer", f"@{body}", "--as", "kim").code == 0
    assert show(store, a)["outcome"] == "cubic feet"


def test_answer_without_outcome_or_option_is_a_usage_error(store, parent, question):
    a = options_ask(store, parent, question, "--option", "a")
    assert store("answer", a, "--answer", "-", "--as", "kim").code == 2


def test_the_page_lists_the_options_with_the_recommended_one_marked(store, parent, question):
    a = options_ask(store, parent, question, "--option", "m³/h", "--option", "L/s", "--recommend", "2")
    html = store("pages", "render", parent, "--out", "-").out
    assert f'data-comment-id="ask-{a}"' in html
    assert '<li value="1">m³/h</li>' in html
    assert '<li value="2">L/s <span class="flag">recommended</span></li>' in html
