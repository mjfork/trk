"""Handoffs: checkpoint records kept with an item, appended one per checkpoint and read back latest-first."""

import io
import json
import sqlite3
import sys

import pytest

from gated import new_flags

MAX = 256 * 1024
S1 = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
    trk("actor", "add", "worker", "--kind", "agent")
    return trk


@pytest.fixture
def stdin(monkeypatch):
    def feed(text):
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    return feed


def new(trk, *argv):
    result = trk("new", *argv, *new_flags(argv), "--json")
    assert result.code == 0, result.err
    return result.json()["item"]["id"]


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


def add(trk, stdin, item_id, body, *flags):
    stdin(body)
    result = trk("handoff", "add", item_id, *flags)
    assert result.code == 0, result.err
    return result


# --- add --------------------------------------------------------------------------------------


def test_add_reads_stdin_and_journals_handoff_added(store, stdin, trk_home, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-11T09:00:00Z")
    p = new(store, "program", "P")
    result = add(store, stdin, p, "\n  Stopped at the migration.  \n\nMore detail.\n", "--session", S1, "--as", "worker")
    assert result.out.strip() == p
    row = journal(trk_home)[-1]
    assert (row["change"], row["item"], row["kind"], row["root"], row["actor"], row["session"]) == (
        "handoff.added", p, "program", p, "worker", S1)
    assert row["data"] == {"id": 1, "first_line": "Stopped at the migration."}
    assert (row["status_old"], row["status_new"]) == (None, None)


def test_add_reads_a_file(store, stdin, tmp_path):
    p = new(store, "program", "P")
    f = tmp_path / "h.md"
    f.write_text("From a file.\n")
    assert store("handoff", "add", p, "--file", str(f)).code == 0
    assert store("handoff", "show", p).out == "From a file.\n"


def test_add_json_returns_the_record_with_its_body(store, stdin, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-11T09:00:00Z")
    p = new(store, "program", "P")
    stdin("Line one\nline two\n")
    result = store("handoff", "add", p, "--session", S1, "--json")
    assert result.code == 0, result.err
    assert result.json() == {"schema": 1, "item": p, "handoff": {
        "id": 1, "item": p, "at": "2026-10-11T09:00:00.000Z", "actor": "michael", "session": S1,
        "first_line": "Line one", "body": "Line one\nline two\n",
    }}


def test_add_takes_the_session_from_the_environment(store, stdin, monkeypatch, trk_home):
    p = new(store, "program", "P")
    monkeypatch.setenv("TRK_SESSION", S1)
    monkeypatch.setenv("TRK_ACTOR", "michael")  # a session in the env is an agent session: name the actor
    add(store, stdin, p, "x")
    assert journal(trk_home)[-1]["session"] == S1


def test_add_to_an_unknown_item_exits_3_and_writes_nothing(store, stdin, trk_home):
    before = len(journal(trk_home))
    stdin("x")
    result = store("handoff", "add", "tr-zzzzz", "--json")
    assert result.code == 3
    assert result.error()["code"] == "not-found"
    assert len(journal(trk_home)) == before


def test_a_body_up_to_256_kib_is_kept_and_a_larger_one_is_refused(store, stdin, trk_home):
    p = new(store, "program", "P")
    add(store, stdin, p, "a" * MAX)
    before = len(journal(trk_home))
    stdin("é" * (MAX // 2 + 1))
    result = store("handoff", "add", p, "--json")
    assert result.code == 1
    assert result.error()["rule"] == "handoff-too-large"
    assert len(journal(trk_home)) == before
    assert len(store("handoff", "show", p, "--all", "--json").json()["handoffs"]) == 1


@pytest.mark.parametrize("body", ["", "\n", "  \n\t\n"])
def test_a_blank_body_is_a_usage_error(store, stdin, body):
    p = new(store, "program", "P")
    stdin(body)
    result = store("handoff", "add", p, "--json")
    assert result.code == 2
    assert result.error()["code"] == "usage"


def test_the_first_line_is_cut_to_200_chars(store, stdin, trk_home):
    p = new(store, "program", "P")
    add(store, stdin, p, "x" * 250 + "\nrest")
    assert journal(trk_home)[-1]["data"]["first_line"] == "x" * 200


def test_a_closed_item_takes_a_handoff(store, stdin):
    p = new(store, "program", "P")
    store("close", p, "--resolution", "wont-do", "--outcome", "Parked.")
    add(store, stdin, p, "Why it was parked.")


def test_handoffs_are_append_only_in_the_database(store, stdin, trk_home):
    p = new(store, "program", "P")
    add(store, stdin, p, "x")
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        for statement in ("UPDATE handoffs SET body = 'y'", "DELETE FROM handoffs"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(statement)
    finally:
        conn.close()


def test_a_handoff_leaves_the_item_untouched(store, stdin, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-11T09:00:00Z")
    p = new(store, "program", "P")
    monkeypatch.setenv("TRK_NOW", "2026-10-11T10:00:00Z")
    add(store, stdin, p, "x")
    assert store("show", p, "--json").json()["item"]["updated_at"] == "2026-10-11T09:00:00.000Z"


# --- show -------------------------------------------------------------------------------------


def test_show_prints_the_latest_body_raw(store, stdin):
    p = new(store, "program", "P")
    add(store, stdin, p, "first\n")
    add(store, stdin, p, "second\n")
    assert store("handoff", "show", p).out == "second\n"


def test_show_json_and_all(store, stdin, monkeypatch):
    p = new(store, "program", "P")
    monkeypatch.setenv("TRK_NOW", "2026-10-11T09:00:00Z")
    add(store, stdin, p, "first\n", "--session", S1, "--as", "worker")
    monkeypatch.setenv("TRK_NOW", "2026-10-11T10:00:00Z")
    add(store, stdin, p, "second\n")
    first = {"id": 1, "item": p, "at": "2026-10-11T09:00:00.000Z", "actor": "worker", "session": S1,
             "first_line": "first", "body": "first\n"}
    second = {"id": 2, "item": p, "at": "2026-10-11T10:00:00.000Z", "actor": "michael", "session": None,
              "first_line": "second", "body": "second\n"}
    assert store("handoff", "show", p, "--json").json() == {"schema": 1, "item": p, "handoff": second}
    assert store("handoff", "show", p, "--all", "--json").json() == {"schema": 1, "item": p,
                                                                      "handoffs": [first, second]}


def test_show_all_prints_each_under_a_header_oldest_first(store, stdin, monkeypatch):
    p = new(store, "program", "P")
    monkeypatch.setenv("TRK_NOW", "2026-10-11T09:00:00Z")
    add(store, stdin, p, "first\n", "--session", S1, "--as", "worker")
    monkeypatch.setenv("TRK_NOW", "2026-10-11T10:00:00Z")
    add(store, stdin, p, "second")
    assert store("handoff", "show", p, "--all").out == (
        f"## handoff 1  2026-10-11T09:00:00.000Z  worker  session {S1}\n\nfirst\n\n"
        "## handoff 2  2026-10-11T10:00:00.000Z  michael\n\nsecond\n"
    )


def test_ids_count_per_store_not_per_item(store, stdin):
    p, q = new(store, "program", "P"), new(store, "program", "Q")
    add(store, stdin, p, "p")
    add(store, stdin, q, "q")
    assert store("handoff", "show", q, "--json").json()["handoff"]["id"] == 2
    assert [h["body"] for h in store("handoff", "show", p, "--all", "--json").json()["handoffs"]] == ["p"]


def test_show_with_no_handoff_exits_3_and_all_lists_none(store):
    p = new(store, "program", "P")
    result = store("handoff", "show", p, "--json")
    assert result.code == 3
    assert result.error()["item"] == p
    assert store("handoff", "show", p, "--all", "--json").json() == {"schema": 1, "item": p, "handoffs": []}
    assert store("handoff", "show", p, "--all").out == ""


def test_show_on_an_unknown_item_exits_3(store):
    assert store("handoff", "show", "tr-zzzzz").code == 3
    assert store("handoff", "show", "tr-zzzzz", "--all", "--json").code == 3


def test_show_matches_ids_like_every_verb(store, stdin):
    p = new(store, "program", "P")
    add(store, stdin, p, "x")
    assert store("handoff", "show", p.upper()).out == "x"


# --- the item JSON's summary ------------------------------------------------------------------


def test_the_item_json_summarises_the_latest_handoff_without_its_body(store, stdin, monkeypatch):
    p = new(store, "program", "P")
    assert store("show", p, "--json").json()["item"]["handoff"] is None
    add(store, stdin, p, "first")
    monkeypatch.setenv("TRK_NOW", "2026-10-11T10:00:00Z")
    add(store, stdin, p, "Second line one\nsecret detail\n", "--session", S1, "--as", "worker")
    summary = {"id": 2, "at": "2026-10-11T10:00:00.000Z", "actor": "worker", "session": S1,
               "first_line": "Second line one"}
    assert store("show", p, "--json").json()["item"]["handoff"] == summary
    assert store("tree", "--json").json()["items"][0]["handoff"] == summary
    assert "secret detail" not in store("show", p, "--json").out


def test_human_show_names_the_latest_handoff(store, stdin, monkeypatch):
    p = new(store, "program", "P")
    assert "handoff" not in store("show", p).out
    monkeypatch.setenv("TRK_NOW", "2026-10-11T10:00:00Z")
    add(store, stdin, p, "Where I stopped\nmore", "--as", "worker")
    assert f"  handoff: 2026-10-11T10:00:00.000Z  worker  Where I stopped  (trk handoff show {p})" in store("show", p).out
