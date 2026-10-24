"""Stages and criterion rows (Stage gates): tickets born Framed (or Spec'd from data), criteria as
versioned `ac-<n>` rows behind `trk criterion` and `trk criteria`, and the generated sections."""

import io
import sqlite3
import sys

import pytest

from gated import DEST, accepting, section_file, ticket


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
    return trk


@pytest.fixture
def stdin(monkeypatch):
    def feed(text):
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    return feed


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, result.err
    return result.out.strip()


def item(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


def feed(trk, since=0):
    return trk("changes", "--since", str(since), "--json").json()["changes"]


def head(trk):
    return trk("changes", "--since", "0", "--json").json()["head"]


@pytest.fixture
def tree(store):
    """Program P (one destination statement), draft project K and accepted project J under it."""
    p = ok(store, "new", "program", "P", *DEST)
    k = ok(store, "new", "project", "K", "--parent", p)
    j = ok(store, "new", "project", "J", "--parent", p)
    for argv in accepting(j):
        ok(store, *argv)
    return {"P": p, "K": k, "J": j}


# --- creation: the framed gate ---

def test_a_spec_bound_ticket_with_no_section_and_no_criterion_is_refused_and_nothing_is_written(store, tree):
    before = head(store)
    result = store("new", "work-item", "T", "--form", "spec-bound", "--parent", tree["K"], "--json")
    assert result.code == 1
    error = result.error()
    assert (error["rule"], error["gate"], error["item"]) == ("gate-unmet", "framed", None)
    assert error["missing"] == [{"need": "section", "section": "what-to-build"}, {"need": "criterion"}]
    assert head(store) == before
    assert [c["id"] for c in item(store, tree["K"])["children"]] == []


def test_a_self_contained_tickets_missing_list_names_both_written_sections(store, tree):
    result = store("new", "work-item", "S", "--form", "self-contained", "--parent", tree["K"], "--criterion", "c1",
                   "--json")
    assert result.code == 1
    assert result.error()["missing"] == [{"need": "section", "section": "current-behaviour"},
                                         {"need": "section", "section": "desired-behaviour"}]


def test_a_blank_written_section_does_not_count(store, tree, stdin):
    stdin("  \n\n")
    result = store("new", "work-item", "T", "--form", "spec-bound", "--parent", tree["K"],
                   "--section", "what-to-build=-", "--criterion", "c1", "--json")
    assert result.error()["missing"] == [{"need": "section", "section": "what-to-build"}]


@pytest.mark.parametrize("form, flags", [
    ("spec-bound", "--section what-to-build=@FILE"),
    ("self-contained", "--section current-behaviour=@FILE --section desired-behaviour=@FILE"),
])
def test_the_refusal_message_names_the_flags_with_an_example(store, tree, form, flags):
    error = store("new", "work-item", "T", "--form", form, "--parent", tree["K"], "--json").error()
    assert error["message"] == "framed: " + str(len(error["missing"])) + " missing"
    assert flags in error["hint"] and '--criterion "<one line>"' in error["hint"]
    assert f'e.g. trk new work-item "<title>" --form {form} --parent <id> --section' in error["hint"]
    text = store("new", "work-item", "T", "--form", form, "--parent", tree["K"]).err.splitlines()
    assert text[-1] == f"  hint: {error['hint']}"


def test_the_refusal_prints_one_line_per_missing_entry(store, tree):
    result = store("new", "work-item", "T", "--form", "spec-bound", "--parent", tree["K"])
    assert result.code == 1
    assert result.err.splitlines()[1:3] == ["  missing: section (section what-to-build)", "  missing: criterion"]


def test_a_ticket_under_a_draft_project_is_born_framed(store, tree, stdin):
    since = head(store)
    stdin("Build T.\n")
    result = store("new", "work-item", "T", "--form", "spec-bound", "--parent", tree["K"],
                   "--section", "what-to-build=-", "--criterion", "c1", "--json")
    assert result.code == 0, result.err
    t = result.json()["item"]
    assert t["stage"] == "framed"
    rows = feed(store, since)
    assert [r["change"] for r in rows] == ["item.created", "criterion.added", "stage.changed"]
    assert (rows[0]["data"]["stage"], rows[0]["data"]["criteria"]) == ("framed", ["ac-1"])
    assert rows[1]["data"] == {"handle": "ac-1", "text": "c1", "version": 1, "rank": t["criteria"][0]["rank"]}
    assert rows[2]["data"] == {"old": None, "new": "framed", "via": "create"}
    assert all(r["item"] == t["id"] for r in rows)


def test_a_spec_bound_ticket_under_an_accepted_project_is_born_specd(store, tree):
    since = head(store)
    t = ok(store, "new", "work-item", "T", "--form", "spec-bound", "--parent", tree["J"], *ticket("spec-bound"),
           "--criterion", "c2")
    assert item(store, t)["stage"] == "specd"
    rows = feed(store, since)
    assert [r["change"] for r in rows] == ["item.created", "criterion.added", "criterion.added",
                                           "stage.changed", "stage.changed"]
    assert (rows[0]["data"]["stage"], rows[0]["data"]["criteria"]) == ("specd", ["ac-1", "ac-2"])
    assert [r["data"] for r in rows[3:]] == [{"old": None, "new": "framed", "via": "create"},
                                             {"old": "framed", "new": "specd", "via": "create"}]


def test_a_spec_bound_ticket_under_no_project_and_a_self_contained_ticket_stay_framed(store, tree):
    loose = ok(store, "new", "work-item", "L", "--form", "spec-bound", "--parent", tree["P"], *ticket("spec-bound"))
    s = ok(store, "new", "work-item", "S", "--form", "self-contained", "--parent", tree["J"], *ticket("self-contained"))
    assert (item(store, loose)["stage"], item(store, s)["stage"]) == ("framed", "framed")


def test_a_program_needs_a_destination_statement_and_a_project_may_start_with_none(store):
    result = store("new", "program", "P", "--json")
    assert (result.code, result.error()["code"]) == (2, "usage")
    assert 'e.g. trk new program "<title>" --criterion' in result.error()["message"]
    p = ok(store, "new", "program", "P", "--criterion", "d1", "--criterion", "d2")
    assert [c["handle"] for c in item(store, p)["criteria"]] == ["ac-1", "ac-2"]
    j = ok(store, "new", "project", "J", "--parent", p)
    assert item(store, j)["criteria"] == []
    assert item(store, j)["stage"] is None and item(store, p)["stage"] is None


def test_decisions_take_no_criteria(store, tree):
    result = store("new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", tree["J"],
                   "--criterion", "c1", "--json")
    assert (result.code, result.error()["rule"]) == (1, "no-criteria")
    d = ok(store, "new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", tree["J"])
    assert item(store, d)["stage"] is None
    result = store("criterion", "add", d, "c1", "--json")
    assert (result.code, result.error()["rule"]) == (1, "no-criteria")
    assert store("criteria", d, "--json").error()["rule"] == "no-criteria"


@pytest.mark.parametrize("kind", [("program", "P"), ("project", "J"), ("work-item", "T", "--form", "spec-bound")])
def test_new_refuses_a_written_generated_section(store, tree, kind):
    handle = "destination" if kind[0] == "program" else "acceptance-criteria"
    result = store("new", *kind, "--parent", tree["P"] if kind[0] != "program" else tree["P"],
                   "--section", f"{handle}=@{section_file(handle)}", "--criterion", "c1", "--json")
    assert (result.code, result.error()["rule"]) == (1, "generated-section")
    assert "--criterion" in result.error()["message"]


def test_a_decision_may_still_write_an_acceptance_criteria_section(store, tree):
    d = ok(store, "new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", tree["J"],
           "--section", f"acceptance-criteria=@{section_file('acceptance-criteria', 'Free text.\n')}")
    assert ok(store, "section", "get", d, "acceptance-criteria") == "Free text."


@pytest.mark.parametrize("text", ["", "   ", "two\nlines", "x" * 501])
def test_a_criterion_is_one_line_of_at_most_500_chars(store, tree, text):
    result = store("new", "program", "Q", "--criterion", text, "--json")
    assert result.code == 2
    t = ok(store, "new", "work-item", "T", "--form", "spec-bound", "--parent", tree["J"], *ticket("spec-bound"))
    assert store("criterion", "add", t, text).code == 2
    assert store("criterion", "edit", t, "ac-1", text).code == 2


# --- trk criterion and trk criteria ---

@pytest.fixture
def t(store, tree):
    return ok(store, "new", "work-item", "T", "--form", "spec-bound", "--parent", tree["J"], "--section",
              f"what-to-build=@{section_file('what-to-build')}", "--criterion", "first")


def handles(trk, item_id):
    return [(c["handle"], c["text"]) for c in trk("criteria", item_id, "--json").json()["criteria"]]


def test_add_prints_the_handle_and_placement_orders_the_list(store, t):
    assert ok(store, "criterion", "add", t, "last") == "ac-2"
    assert ok(store, "criterion", "add", t, "top", "--first") == "ac-3"
    assert ok(store, "criterion", "add", t, "before last", "--before", "ac-2") == "ac-4"
    assert ok(store, "criterion", "add", t, "after top", "--after", "AC-3") == "ac-5"
    assert handles(store, t) == [("ac-3", "top"), ("ac-5", "after top"), ("ac-1", "first"),
                                 ("ac-4", "before last"), ("ac-2", "last")]
    assert store("criterion", "add", t, "x", "--before", "ac-9").code == 3


def test_add_journals_criterion_added(store, t):
    since = head(store)
    result = store("criterion", "add", t, "second", "--json")
    criterion = result.json()["criterion"]
    assert (criterion["handle"], criterion["version"], criterion["status"]) == ("ac-2", 1, "none")
    [row] = feed(store, since)
    assert (row["change"], row["item"]) == ("criterion.added", t)
    assert row["data"] == {"handle": "ac-2", "text": "second", "version": 1, "rank": criterion["rank"]}


def test_handles_are_never_reused(store, t):
    ok(store, "criterion", "add", t, "second")
    ok(store, "criterion", "retire", t, "ac-2", "--reason", "dropped")
    assert ok(store, "criterion", "add", t, "third") == "ac-3"


def test_edit_bumps_the_version_and_journals_each_versions_text(store, t):
    since = head(store)
    assert ok(store, "criterion", "edit", t, "ac-1", "first, sharper") == "ac-1"
    ok(store, "criterion", "edit", t, "ac-1", "first, sharpest")
    [c] = store("criteria", t, "--json").json()["criteria"]
    assert (c["text"], c["version"]) == ("first, sharpest", 3)
    assert [r["data"] for r in feed(store, since)] == [
        {"handle": "ac-1", "old_version": 1, "version": 2, "text": "first, sharper"},
        {"handle": "ac-1", "old_version": 2, "version": 3, "text": "first, sharpest"},
    ]


def test_editing_to_the_same_text_changes_nothing(store, t):
    before = head(store)
    ok(store, "criterion", "edit", t, "ac-1", "first")
    assert head(store) == before


def test_retire_keeps_the_row_struck_through_and_the_last_live_one_is_refused(store, t):
    result = store("criterion", "retire", t, "ac-1", "--reason", "only one", "--json")
    assert (result.code, result.error()["rule"]) == (1, "last-criterion")
    ok(store, "criterion", "add", t, "second")
    since = head(store)
    ok(store, "criterion", "retire", t, "ac-1", "--reason", "superseded by ac-2")
    [row] = feed(store, since)
    assert (row["change"], row["data"]) == ("criterion.retired", {"handle": "ac-1", "reason": "superseded by ac-2"})
    listed = store("criteria", t, "--json").json()["criteria"]
    assert [(c["handle"], c["retired"], c["retired_reason"]) for c in listed] == [
        ("ac-1", True, "superseded by ac-2"), ("ac-2", False, None)]
    assert store("criterion", "retire", t, "ac-2", "--reason", "x", "--json").error()["rule"] == "last-criterion"
    assert store("criterion", "edit", t, "ac-1", "revived").code == 2
    assert store("criterion", "retire", t, "ac-1", "--reason", "again").code == 2


def test_criterion_writes_on_a_closed_item_are_refused(store, t):
    ok(store, "close", t, "--resolution", "wont-do", "--outcome", "Dropped.")
    for argv in (("add", t, "late"), ("edit", t, "ac-1", "late"), ("retire", t, "ac-1", "--reason", "late")):
        result = store("criterion", *argv, "--json")
        assert (result.code, result.error()["rule"]) == (1, "closed")


def test_unknown_handles_and_items(store, t):
    assert store("criterion", "edit", t, "ac-7", "x").code == 3
    assert store("criterion", "edit", t, "seven", "x").code == 2
    assert store("criteria", "tr-zzzzz").code == 3


# --- generated sections ---

def test_section_get_and_show_return_the_generated_text(store, t):
    ok(store, "criterion", "add", t, "second")
    ok(store, "criterion", "edit", t, "ac-2", "second, sharper")
    ok(store, "criterion", "add", t, "third")
    ok(store, "criterion", "retire", t, "ac-3", "--reason", "not needed")
    text = ("- [ ] ac-1 first — no evidence\n"
            "- [ ] ac-2 (v2) second, sharper — no evidence\n"
            "- ~~ac-3 third~~ (retired: not needed)\n")
    assert store("section", "get", t, "acceptance-criteria").out == text
    assert store("criteria", t).out == text
    shown = item(store, t)
    section = next(s for s in shown["sections"] if s["handle"] == "acceptance-criteria")
    assert (section["body"], section["present"], section["generated"]) == (text, True, True)
    assert [c["handle"] for c in shown["criteria"]] == ["ac-1", "ac-2", "ac-3"]
    assert f"## acceptance-criteria\n\n{text.rstrip()}" in store("show", t).out


def test_a_programs_destination_is_generated(store, tree):
    assert store("section", "get", tree["P"], "destination").out == "- [ ] ac-1 The destination holds. — no evidence\n"


def test_a_project_with_no_criteria_has_no_generated_text(store, tree):
    assert store("section", "get", tree["K"], "acceptance-criteria").code == 3
    section = next(s for s in item(store, tree["K"])["sections"] if s["handle"] == "acceptance-criteria")
    assert (section["present"], section["body"]) == (False, None)


@pytest.mark.parametrize("verb", ["set", "rm"])
def test_section_set_and_rm_refuse_a_generated_section(store, tree, t, verb):
    for item_id, handle in ((t, "acceptance-criteria"), (tree["J"], "acceptance-criteria"), (tree["P"], "destination")):
        argv = ("section", verb, item_id, handle) + (("--file", str(section_file(handle))) if verb == "set" else ())
        result = store(*argv, "--json")
        assert (result.code, result.error()["rule"]) == (1, "generated-section")
        assert "trk criterion" in result.error()["message"]


def test_items_that_take_no_stage_have_a_null_stage(store, tree):
    d = ok(store, "new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", tree["J"])
    assert [item(store, i)["stage"] for i in (tree["P"], tree["J"], d)] == [None, None, None]
    assert "stage:" not in store("show", d).out


def test_show_text_names_the_stage_in_its_pipeline_and_what_the_next_gate_lacks(store, t):
    lines = store("show", t).out.splitlines()
    start = lines.index("  stage: specd  (framed → [specd] → built → merged → verified)")
    assert lines[start + 1:start + 4] == ["  next: built — 2 missing", "    missing: pr",
                                          "    missing: test-run-pass"]


# --- the append-only tables ---

# One planted row per table (the triggers fire per row), with only its NOT NULL columns.
PLANTED = {
    "evidence": "INSERT INTO evidence (id, item_id, gate, kind, note, actor, seq, created_at)"
                " VALUES ('e00001', :item, 'specd', 'judgment', 'n', 'michael', 1, 'now')",
    "ledger": "INSERT INTO ledger (item_id, n, kind, decided, why, origin, actor, created_at)"
              " VALUES (:item, 1, 'fork', 'd', 'w', 'builder', 'michael', 'now')",
    "ledger_verdicts": "INSERT INTO ledger_verdicts (item_id, n, verdict, note, actor, seq, created_at)"
                       " VALUES (:item, 1, 'sound', 'n', 'michael', 1, 'now')",
    "ledger_settlements": "INSERT INTO ledger_settlements (item_id, n, settlement, actor, seq, created_at)"
                          " VALUES (:item, 1, 'deferred', 'michael', 1, 'now')",
    "ledger_promotions": "INSERT INTO ledger_promotions (item_id, n, adr, actor, seq, created_at)"
                         " VALUES (:item, 1, 'docs/adr/x.md', 'michael', 1, 'now')",
}


@pytest.mark.parametrize("table", list(PLANTED))
def test_update_and_delete_on_the_append_only_tables_abort(store, trk_home, t, table):
    conn = sqlite3.connect(trk_home / "store.db", isolation_level=None)
    try:
        conn.execute(PLANTED[table], {"item": t})
        for statement in (f"UPDATE {table} SET item_id = item_id", f"DELETE FROM {table}"):
            with pytest.raises(sqlite3.IntegrityError, match=f"{table} is append-only"):
                conn.execute(statement)
    finally:
        conn.close()

