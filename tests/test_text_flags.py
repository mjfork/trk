"""One-line free-text flags (`--note`, `--outcome`, `--decided`, `--why`, `--alternative`) read `@file` and `-`."""

import io

import pytest

from gated import DEST, ticket


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, result.err
    return result.out.strip()


@pytest.fixture
def t(trk):
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    return ok(trk, "new", "work-item", "T", "--form", "spec-bound", "--parent", p, *ticket("spec-bound"))


def note_file(tmp_path, text, name="n.txt"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return "@" + str(path)


SYMBOLS = "±0.2 % (flow) 'a' \"b\" + c"


def test_note_reads_a_file_and_stdin_and_strips_the_trailing_newline(trk, t, tmp_path, monkeypatch):
    ok(trk, "evidence", "add", t, "--kind", "judgment", "--gate", "specd", "--note", note_file(tmp_path, SYMBOLS + "\n"))
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin " + SYMBOLS + "\n"))
    ok(trk, "evidence", "add", t, "--kind", "judgment", "--gate", "specd", "--note", "-")
    notes = [row["note"] for row in trk("evidence", t, "--json").json()["evidence"]]
    assert notes == [SYMBOLS, "from stdin " + SYMBOLS]


def test_decided_why_and_alternative_each_read_a_file(trk, t, tmp_path):
    ok(trk, "ledger", "add", t, "--kind", "fork", "--decided", note_file(tmp_path, "D ±1\n", "d"),
       "--alternative", note_file(tmp_path, "A (x)\n", "a"), "--alternative", "plain",
       "--why", note_file(tmp_path, "W +\n", "w"))
    (row,) = trk("ledger", "list", t, "--json").json()["ledger"]
    assert (row["decided"], row["alternatives"], row["why"]) == ("D ±1", ["A (x)", "plain"], "W +")


def test_outcome_reads_a_file(trk, t, tmp_path):
    ok(trk, "close", t, "--resolution", "out-of-scope", "--outcome", note_file(tmp_path, "Dropped ±5 (rare)\n"))
    assert trk("show", t, "--json").json()["item"]["outcome"] == "Dropped ±5 (rare)"


def test_a_file_over_the_cap_is_refused_like_inline_text(trk, t, tmp_path):
    inline = trk("evidence", "add", t, "--kind", "judgment", "--gate", "specd", "--note", "x" * 301)
    from_file = trk("evidence", "add", t, "--kind", "judgment", "--gate", "specd",
                    "--note", note_file(tmp_path, "x" * 301))
    assert inline.code == from_file.code == 2
    assert inline.err == from_file.err


def test_a_literal_without_a_leading_at_is_stored_unchanged(trk, t):
    ok(trk, "evidence", "add", t, "--kind", "judgment", "--gate", "specd", "--note", "a@b - c")
    assert trk("evidence", t, "--json").json()["evidence"][0]["note"] == "a@b - c"


def test_a_missing_file_and_a_second_stdin_flag_are_usage_errors(trk, t, tmp_path, monkeypatch):
    assert trk("evidence", "add", t, "--kind", "judgment", "--gate", "specd", "--note", "@/nope/x").code == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("x\n"))
    assert trk("ledger", "add", t, "--kind", "fork", "--decided", "-", "--alternative", "a", "--why", "-").code == 2
