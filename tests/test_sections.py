"""Sections: named body parts, written at creation or later, read back raw, listed by `show`."""

import io
import json
import sqlite3
import sys

import pytest
from gated import DEST, new_flags

MAX = 256 * 1024


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
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


def stored_handles(trk_home, item_id):
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        return [r[0] for r in conn.execute("SELECT handle FROM sections WHERE item_id = ? ORDER BY rowid", (item_id,))]
    finally:
        conn.close()


def sections(trk, item_id, *flags):
    result = trk("show", item_id, "--json", *flags)
    assert result.code == 0, result.err
    return result.json()["item"]["sections"]


# --- handles and bodies -----------------------------------------------------------------------


@pytest.mark.parametrize("handle", ["Notes", "two words", "under_score", "-lead", "trail-", "double--dash", ""])
def test_a_handle_that_is_not_a_lowercase_kebab_slug_is_a_usage_error(store, stdin, handle):
    p = new(store, "program", "P")
    stdin("body")
    result = store("section", "set", p, handle, "--json")
    assert result.code == 2
    assert result.error()["code"] == "usage"


def test_kebab_slugs_with_digits_are_handles(store, stdin):
    p = new(store, "program", "P")
    stdin("x")
    assert store("section", "set", p, "ac-2").code == 0
    assert store("section", "get", p, "ac-2").out == "x"


def test_a_body_up_to_256_kib_is_kept_and_a_larger_one_is_refused(store, stdin):
    p = new(store, "program", "P")
    stdin("a" * MAX)
    assert store("section", "set", p, "notes").code == 0
    stdin("a" * (MAX + 1))
    result = store("section", "set", p, "notes", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "section-too-large"
    assert len(store("section", "get", p, "notes").out) == MAX


def test_the_size_limit_counts_utf8_bytes(store, stdin):
    p = new(store, "program", "P")
    stdin("é" * (MAX // 2 + 1))
    assert store("section", "set", p, "notes").code == 1


def test_bodies_are_stored_unparsed(store, stdin):
    p = new(store, "program", "P")
    body = "# Heading\n\n- [ ] item\n\n```\ncode\n```\n\n<!-- comment -->\n"
    stdin(body)
    store("section", "set", p, "notes")
    assert store("section", "get", p, "notes").out == body


# --- new --section ----------------------------------------------------------------------------


def test_new_takes_sections_from_files_and_stdin(store, stdin, tmp_path, trk_home):
    later = tmp_path / "later.md"
    later.write_text("Not yet.\n")
    stdin("Some notes.\n")
    p = new(store, "program", "P", "--section", f"not-yet-specified=@{later}", "--section", "notes=-")
    assert store("section", "get", p, "not-yet-specified").out == "Not yet.\n"
    assert store("section", "get", p, "notes").out == "Some notes.\n"
    created = [r for r in journal(trk_home) if r["change"] == "item.created"]
    assert created[-1]["data"]["sections"] == ["not-yet-specified", "notes"]


def test_new_without_sections_lists_none_on_item_created(store, trk_home):
    new(store, "program", "P")
    created = [r for r in journal(trk_home) if r["change"] == "item.created"]
    assert created[-1]["data"]["sections"] == []


@pytest.mark.parametrize(
    "spec",
    ["notes", "notes=body", "Notes=-", "=-", "notes=@"],
)
def test_a_malformed_section_flag_is_a_usage_error(store, stdin, spec):
    stdin("x")
    result = store("new", "program", "P", "--section", spec, "--json", *DEST)
    assert result.code == 2, result.err


def test_new_refuses_a_handle_given_twice(store, stdin, tmp_path):
    f = tmp_path / "f.md"
    f.write_text("x")
    result = store("new", "program", "P", "--section", f"notes=@{f}", "--section", f"notes=@{f}", *DEST)
    assert result.code == 2


def test_new_reads_stdin_for_one_section_only(store, stdin):
    stdin("x")
    result = store("new", "program", "P", "--section", "notes=-", "--section", "not-yet-specified=-", *DEST)
    assert result.code == 2


def test_new_with_an_unreadable_file_is_a_usage_error_and_creates_nothing(store, tmp_path, trk_home):
    result = store("new", "program", "P", "--section", f"notes=@{tmp_path / 'missing.md'}", *DEST)
    assert result.code == 2
    assert "item.created" not in [r["change"] for r in journal(trk_home)]


def test_new_with_an_oversized_section_creates_nothing(store, stdin, trk_home):
    stdin("a" * (MAX + 1))
    result = store("new", "program", "P", "--section", "notes=-", "--json", *DEST)
    assert result.code == 1
    assert result.error()["rule"] == "section-too-large"
    assert "item.created" not in [r["change"] for r in journal(trk_home)]


# --- section set / rm / get -------------------------------------------------------------------


def test_section_set_reads_stdin_by_default_and_journals_the_handle_without_values(store, stdin, trk_home):
    p = new(store, "program", "P")
    stdin("first\n")
    result = store("section", "set", p, "notes")
    assert result.code == 0, result.err
    assert result.out.strip() == p
    row = journal(trk_home)[-1]
    assert row["change"] == "item.updated"
    assert row["item"] == p
    assert row["data"] == {"fields": ["section:notes"], "old": {}, "new": {}}


def test_section_set_from_a_file_replaces_the_body(store, stdin, tmp_path):
    p = new(store, "program", "P")
    stdin("first")
    store("section", "set", p, "notes")
    f = tmp_path / "notes.md"
    f.write_text("second")
    assert store("section", "set", p, "notes", "--file", str(f)).code == 0
    assert store("section", "get", p, "notes").out == "second"


def test_section_set_bumps_updated_at(store, stdin, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-10T10:00:00Z")
    p = new(store, "program", "P")
    monkeypatch.setenv("TRK_NOW", "2026-10-10T11:00:00Z")
    stdin("x")
    store("section", "set", p, "notes")
    item = store("show", p, "--json").json()["item"]
    assert item["updated_at"] == "2026-10-10T11:00:00.000Z"


def test_setting_the_same_body_again_writes_no_row(store, stdin, trk_home):
    p = new(store, "program", "P")
    stdin("same")
    store("section", "set", p, "notes")
    before = len(journal(trk_home))
    stdin("same")
    assert store("section", "set", p, "notes").code == 0
    assert len(journal(trk_home)) == before


def test_an_empty_body_is_a_present_section(store, stdin):
    p = new(store, "program", "P")
    stdin("")
    assert store("section", "set", p, "notes").code == 0
    notes = next(s for s in sections(store, p) if s["handle"] == "notes")
    assert notes == {"handle": "notes", "expected": True, "present": True, "body": ""}


def test_section_rm_removes_it_and_journals_the_same_way(store, stdin, trk_home):
    p = new(store, "program", "P")
    stdin("x")
    store("section", "set", p, "extra")
    result = store("section", "rm", p, "extra")
    assert result.code == 0, result.err
    row = journal(trk_home)[-1]
    assert row["change"] == "item.updated"
    assert row["data"] == {"fields": ["section:extra"], "old": {}, "new": {}}
    assert store("section", "get", p, "extra").code == 3
    assert stored_handles(trk_home, p) == []


def test_section_rm_of_an_absent_section_is_a_no_op(store, trk_home):
    p = new(store, "program", "P")
    before = len(journal(trk_home))
    assert store("section", "rm", p, "notes").code == 0
    assert len(journal(trk_home)) == before


def test_section_get_prints_the_raw_body(store, stdin):
    p = new(store, "program", "P")
    stdin("no trailing newline")
    store("section", "set", p, "notes")
    assert store("section", "get", p, "notes").out == "no trailing newline"


def test_section_get_of_an_absent_section_exits_3_even_when_expected(store):
    p = new(store, "program", "P")
    result = store("section", "get", p, "notes", "--json")
    assert result.code == 3
    assert result.error()["code"] == "not-found"


def test_section_get_json(store, stdin):
    p = new(store, "program", "P")
    stdin("body")
    store("section", "set", p, "notes")
    assert store("section", "get", p, "notes", "--json").json() == {
        "schema": 1, "item": p, "handle": "notes", "body": "body",
    }


def test_section_verbs_on_an_unknown_item_exit_3(store, stdin):
    stdin("x")
    assert store("section", "set", "tr-zzzzz", "notes").code == 3
    assert store("section", "get", "tr-zzzzz", "notes").code == 3
    assert store("section", "rm", "tr-zzzzz", "notes").code == 3


def test_section_set_matches_ids_like_every_verb(store, stdin):
    p = new(store, "program", "P")
    stdin("x")
    assert store("section", "set", p.upper(), "notes").code == 0
    assert store("section", "get", p, "notes").out == "x"


# --- show --------------------------------------------------------------------------------------


EXPECTED = {
    ("program",): ["destination", "notes", "not-yet-specified"],
    ("project",): ["problem", "solution", "user-stories", "implementation-decisions", "testing-decisions",
                   "acceptance-criteria", "out-of-scope"],
    ("work-item", "--form", "spec-bound"): ["what-to-build", "acceptance-criteria"],
    ("work-item", "--form", "self-contained"): ["current-behaviour", "desired-behaviour", "acceptance-criteria"],
    ("work-item", "--form", "decision", "--subtype", "task"): ["question", "resolution"],
}


@pytest.mark.parametrize("kind", list(EXPECTED), ids=lambda k: k[-1] if k[0] == "work-item" and len(k) == 3 else k[0])
def test_show_lists_the_expected_sections_of_each_kind_and_form_empty_when_missing(store, kind):
    item_id = new(store, kind[0], "X", *kind[1:])
    listed = sections(store, item_id)
    assert [s["handle"] for s in listed] == EXPECTED[kind]
    # The creation gate's sections and criterion are present (and the criteria section generated);
    # every other expected section is listed empty.
    gated = {flag.partition("=")[0] for flag in new_flags(kind) if "=" in flag}
    for s in listed:
        if s.get("generated"):
            assert s["handle"] in ("destination", "acceptance-criteria")
            assert s["present"] == (kind[0] != "project")
        else:
            assert s == {"handle": s["handle"], "expected": True, "present": s["handle"] in gated,
                         "body": s["body"] if s["handle"] in gated else None}


def test_show_puts_expected_sections_in_table_order_then_extras_in_creation_order(store, stdin, trk_home):
    p = new(store, "program", "P")
    for handle in ("zeta", "not-yet-specified", "alpha", "notes"):
        stdin(f"{handle} body")
        store("section", "set", p, handle)
    listed = sections(store, p)
    assert [s["handle"] for s in listed] == ["destination", "notes", "not-yet-specified", "zeta", "alpha"]
    assert [s["expected"] for s in listed] == [True, True, True, False, False]
    assert [s["body"] for s in listed] == ["- [ ] ac-1 The destination holds. — no evidence\n", "notes body",
                                           "not-yet-specified body", "zeta body", "alpha body"]
    # Nothing generated is stored: only the four handles that were written.
    assert stored_handles(trk_home, p) == ["zeta", "not-yet-specified", "alpha", "notes"]


def test_rewriting_an_extra_keeps_its_creation_order(store, stdin):
    p = new(store, "program", "P")
    for handle in ("one", "two", "one"):
        stdin(handle)
        store("section", "set", p, handle)
    assert [s["handle"] for s in sections(store, p)][3:] == ["one", "two"]


def test_show_no_sections_omits_bodies(store, stdin):
    p = new(store, "program", "P")
    stdin("secret body")
    store("section", "set", p, "notes")
    listed = sections(store, p, "--no-sections")
    assert [s["handle"] for s in listed] == ["destination", "notes", "not-yet-specified"]
    assert all("body" not in s for s in listed)
    human = store("show", p, "--no-sections")
    assert "secret body" not in human.out
    assert "notes" in human.out


def test_human_show_prints_each_section_under_its_handle(store, stdin):
    p = new(store, "program", "P")
    stdin("Where we are going.\n")
    store("section", "set", p, "notes")
    out = store("show", p).out
    assert "## destination\n\n- [ ] ac-1 The destination holds. — no evidence\n" in out
    assert "## notes\n\nWhere we are going.\n" in out
    assert out.index("## destination") < out.index("## notes") < out.index("## not-yet-specified")


def test_tree_json_shares_the_item_shape_with_sections(store, stdin):
    p = new(store, "program", "P")
    stdin("d")
    store("section", "set", p, "notes")
    tree_item = store("tree", "--json").json()["items"][0]
    assert tree_item["sections"] == sections(store, p)
