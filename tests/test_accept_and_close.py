"""The `accept` and `close` gates (Stage gates, Spec state and Closing done): `spec accept` needs a complete
spec, a live criterion and a `judgment` since the last return to draft; `spec draft` needs a reason and moves
Spec'd tickets back to Framed; projects, programs and decisions close `done` only with their gate met."""

import io

import pytest

from gated import DEST, PROJECT_SECTIONS, section_file, ticket


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def item(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


def feed(trk, since=0):
    return trk("changes", "--since", str(since), "--json").json()["changes"]


def head(trk):
    return trk("changes", "--since", "0", "--json").json()["head"]


def refusal(trk, *argv):
    result = trk(*argv, "--json")
    assert result.code == 1, (argv, result.out, result.err)
    error = result.error()
    assert error["rule"] == "gate-unmet"
    return error


def write_spec(trk, project, skip=()):
    for handle in PROJECT_SECTIONS:
        if handle not in skip:
            ok(trk, "section", "set", project, handle, "--file", str(section_file(handle)))


def judge(trk, item_id, gate, *flags, note="Reviewed and complete."):
    return ok(trk, "evidence", "add", item_id, "--kind", "judgment", "--gate", gate, *flags, "--note", note)


@pytest.fixture
def world(trk):
    ok(trk, "init", "--me", "michael")
    ok(trk, "actor", "add", "worker", "--kind", "agent")
    p = ok(trk, "new", "program", "P", *DEST)
    k = ok(trk, "new", "project", "K", "--parent", p)
    return {"P": p, "K": k}


def spec_bound(trk, parent, title="T"):
    return ok(trk, "new", "work-item", title, "--form", "spec-bound", "--parent", parent, *ticket("spec-bound"))


# --- spec accept ---

def test_accept_lists_every_empty_section_the_criterion_and_judgment(trk, world):
    error = refusal(trk, "spec", "accept", world["K"])
    assert error["gate"] == "accept"
    assert error["item"] == world["K"]
    assert error["missing"] == [*({"need": "section", "section": h} for h in PROJECT_SECTIONS),
                                {"need": "criterion"}, {"need": "judgment"}]
    assert item(trk, world["K"])["spec_state"] == "draft"


def test_accept_refusal_writes_nothing(trk, world):
    before = head(trk)
    refusal(trk, "spec", "accept", world["K"])
    assert head(trk) == before


def test_accept_counts_whitespace_sections_as_empty(trk, world):
    write_spec(trk, world["K"], skip=("solution",))
    ok(trk, "section", "set", world["K"], "solution", "--file", str(section_file("solution", "  \n")))
    ok(trk, "criterion", "add", world["K"], "It ships.")
    judge(trk, world["K"], "accept")
    assert refusal(trk, "spec", "accept", world["K"])["missing"] == [{"need": "section", "section": "solution"}]


def test_accept_needs_a_live_criterion_and_a_judgment(trk, world):
    write_spec(trk, world["K"])
    assert refusal(trk, "spec", "accept", world["K"])["missing"] == [{"need": "criterion"}, {"need": "judgment"}]
    ok(trk, "criterion", "add", world["K"], "It ships.")
    assert refusal(trk, "spec", "accept", world["K"])["missing"] == [{"need": "judgment"}]


def test_accept_moves_framed_tickets_to_specd_and_records_the_acceptor(trk, world):
    t = spec_bound(trk, world["K"])
    assert item(trk, t)["stage"] == "framed"
    write_spec(trk, world["K"])
    ok(trk, "criterion", "add", world["K"], "It ships.")
    judgment = judge(trk, world["K"], "accept", "--as", "worker", "--session", "33333333-3333-4333-8333-333333333333")
    since = head(trk)
    ok(trk, "spec", "accept", world["K"])
    assert item(trk, world["K"])["spec_state"] == "accepted"
    assert item(trk, t)["stage"] == "specd"
    rows = feed(trk, since)
    assert [r["change"] for r in rows] == ["spec.changed", "stage.changed"]
    assert rows[0]["data"] == {"old": "draft", "new": "accepted", "acceptor": "worker", "judgment": judgment}
    assert rows[0]["actor"] == "michael"
    assert rows[1]["item"] == t
    assert rows[1]["data"] == {"old": "framed", "new": "specd", "via": "spec-accept"}


def test_accept_leaves_tickets_of_a_nested_project_and_closed_ones(trk, world):
    inner = ok(trk, "new", "project", "Inner", "--parent", world["K"])
    nested = spec_bound(trk, inner, "nested")
    closed = spec_bound(trk, world["K"], "closed")
    ok(trk, "close", closed, "--resolution", "wont-do", "--outcome", "Dropped.")
    write_spec(trk, world["K"])
    ok(trk, "criterion", "add", world["K"], "It ships.")
    judge(trk, world["K"], "accept")
    ok(trk, "spec", "accept", world["K"])
    assert item(trk, nested)["stage"] == "framed"
    assert item(trk, closed)["stage"] == "framed"


def accepted(trk, world):
    write_spec(trk, world["K"])
    ok(trk, "criterion", "add", world["K"], "It ships.")
    judge(trk, world["K"], "accept")
    ok(trk, "spec", "accept", world["K"])
    return world["K"]


# --- spec draft ---

def test_draft_needs_a_reason(trk, world):
    accepted(trk, world)
    result = trk("spec", "draft", world["K"], "--json")
    assert result.code == 2
    assert item(trk, world["K"])["spec_state"] == "accepted"


def test_draft_moves_specd_tickets_back_and_keeps_built_ones(trk, world):
    t = spec_bound(trk, world["K"])
    built = spec_bound(trk, world["K"], "built")
    accepted(trk, world)
    ok(trk, "evidence", "add", built, "--kind", "pr", "--gate", "built", "--ref", "none", "--sha", "abcdef1",
       "--note", "x")
    ok(trk, "evidence", "add", built, "--kind", "test-run", "--gate", "built", "--ref", "r", "--sha", "abcdef1",
       "--verdict", "pass", "--note", "x")
    ok(trk, "advance", built)
    since = head(trk)
    ok(trk, "spec", "draft", world["K"], "--reason", "The solution changed.")
    assert item(trk, t)["stage"] == "framed"
    assert "spec-not-accepted" in item(trk, t)["why_not"]
    assert item(trk, built)["stage"] == "built"
    rows = feed(trk, since)
    assert [r["change"] for r in rows] == ["spec.changed", "stage.changed"]
    assert rows[0]["data"] == {"old": "accepted", "new": "draft", "reason": "The solution changed."}
    assert rows[1]["item"] == t
    assert rows[1]["data"] == {"old": "specd", "new": "framed", "via": "spec-draft", "reason": "The solution changed."}


def test_a_judgment_before_a_return_to_draft_does_not_count(trk, world):
    t = spec_bound(trk, world["K"])
    accepted(trk, world)
    ok(trk, "spec", "draft", world["K"], "--reason", "Rethink.")
    error = refusal(trk, "spec", "accept", world["K"])
    assert error["missing"] == [{"need": "judgment"}]
    old = [e for e in trk("evidence", world["K"], "--json").json()["evidence"] if e["gate"] == "accept"]
    assert [(e["counts"], e["reason"]) for e in old] == [(False, "before-draft")]
    judge(trk, world["K"], "accept")
    ok(trk, "spec", "accept", world["K"])
    assert item(trk, t)["stage"] == "specd"


def test_drafting_a_draft_spec_still_needs_its_reason_and_writes_nothing(trk, world):
    assert trk("spec", "draft", world["K"]).code == 2
    before = head(trk)
    ok(trk, "spec", "draft", world["K"], "--reason", "Already.")
    assert head(trk) == before


def test_accepting_an_accepted_spec_writes_nothing(trk, world):
    accepted(trk, world)
    before = head(trk)
    ok(trk, "spec", "accept", world["K"])
    assert head(trk) == before


# --- closing done ---

def test_project_closes_done_only_with_a_passing_close_row_per_criterion(trk, world):
    k = accepted(trk, world)
    dropped = spec_bound(trk, k)
    ok(trk, "close", dropped, "--resolution", "wont-do", "--outcome", "Dropped.")
    error = refusal(trk, "close", k, "--resolution", "done", "--outcome", "Shipped.")
    assert error["gate"] == "close"
    assert error["missing"] == [{"need": "criterion-pass", "criterion": "ac-1", "version": 1, "latest": None}]
    assert item(trk, k)["gate"] == {"next": "close", "missing": error["missing"]}
    judge(trk, k, "close", "--criterion", "ac-1", note=f"Delivered by {dropped}.")
    ok(trk, "close", k, "--resolution", "done", "--outcome", "Shipped.")
    assert item(trk, k)["status"] == "closed"


def test_a_failing_latest_run_blocks_close(trk, world):
    k = accepted(trk, world)
    judge(trk, k, "close", "--criterion", "ac-1")
    fail = ok(trk, "evidence", "add", k, "--kind", "test-run", "--gate", "close", "--criterion", "ac-1",
              "--ref", "e2e", "--verdict", "fail", "--note", "Broke.")
    error = refusal(trk, "close", k, "--resolution", "done", "--outcome", "Shipped.")
    assert error["missing"] == [{"need": "criterion-pass", "criterion": "ac-1", "version": 1,
                                 "latest": {"id": fail, "kind": "test-run", "sha": None, "verdict": "fail"}}]


def test_a_draft_project_with_no_live_criterion_is_refused_spec_accepted_and_criterion(trk, world):
    error = refusal(trk, "close", world["K"], "--resolution", "done", "--outcome", "Shipped.")
    assert error["missing"] == [{"need": "spec-accepted", "project": world["K"]}, {"need": "criterion"}]


def test_a_draft_project_closes_done_only_once_its_spec_is_accepted(trk, world):
    k = world["K"]
    ok(trk, "criterion", "add", k, "Delivered.")
    judge(trk, k, "close", "--criterion", "ac-1")
    error = refusal(trk, "close", k, "--resolution", "done", "--outcome", "Shipped.")
    assert error["gate"] == "close"
    assert error["missing"] == [{"need": "spec-accepted", "project": k}]
    assert item(trk, k)["status"] == "open"
    write_spec(trk, k)
    judge(trk, k, "accept")
    ok(trk, "spec", "accept", k)
    ok(trk, "close", k, "--resolution", "done", "--outcome", "Shipped.")
    assert item(trk, k)["status"] == "closed"


def test_a_draft_project_closes_with_the_other_resolutions(trk, world):
    other = ok(trk, "new", "project", "K2", "--parent", world["P"])
    third = ok(trk, "new", "project", "K3", "--parent", world["P"])
    fourth = ok(trk, "new", "project", "K4", "--parent", world["P"])
    ok(trk, "close", world["K"], "--resolution", "superseded", "--by", other, "--outcome", "Replaced.")
    ok(trk, "close", third, "--resolution", "duplicate", "--of", other, "--outcome", "Same.")
    ok(trk, "close", fourth, "--resolution", "wont-do", "--outcome", "Not now.")
    assert {item(trk, k)["status"] for k in (world["K"], third, fourth)} == {"closed"}


def test_program_with_a_destination_and_no_close_row_is_refused(trk, world):
    ok(trk, "close", world["K"], "--resolution", "wont-do", "--outcome", "Dropped.")
    error = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")
    assert error["missing"] == [{"need": "criterion-pass", "criterion": "ac-1", "version": 1, "latest": None}]
    ok(trk, "criterion", "edit", world["P"], "ac-1", "The destination holds. accept-by: michael")
    ok(trk, "evidence", "add", world["P"], "--kind", "human-accept", "--gate", "close", "--criterion", "ac-1",
       "--ref", "page", "--verdict", "pass", "--note", "Accepted.")
    ok(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")


def test_a_decision_closes_done_only_with_its_resolution(trk, world):
    d = ok(trk, "new", "work-item", "D", "--form", "decision", "--subtype", "research", "--parent", world["K"])
    error = refusal(trk, "close", d, "--resolution", "done", "--outcome", "Decided.")
    assert error["missing"] == [{"need": "resolution"}]
    assert item(trk, d)["gate"] == {"next": "close", "missing": [{"need": "resolution"}]}
    ok(trk, "section", "set", d, "resolution", "--file", str(section_file("resolution", "Use SQLite.\n")))
    ok(trk, "close", d, "--resolution", "done", "--outcome", "Decided.")


def test_other_resolutions_close_without_the_gate(trk, world):
    d = ok(trk, "new", "work-item", "D", "--form", "decision", "--subtype", "research", "--parent", world["K"])
    ok(trk, "close", d, "--resolution", "wont-do", "--outcome", "Moot.")
    ok(trk, "close", world["K"], "--resolution", "out-of-scope", "--outcome", "Later.")


def test_open_children_refuse_first_and_reads_report_them(trk, world):
    t = spec_bound(trk, world["K"])
    accepted(trk, world)
    result = trk("close", world["K"], "--resolution", "done", "--outcome", "Shipped.", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "open-children"
    assert item(trk, world["K"])["gate"]["missing"][0] == {"need": "open-children", "children": [t]}


def test_open_asks_are_not_open_children_for_the_gate(trk, world, monkeypatch):
    d = ok(trk, "new", "work-item", "D", "--form", "decision", "--subtype", "research", "--parent", world["K"])
    monkeypatch.setattr("sys.stdin", io.StringIO("Which one?\n"))
    ok(trk, "ask", d, "--to", "product", "Which?", "--question", "-", "--as", "worker")
    assert item(trk, d)["gate"]["missing"] == [{"need": "resolution"}]


# --- every item JSON's gate equals the refusal ---

def test_show_gate_missing_equals_each_refusal(trk, world):
    d = ok(trk, "new", "work-item", "D", "--form", "decision", "--subtype", "research", "--parent", world["P"])
    cases = [
        (world["K"], ("spec", "accept", world["K"])),
        (d, ("close", d, "--resolution", "done", "--outcome", "x")),
    ]
    for item_id, argv in cases:
        shown = item(trk, item_id)["gate"]
        error = refusal(trk, *argv)
        assert shown == {"next": error["gate"], "missing": error["missing"]}
    accepted(trk, world)
    ok(trk, "close", d, "--resolution", "wont-do", "--outcome", "x")
    shown = item(trk, world["K"])["gate"]
    assert shown == {"next": "close", "missing": refusal(trk, "close", world["K"], "--resolution", "done",
                                                         "--outcome", "x")["missing"]}
    ok(trk, "close", world["K"], "--resolution", "wont-do", "--outcome", "x")
    shown = item(trk, world["P"])["gate"]
    assert shown == {"next": "close", "missing": refusal(trk, "close", world["P"], "--resolution", "done",
                                                         "--outcome", "x")["missing"]}


def test_show_text_prints_the_accept_gate(trk, world):
    out = ok(trk, "show", world["K"])
    assert "next: accept — 8 missing" in out
    assert "missing: section (section problem)" in out


def test_spec_changed_rows_carry_acceptor_judgment_and_reason(trk, world):
    accepted(trk, world)
    ok(trk, "spec", "draft", world["K"], "--reason", "Rethink.")
    rows = [r["data"] for r in feed(trk) if r["change"] == "spec.changed"]
    assert [sorted(r) for r in rows] == [["acceptor", "judgment", "new", "old"], ["new", "old", "reason"]]
