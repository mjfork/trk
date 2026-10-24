"""The RED review brief (tr-x78m0): a RED ticket's independent auditor writes a `review-brief` section with nine
headings before `trk ledger audit` takes (rule `brief-required`), and the RED-read ask's question shows that
brief above the PR, the RED files and the approve command: "brief pending" before it is complete, refreshed
whenever the section changes. YELLOW and GREEN tickets are unchanged.

Every test runs against a real throwaway git repo whose rules file makes `contract/**` RED."""

import pytest

from trk.blast import BRIEF_HEADINGS, brief_missing
from test_blast_and_verified import BRIEF, RULES, item, lane_repo, lane_ticket, merge, ok, refusal, write_brief
from test_red_read import PR, asks_under, red_ticket

V = ("--as", "reviewer", "--session", "V")
AUDIT = ("ledger", "audit")


def audit(trk, t):
    return trk(*AUDIT, t, "--note", "Read the diff", *V, "--json")


def question(trk, t):
    [ask] = [a for a in asks_under(trk, t) if a["status"] == "open"]
    return ok(trk, "section", "get", ask["id"], "question")


@pytest.fixture
def red(trk, tmp_path):
    _, sha, t = red_ticket(trk, tmp_path)
    ok(trk, "actor", "add", "reviewer", "--kind", "agent")
    assert ok(trk, "blast-radius", t, "--as", "worker") == "red"
    return t, sha


def without(*names):
    """BRIEF minus the sections whose heading is in `names`."""
    out, skipping = [], False
    for line in BRIEF.split("\n"):
        if line.startswith("## "):
            skipping = line[3:] in names
        if not skipping:
            out.append(line)
    return "\n".join(out)


# --- ac-1: the audit gate ---

def test_a_red_audit_without_a_brief_is_refused_naming_every_heading(trk, red):
    t, _ = red
    error = audit(trk, t)
    assert error.code == 1
    error = error.error()
    assert error["rule"] == "brief-required" and error["item"] == t
    assert "no review-brief section" in error["message"]
    for heading in BRIEF_HEADINGS:
        assert heading in error["message"]
    assert not [e for e in trk("evidence", t, "--json").json()["evidence"] if e["kind"] == "audit"]


def test_a_brief_missing_headings_is_refused_naming_only_those(trk, red):
    t, _ = red
    write_brief(trk, t, without("Rollback", "Where to look"))
    message = audit(trk, t).error()["message"]
    assert "'Rollback', 'Where to look'" in message
    assert "'Decision'" not in message and "'Evidence'" not in message


def test_a_heading_with_no_text_counts_as_missing(trk, red):
    t, _ = red
    write_brief(trk, t, BRIEF.replace("Text under Rollback.", ""))
    assert "'Rollback'" in audit(trk, t).error()["message"]


def test_a_complete_brief_lets_the_audit_through(trk, red):
    t, _ = red
    write_brief(trk, t)
    assert audit(trk, t).code == 0
    [row] = [e for e in trk("evidence", t, "--json").json()["evidence"] if e["kind"] == "audit"]
    assert (row["gate"], row["actor"]) == ("merged", "reviewer")


def test_the_session_refusal_still_comes_before_the_brief_one(trk, red):
    t, _ = red
    error = refusal(trk, *AUDIT, t, "--note", "n", "--as", "reviewer")
    assert error["rule"] == "session-required"


@pytest.mark.parametrize("text", [
    BRIEF,
    BRIEF.replace("## ", "### "),
    BRIEF.replace("## ", "# "),
    "".join(f"{n}. **{h}**\n\nText.\n\n" for n, h in enumerate(BRIEF_HEADINGS, 1)),
    "".join(f"## {n}. {h}:\n\nText.\n\n" for n, h in enumerate(BRIEF_HEADINGS, 1)),
    BRIEF.replace("RED", "RED").replace("Why it's RED", "Why it’s RED").upper().replace("TEXT UNDER", "text under"),
])
def test_headings_are_read_in_plain_variants(text):
    assert brief_missing(text) == []


def test_a_heading_inside_a_code_fence_is_not_a_heading():
    fenced = "```\n" + BRIEF + "```\n"
    assert brief_missing(fenced) == list(BRIEF_HEADINGS)
    assert brief_missing(None) == list(BRIEF_HEADINGS)


def test_sub_headings_and_lists_inside_a_section_are_its_text():
    text = BRIEF.replace("Text under Evidence.", "### Tests\n\n- test_a passed\n")
    assert brief_missing(text) == []


@pytest.mark.parametrize("files, lane", [(["src/a.py"], "yellow"), (["README.md"], "green")])
def test_yellow_and_green_audits_need_no_brief(trk, tmp_path, files, lane):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, files)
    t = lane_ticket(trk, path, sha)
    ok(trk, "actor", "add", "worker", "--kind", "agent")
    ok(trk, "actor", "add", "reviewer", "--kind", "agent")
    assert ok(trk, "blast-radius", t, "--as", "worker") == lane
    assert audit(trk, t).code == 0


def test_an_unrated_ticket_audits_without_a_brief(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["contract/x.json"])
    t = lane_ticket(trk, path, sha)
    ok(trk, "actor", "add", "reviewer", "--kind", "agent")
    assert audit(trk, t).code == 0


# --- the Merged gate asks for the brief too, whatever order the audit and the rating came in ---

def gate_needs(trk, t):
    return {m["need"]: m for m in item(trk, t)["gate"]["missing"]}


def test_merged_lacks_the_brief_of_a_red_ticket_audited_before_it_was_rated(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["contract/x.json"])
    t = lane_ticket(trk, path, sha)
    ok(trk, "actor", "add", "worker", "--kind", "agent")
    ok(trk, "actor", "add", "reviewer", "--kind", "agent")
    assert audit(trk, t).code == 0  # unrated: no brief asked yet
    assert ok(trk, "blast-radius", t, "--as", "worker") == "red"
    need = gate_needs(trk, t)["review-brief"]
    assert need["missing"] == list(BRIEF_HEADINGS)
    write_brief(trk, t)
    assert "review-brief" not in gate_needs(trk, t)


def test_merged_names_only_the_headings_a_red_brief_still_lacks(trk, red):
    t, _ = red
    write_brief(trk, t, without("Rollback", "Where to look"))
    assert gate_needs(trk, t)["review-brief"]["missing"] == ["Rollback", "Where to look"]
    write_brief(trk, t, BRIEF.replace("Text under Rollback.", ""))
    assert gate_needs(trk, t)["review-brief"]["missing"] == ["Rollback"]
    text = ok(trk, "show", t)
    assert "missing: review-brief (missing Rollback)" in text


def test_a_red_re_rating_after_a_green_one_brings_the_brief_back_into_the_gate(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["contract/x.json"])
    t = lane_ticket(trk, path, sha)
    for actor in ("worker", "reviewer"):
        ok(trk, "actor", "add", actor, "--kind", "agent")
    assert ok(trk, "blast-radius", t, "--as", "worker") == "red"
    assert ok(trk, "blast-radius", t, "--base", sha, "--as", "worker") == "green"
    assert "review-brief" not in gate_needs(trk, t)
    assert audit(trk, t).code == 0  # latest rating is GREEN: no brief asked
    assert ok(trk, "blast-radius", t, "--as", "worker") == "red"
    assert gate_needs(trk, t)["review-brief"]["missing"] == list(BRIEF_HEADINGS)


def test_a_brief_removed_after_the_audit_is_missing_at_merged(trk, red):
    t, sha = red
    write_brief(trk, t)
    assert audit(trk, t).code == 0
    assert "review-brief" not in gate_needs(trk, t)
    ok(trk, "section", "rm", t, "review-brief")
    assert gate_needs(trk, t)["review-brief"]["missing"] == list(BRIEF_HEADINGS)
    merge(trk, t, sha)
    error = refusal(trk, "advance", t)
    assert "review-brief" in [m["need"] for m in error["missing"]]


@pytest.mark.parametrize("files, lane", [(["src/a.py"], "yellow"), (["README.md"], "green")])
def test_the_merged_gate_of_a_yellow_or_green_ticket_never_asks_for_a_brief(trk, tmp_path, files, lane):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, files)
    t = lane_ticket(trk, path, sha)
    ok(trk, "actor", "add", "worker", "--kind", "agent")
    assert ok(trk, "blast-radius", t, "--as", "worker") == lane
    assert "review-brief" not in gate_needs(trk, t)


# --- ac-2: the ask shows the brief ---

def test_the_ask_says_brief_pending_before_a_brief_exists(trk, red):
    t, sha = red
    text = question(trk, t)
    assert "brief pending" in text
    assert text.index("brief pending") < text.index("PR: ") < text.index("RED files:") < text.index("trk review record")


def test_the_ask_shows_the_brief_above_the_pr_files_and_approve_command(trk, red):
    t, sha = red
    write_brief(trk, t)
    text = question(trk, t)
    assert "brief pending" not in text
    assert "## Decision" in text and "Text under Recommendation." in text
    assert text.index("## Recommendation") < text.index(f"PR: {PR}") < text.index("`contract/x.json`") \
        < text.index("trk review record")
    assert f"rated RED at {sha}" in text.split("\n\n")[0]


def test_an_incomplete_brief_is_still_pending_and_names_what_it_lacks(trk, red):
    t, _ = red
    write_brief(trk, t, without("Rollback"))
    text = question(trk, t)
    assert "brief pending" in text and "still lacks Rollback" in text
    assert "## Decision" not in text


def test_changing_or_removing_the_brief_refreshes_the_open_ask(trk, red):
    t, _ = red
    [ask] = asks_under(trk, t)
    write_brief(trk, t)
    assert "Text under Rollback." in question(trk, t)
    write_brief(trk, t, BRIEF.replace("Text under Rollback.", "Revert the merge commit."))
    text = question(trk, t)
    assert "Revert the merge commit." in text and "Text under Rollback." not in text
    assert [a["id"] for a in asks_under(trk, t)] == [ask["id"]]
    ok(trk, "section", "rm", t, "review-brief")
    assert "brief pending" in question(trk, t)


def test_a_re_rating_keeps_the_brief_in_the_refreshed_question(trk, red):
    t, _ = red
    write_brief(trk, t)
    ok(trk, "blast-radius", t, "--as", "worker")
    assert "## Decision" in question(trk, t)


def test_an_answered_ask_is_not_rewritten_when_the_brief_changes(trk, red):
    t, sha = red
    [ask] = asks_under(trk, t)
    ok(trk, "review", "record", t, "--ref", "https://github.com/o/r/pull/1#pullrequestreview-1", "--sha", sha,
       "approved", "--note", "Read it.", "--as", "michael")
    before = ok(trk, "section", "get", ask["id"], "question")
    write_brief(trk, t)
    assert ok(trk, "section", "get", ask["id"], "question") == before


def test_the_item_page_and_the_needs_you_page_show_the_brief_above_the_files(trk, red):
    t, _ = red
    [ask] = asks_under(trk, t)
    for target in (t, "needs-you:michael"):
        html = ok(trk, "pages", "render", target, "--out", "-")
        assert "brief pending" in html
        assert html.index("brief pending") < html.index("RED files:") < html.index("trk review record")
    write_brief(trk, t)
    for target in (t, "needs-you:michael"):
        html = ok(trk, "pages", "render", target, "--out", "-")
        assert "brief pending" not in html
        assert html.index("Text under Decision.") < html.index("RED files:") < html.index("trk review record")


# --- ac-3: the guidance names the brief and the rule ---

@pytest.mark.parametrize("path", ["skills/trk/SKILL.md", "skills/trk-drive-program/SKILL.md", "docs/agents.md"])
def test_the_guidance_describes_the_brief_and_its_rule(path):
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / path).read_text()
    assert "review-brief" in text and "brief-required" in text
    for heading in BRIEF_HEADINGS[:1] + BRIEF_HEADINGS[-1:]:
        assert heading in text
    if path != "skills/trk-drive-program/SKILL.md":
        assert all(heading in text for heading in BRIEF_HEADINGS)
