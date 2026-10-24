"""A RED-read ask is held until its ticket's review brief is complete (tr-4bhay; Michael, 2026-10-24: "something
rated red needs to have enough context and information that you know why it is red and what risk you are taking
and what the consequences could be"). A held ask stays open, but `trk waiting` for its addressee, the Needs-you
page and the program pages leave it out; its asker sees it marked held. Setting a complete brief releases it,
and its wait clock starts then. A direct `trk review record ... approved` is refused `no-brief` until the brief
is complete. Non-RED asks are untouched.

Every test runs against a real throwaway git repo whose rules file makes `contract/**` RED."""

import json

import pytest

from trk.blast import BRIEF_HEADINGS
from test_blast_and_verified import BRIEF, RULES, item, lane_repo, lane_ticket, ok, refusal, write_brief
from test_red_read import asks_under, red_ticket

OPENED = "2026-10-24T10:00:00+00:00"
RELEASED = "2026-10-24T12:00:00+00:00"
LATER = "2026-10-24T12:30:00+00:00"


@pytest.fixture
def clock(monkeypatch):
    def at(moment):
        monkeypatch.setenv("TRK_NOW", moment)
    return at


@pytest.fixture
def red(trk, tmp_path, clock):
    """A RED ticket whose RED-read ask opened at OPENED with no brief: (ticket, head sha, ask id)."""
    _, sha, t = red_ticket(trk, tmp_path)
    clock(OPENED)
    assert ok(trk, "blast-radius", t, "--as", "worker") == "red"
    [ask] = asks_under(trk, t)
    return t, sha, ask["id"]


def waiting(trk, *flags):
    return trk("waiting", *flags, "--json").json()["asks"]


def ids(asks):
    return [a["id"] for a in asks]


def direct(trk, t, sha):
    return trk("review", "record", t, "--sha", sha, "approved", "--note", "Read it.", "--as", "michael", "--json")


def test_a_briefless_red_ask_is_absent_for_its_addressee_in_text_and_json(trk, red):
    t, _, ask = red
    assert ids(waiting(trk, "--me", "michael")) == []
    assert ask not in trk("waiting", "--me", "michael").out
    assert ask not in trk("waiting", "--me", "michael").out
    assert item(trk, ask)["status"] == "open"  # held, not refused or closed
    assert [o["id"] for o in item(trk, t)["open_asks"]] == [ask]  # the ticket still waits on it


def test_the_needs_you_page_leaves_a_held_ask_out_and_says_nothing_waits(trk, red):
    _, _, ask = red
    html = ok(trk, "pages", "render", "needs-you:michael", "--out", "-")
    assert ask not in html and "brief pending" not in html
    assert "Nothing is waiting on you" in html


def blocked_section(html):
    return html.replace("'", '"').split('data-comment-id="blocked"', 1)[1].split("</section>", 1)[0]


def test_the_blocked_on_you_table_leaves_a_held_ask_out_until_the_brief_is_complete(trk, red):
    t, _, ask = red
    held = blocked_section(ok(trk, "pages", "render", "needs-you:michael", "--out", "-"))
    assert "engineering" not in held and "<table>" not in held and "None" in held
    write_brief(trk, t)
    released = blocked_section(ok(trk, "pages", "render", "needs-you:michael", "--out", "-"))
    assert "engineering" in released and "<table>" in released


def test_a_program_page_does_not_list_a_held_ask_as_waiting_on_people(trk, red):
    t, _, ask = red
    html = ok(trk, "pages", "render", t, "--out", "-")
    assert f'data-handle="ask-{ask}"' not in html.replace("'", '"') and "Nothing is waiting on people" in html


def test_the_asker_and_all_text_list_it_marked_held_with_the_missing_headings(trk, red):
    _, _, ask = red
    for flags in (("--me", "worker"), ("--all",)):
        [line] = [l for l in trk("waiting", *flags).out.splitlines() if l.startswith(ask)]
        assert "held: review brief missing: " in line
        for heading in BRIEF_HEADINGS:
            assert heading in line


def test_the_asker_s_json_lists_it_with_held_and_all_json_leaves_it_out_until_released(trk, red):
    t, _, ask = red
    [entry] = waiting(trk, "--me", "worker")
    assert entry["id"] == ask and entry["held"].startswith("review brief missing: ")
    for heading in BRIEF_HEADINGS:
        assert heading in entry["held"]
    assert waiting(trk, "--all") == [] and waiting(trk, "--me", "michael") == []  # hx routes what --all lists
    write_brief(trk, t)
    [released] = waiting(trk, "--all")
    assert released["id"] == ask and released["held"] is None
    assert [e["held"] for e in waiting(trk, "--me", "michael")] == [None]


def test_an_incomplete_brief_still_holds_and_names_only_what_is_missing(trk, red):
    t, _, ask = red
    write_brief(trk, t, BRIEF.replace("Text under Rollback.", ""))
    assert ids(waiting(trk, "--me", "michael")) == []
    [line] = trk("waiting", "--me", "worker").out.splitlines()
    assert "Rollback" in line and "Decision" not in line.split("held:")[1]


def test_show_says_held_while_held_and_not_after(trk, red):
    t, _, ask = red
    shown = trk("show", ask).out
    assert "held: review brief missing" in shown
    write_brief(trk, t)
    assert "held:" not in trk("show", ask).out


def test_a_complete_brief_releases_the_ask_and_its_clock_starts_then(trk, red, clock):
    t, _, ask = red
    clock(RELEASED)
    write_brief(trk, t)
    clock(LATER)
    [entry] = waiting(trk, "--me", "michael")
    assert entry["id"] == ask
    assert entry["urgency"]["minutes"] == 30  # LATER - RELEASED, not LATER - OPENED (150 minutes)
    assert "held:" not in trk("waiting", "--me", "michael").out
    assert "Nothing is waiting on you" not in ok(trk, "pages", "render", "needs-you:michael", "--out", "-")


def test_the_wait_starts_at_the_latest_brief_write_so_an_edit_restarts_it(trk, red, clock):
    t, _, ask = red
    clock(RELEASED)
    write_brief(trk, t)
    clock(LATER)
    write_brief(trk, t, BRIEF.replace("Text under Rollback.", "Revert the merge commit."))
    [entry] = waiting(trk, "--me", "michael")
    assert entry["urgency"]["minutes"] == 0  # the journal's latest review-brief write, as the spec says


def test_the_release_time_is_read_from_the_journal_not_the_question_text(trk, red, clock):
    t, _, ask = red
    clock(RELEASED)
    write_brief(trk, t)
    question = ok(trk, "section", "get", ask, "question")
    assert "Review brief, written by " in question and "independent auditor" not in question
    assert "complete since" not in question and RELEASED[:10] not in question
    clock(LATER)
    assert waiting(trk, "--me", "michael")[0]["urgency"]["minutes"] == 30


def test_a_brief_written_before_the_ask_opened_leaves_the_clock_at_the_opening(trk, tmp_path, clock):
    _, sha, t = red_ticket(trk, tmp_path)
    clock("2026-10-24T09:00:00+00:00")
    write_brief(trk, t)
    clock(OPENED)
    assert ok(trk, "blast-radius", t, "--as", "worker") == "red"
    clock("2026-10-24T10:20:00+00:00")
    [entry] = waiting(trk, "--me", "michael")
    assert entry["urgency"]["minutes"] == 20  # from the opening, not the earlier brief


def test_a_brief_removed_and_set_again_restarts_the_clock(trk, red, clock):
    t, _, ask = red
    clock(RELEASED)
    write_brief(trk, t)
    clock(LATER)
    ok(trk, "section", "rm", t, "review-brief")
    assert ids(waiting(trk, "--me", "michael")) == []
    clock("2026-10-24T13:00:00+00:00")
    write_brief(trk, t)
    clock("2026-10-24T13:10:00+00:00")
    assert waiting(trk, "--me", "michael")[0]["urgency"]["minutes"] == 10


def test_a_direct_approval_is_refused_no_brief_naming_the_headings(trk, red):
    t, sha, ask = red
    error = direct(trk, t, sha)
    assert error.code == 1
    error = error.error()
    assert error["rule"] == "no-brief" and error["item"] == t
    for heading in BRIEF_HEADINGS:
        assert heading in error["message"]
    assert item(trk, ask)["status"] == "open"


def test_a_direct_approval_names_only_the_missing_headings(trk, red):
    t, sha, _ = red
    write_brief(trk, t, BRIEF.replace("Text under Rollback.", ""))
    message = direct(trk, t, sha).error()["message"]
    assert "'Rollback'" in message and "'Decision'" not in message


def test_a_direct_approval_is_accepted_once_the_brief_is_complete(trk, red):
    t, sha, ask = red
    write_brief(trk, t)
    assert direct(trk, t, sha).code == 0
    assert item(trk, ask)["status"] == "closed"


def test_a_github_review_ref_is_left_alone(trk, red):
    t, sha, _ = red
    ok(trk, "review", "record", t, "--ref", "https://github.com/o/r/pull/1#pullrequestreview-1", "--sha", sha,
       "approved", "--note", "Read it.", "--as", "michael")


def plain_ask(trk, tmp_path, t, role="engineering"):
    question = tmp_path / "plain-question.md"
    question.write_text("Which colour?\n")
    return ok(trk, "ask", t, "--to", role, "Which colour?", "--question", f"@{question}", "--as", "worker")


def test_a_plain_ask_under_a_red_ticket_is_never_held(trk, tmp_path, red):
    t, _, held = red
    plain = plain_ask(trk, tmp_path, t)
    assert ids(waiting(trk, "--me", "michael")) == [plain]
    assert ids(waiting(trk, "--all")) == [plain]  # a held ask is left out of --all --json
    assert ids(waiting(trk, "--me", "worker")) == [held]  # its asker still sees it
    assert "held:" not in trk("show", plain).out


def test_a_yellow_ticket_and_its_ask_are_never_held(trk, tmp_path, clock):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["src/a.py"])
    t = lane_ticket(trk, path, sha)
    ok(trk, "actor", "add", "worker", "--kind", "agent")
    assert ok(trk, "blast-radius", t, "--as", "worker") == "yellow"
    plain = plain_ask(trk, tmp_path, t)
    assert ids(waiting(trk, "--me", "michael")) == [plain]
    assert "held:" not in trk("waiting", "--me", "michael").out


@pytest.mark.parametrize("path", ["skills/trk/SKILL.md", "skills/trk-drive-program/SKILL.md", "docs/agents.md"])
def test_the_guidance_quotes_the_rule_and_names_the_hold(path):
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / path).read_text()
    assert "something rated red needs to have enough context and information" in text
    assert "held" in text and "no-brief" in text
