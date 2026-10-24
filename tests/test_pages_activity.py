"""The cross-page comment activity log (Page publisher spec: Reading comments): when the backend
offers `activity`, the pull reads one activity log from `meta.comment_cursor` instead of
listing comments page by page. Against the in-process fake page backend; the real one is never called.
The disposition table itself runs in both modes in test_pages_pull.py."""

import json
import sqlite3

import pytest

from fake_backend import ACTIVITY, OWNER_ID, refuse
from test_pages_flush import db, flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_pull import ask, asked, body, confirm, dispositions, item, pulled, replies, slug  # noqa: F401
from test_pages_setup import fake, journal, meta, setup_argv, token_file, tree  # noqa: F401 (fixtures)
from trk import pull

pytestmark = pytest.mark.usefixtures("live")


def offer(fake):
    fake.activity = True
    fake.grants.add(ACTIVITY)


def set_cursor(trk_home, value):
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        with conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('comment_cursor', ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (value,))
    finally:
        conn.close()


def foreign(fake):
    """An artifact the token's owner owns that is no tracker page."""
    fake._publish({"title": "Notes", "body": "<p data-comment-id=\"waiting\">x</p>", "team": "tracker",
                            "tags": []})
    return max(fake.artifacts)


# ----- ac-1: one activity call from the cursor instead of a listing per page ---------------------------------


def test_with_the_activity_log_offered_a_pull_makes_one_activity_call_and_lists_no_page(trk, trk_home, asked, fake):
    offer(fake)
    assert pulled(trk)["activity"] == {"seeded": True, "events": 0}
    seeded = meta(trk_home, "comment_cursor")
    fake.calls.clear()
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    report = pulled(trk)
    assert report["activity"] == {"seeded": False, "events": 1}
    assert [e["disposition"] for e in report["comments"]] == ["reply"]
    assert [name for name, _args in fake.calls] == [ACTIVITY, "reply"]
    assert seeded == pull.START  # the log was empty: the next call reads from its oldest event
    assert fake.tool_calls(ACTIVITY) == [{"cursor": None, "limit": pull.ACTIVITY_LIMIT}]
    assert item(trk, asked["A"])["status"] == "open" and replies(fake, c)[0][1] == "assistant"


def test_without_the_activity_log_the_pull_lists_page_by_page(trk, trk_home, asked, fake):
    report = pulled(trk)
    assert report["activity"] is None and fake.tool_calls(ACTIVITY) == []
    assert fake.tool_calls("list_comments")
    assert meta(trk_home, "comment_cursor") is None


def test_the_activity_log_is_paged_while_has_more(trk, trk_home, asked, fake, monkeypatch):
    offer(fake)
    pulled(trk)
    monkeypatch.setattr(pull, "ACTIVITY_LIMIT", 2)
    fake.calls.clear()
    on_d = [fake.add_comment(slug(trk_home, asked["D"]), "header", f"note {n}", author_id="u-sam",
                             created_at="2026-10-11T08:00:00.000Z") for n in range(5)]  # before the ask: routed, each with a reply
    report = pulled(trk)
    assert len(fake.tool_calls(ACTIVITY)) == 3 and report["activity"]["events"] == 5
    assert set(dispositions(trk_home)) == set(on_d)
    fake.calls.clear()
    assert pulled(trk)["comments"] == [] and len(fake.tool_calls(ACTIVITY)) == 3  # the 5 "Sent to" replies it left
    fake.calls.clear()
    assert pulled(trk)["comments"] == [] and len(fake.tool_calls(ACTIVITY)) == 1


def test_an_activity_log_offered_but_not_granted_is_recorded_and_the_per_page_pull_still_runs(trk, trk_home, asked, fake):
    fake.activity = True  # offered, but the token lacks the grant
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    report = pulled(trk)
    assert dispositions(trk_home)[c] == ("reply", asked["A"])
    assert page_row(trk_home, asked["D"])["last_error"] == f"{ACTIVITY}: tool_not_allowed ({ACTIVITY})"
    assert {e["error"] for e in report["errors"]} == {f"{ACTIVITY}: tool_not_allowed ({ACTIVITY})"}
    assert meta(trk_home, "comment_cursor") is None
    fake.grants.add(ACTIVITY)
    pulled(trk)  # seeds, and an activity call that works clears the error
    assert page_row(trk_home, asked["D"])["last_error"] is None


def test_an_activity_log_failure_part_way_keeps_what_was_read_and_falls_back_to_the_pages(trk, trk_home, asked, fake,
                                                                                 monkeypatch):
    offer(fake)
    pulled(trk)
    seeded = meta(trk_home, "comment_cursor")
    monkeypatch.setattr(pull, "ACTIVITY_LIMIT", 1)
    first = fake.add_comment(slug(trk_home, asked["D"]), "header", "one", author_id="u-sam")
    second = fake.add_comment(slug(trk_home, asked["T"]), "header", "two", author_id="u-sam")
    real = fake._activity
    calls = []

    def failing_second_page(arguments):
        calls.append(arguments)
        if len(calls) >= 2:
            refuse("internal_error (try again)")
        return real(arguments)

    monkeypatch.setattr(fake, "_activity", failing_second_page)
    report = pulled(trk)
    assert first in dispositions(trk_home) and second not in dispositions(trk_home)
    assert meta(trk_home, "comment_cursor") not in (None, seeded)  # moved past the first page only
    assert fake.tool_calls("list_comments")  # the fallback read the pages showing an ask
    assert report["errors"] and page_row(trk_home, asked["D"])["last_error"].startswith(f"{ACTIVITY}: internal_error")
    monkeypatch.setattr(fake, "_activity", real)
    pulled(trk)
    assert second in dispositions(trk_home)
    assert page_row(trk_home, asked["D"])["last_error"] is None


# ----- ac-2: only people's root comments on `waiting` or an ask's box are candidates --------------------


def test_the_trackers_replies_and_status_events_never_become_answers(trk, trk_home, asked, fake):
    offer(fake)
    pulled(trk)
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    pulled(trk)
    fake.comments[c]["status"] = "resolved"  # the person resolves their own thread: the tracker never does
    fake._event("status", fake.comments[c], "2026-10-11T13:01:00.000Z", status="resolved")
    kinds = [e["type"] for e in fake.events]
    assert kinds[-3:] == ["comment", "reply", "status"]
    status = fake.events[-1]
    assert status["authorKind"] == "human" and status["handle"] == f"ask-{asked['A']}"  # the root's author
    before = dispositions(trk_home)
    report = pulled(trk)
    assert report["activity"]["events"] == 2 and report["comments"] == []
    assert dispositions(trk_home) == before and list(before) == [c]
    assert len(fake.tool_calls("reply")) == 1


def test_a_persons_reply_or_resolve_on_a_thread_never_read_is_no_new_comment(trk, trk_home, asked, fake):
    """A thread from before the seed, on a page the per-page read never lists: a person's reply in it and
    its resolve carry its `waiting` handle and a human author, and are still not root comments."""
    root = fake.add_comment(slug(trk_home, asked["T"]), "waiting", "Is this done?", author_id="u-kim")
    offer(fake)
    pulled(trk)
    fake.add_reply(root, "Metres.", author_id="u-kim")
    fake.comments[root]["status"] = "resolved"
    fake._event("status", fake.comments[root], "2026-10-11T13:01:00.000Z", status="resolved")
    report = pulled(trk)
    assert report["activity"]["events"] == 2 and report["comments"] == []
    assert dispositions(trk_home) == {} and fake.tool_calls("reply") == []


def test_a_reopened_ask_is_not_answered_by_the_old_threads_status_event(trk, trk_home, asked, fake, monkeypatch):
    """A resolve after a reopen carries the root (a person's comment on the ask's box) as its author."""
    offer(fake)
    pulled(trk)
    a = asked["A"]
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Metres.", author_id="u-kim")
    pulled(trk)
    confirm(trk, a, c)
    monkeypatch.setenv("TRK_NOW", "2026-10-11T13:00:00Z")
    assert trk("reopen", a, "--reason", "need units", "--as", "worker").code == 0
    fake.comments[c]["status"] = "open"
    fake._event("status", fake.comments[c], "2026-10-11T13:01:00.000Z", status="resolved")
    report = pulled(trk)
    assert report["comments"] == [] and item(trk, a)["status"] == "open"


def test_an_assistant_root_comment_is_no_answer(trk, trk_home, asked, fake):
    offer(fake)
    pulled(trk)
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author="Bot",
                         author_id=OWNER_ID, author_kind="assistant")
    pulled(trk)
    assert dispositions(trk_home)[c] == ("ignored", None)
    assert item(trk, asked["A"])["status"] == "open" and fake.comments[c]["replies"] == []


def test_comments_on_artifacts_that_are_no_tracker_page_are_passed_over(trk, trk_home, asked, fake):
    offer(fake)
    pulled(trk)
    other = foreign(fake)
    c = fake.add_comment(other, "waiting", "Yes", author_id="u-kim")
    report = pulled(trk)
    assert report["activity"]["events"] == 1 and report["comments"] == []
    assert c not in dispositions(trk_home) and fake.comments[c]["replies"] == []


# ----- ac-3: the cursor, the seed and invalid_cursor ------------------------------------------------------


def test_the_first_pull_drains_the_activity_log_to_seed_it_and_answers_no_history(trk, trk_home, asked, fake, monkeypatch):
    history = [fake.add_comment(foreign(fake), "waiting", "old", author_id="u-kim"),
               fake.add_comment(slug(trk_home, asked["T"]), "waiting", "Is this done?", author_id="u-kim",
                                created_at="2026-10-11T08:00:00.000Z")]
    history += [fake.add_comment(slug(trk_home, asked["T"]), "header", f"old {n}", author_id="u-sam")
                for n in range(3)]
    offer(fake)
    monkeypatch.setattr(pull, "ACTIVITY_LIMIT", 2)
    report = pulled(trk)
    assert report["activity"] == {"seeded": True, "events": 5}
    assert [call.get("cursor") for call in fake.tool_calls(ACTIVITY)][0] is None and len(fake.tool_calls(ACTIVITY)) == 3
    assert not set(history) & set(dispositions(trk_home))
    assert all(fake.comments[c]["replies"] == [] for c in history)
    cursor = meta(trk_home, "comment_cursor")
    assert isinstance(cursor, str) and cursor
    fake.calls.clear()
    assert pulled(trk)["activity"] == {"seeded": False, "events": 0}
    assert fake.tool_calls(ACTIVITY) == [{"cursor": cursor, "limit": 2}]


def test_the_cursor_is_stored_as_the_service_gave_it(trk, trk_home, asked, fake):
    offer(fake)
    fake.add_comment(slug(trk_home, asked["T"]), "header", "x", author_id="u-sam")
    pulled(trk)
    given = fake._activity({})["cursor"]
    assert meta(trk_home, "comment_cursor") == given


def test_the_seeding_pull_still_reads_the_pages_so_no_answer_written_before_it_is_lost(trk, trk_home, asked, fake):
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    offer(fake)  # the service gained the activity log after the comment, before any pull read it
    report = pulled(trk)
    assert report["activity"]["seeded"] is True
    assert dispositions(trk_home)[c] == ("reply", asked["A"])
    assert len(replies(fake, c)) == 1


def test_an_invalid_cursor_is_recorded_and_re_seeds_rather_than_replaying(trk, trk_home, asked, fake):
    offer(fake)
    old = fake.add_comment(slug(trk_home, asked["T"]), "waiting", "Is this done?", author_id="u-kim")
    set_cursor(trk_home, "not-a-cursor")
    report = pulled(trk)
    assert report["activity"]["seeded"] is True
    assert old not in dispositions(trk_home) and fake.comments[old]["replies"] == []
    assert page_row(trk_home, asked["D"])["last_error"].startswith(f"{ACTIVITY}: invalid_cursor")
    assert meta(trk_home, "comment_cursor") != "not-a-cursor"
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    report = pulled(trk)
    assert report["activity"] == {"seeded": False, "events": 1}
    assert dispositions(trk_home)[c] == ("reply", asked["A"])
    assert page_row(trk_home, asked["D"])["last_error"] is None


# ----- ac-4: replies go to the event's root comment -----------------------------------------------------


def test_replies_use_the_events_root_comment_id(trk, trk_home, asked, fake):
    offer(fake)
    pulled(trk)
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    (event,) = [e for e in fake.events if e["type"] == "comment" and e["rootCommentId"] == c]
    pulled(trk)
    (sent,) = fake.tool_calls("reply")
    assert sent["comment_id"] == event["rootCommentId"] and sent["slug"] == event["slug"]


def test_a_seed_of_an_empty_log_sticks_though_the_service_gives_no_cursor(trk, trk_home, asked, fake):
    offer(fake)
    assert pulled(trk)["activity"]["seeded"] is True
    assert meta(trk_home, "comment_cursor") == pull.START  # an empty log answered with a null cursor
    c = fake.add_comment(slug(trk_home, asked["T"]), "waiting", "Is this done?", author_id="u-kim")
    report = pulled(trk)
    assert report["activity"] == {"seeded": False, "events": 1}
    assert dispositions(trk_home)[c] == ("which-ask", None)  # read as new, not drained as history
    assert meta(trk_home, "comment_cursor") not in (None, pull.START)


# ----- reading only what the per-page read would ------------------------------------------------------


def test_an_offers_activity_failure_is_recorded_and_the_pass_reads_page_by_page(trk, trk_home, asked, fake, monkeypatch):
    offer(fake)
    fake.fail_next("500", tool="offers_activity")
    report = pulled(trk)
    assert report["activity"] is None and fake.tool_calls("list_comments")
    assert page_row(trk_home, asked["D"])["last_error"].startswith("offers_activity: HTTP 500")
    pulled(trk)
    assert page_row(trk_home, asked["D"])["last_error"] is None


def set_page(trk_home, item_id, **fields):
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        with conn:
            for key, value in fields.items():
                conn.execute(f"UPDATE pages SET {key} = ? WHERE item_id = ?", (value, item_id))
    finally:
        conn.close()


@pytest.mark.parametrize("slug_known", [True, False])
def test_a_comment_on_a_page_whose_publish_is_in_doubt_is_read_once_it_is_adopted(trk, trk_home, asked, fake,
                                                                                  slug_known):
    """Audit of ledger-13: the page's first publish was ambiguous, so the tracker may not know its slug yet.
    The cursor stays before the comment until a later flush adopts the page, and nothing is lost."""
    offer(fake)
    pulled(trk)
    t_slug = slug(trk_home, asked["T"])
    set_page(trk_home, asked["T"], state="publishing", slug=t_slug if slug_known else None)
    before = meta(trk_home, "comment_cursor")
    on_t = fake.add_comment(t_slug, "waiting", "Is this done?", author_id="u-kim")
    on_d = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    pulled(trk)
    assert on_t not in dispositions(trk_home) and fake.comments[on_t]["replies"] == []
    assert dispositions(trk_home)[on_d] == ("reply", asked["A"])  # a known page's comment is not held up
    assert meta(trk_home, "comment_cursor") == before
    set_page(trk_home, asked["T"], state="live", slug=t_slug)  # a later flush adopted the page
    report = pulled(trk)
    assert [(e["comment"], e["disposition"]) for e in report["comments"]] == [(on_t, "which-ask")]
    assert meta(trk_home, "comment_cursor") != before
    assert len(fake.tool_calls("reply")) == 2  # the answer's reply was not sent twice


def test_with_no_publish_in_doubt_comments_elsewhere_do_not_hold_the_cursor(trk, trk_home, asked, fake):
    offer(fake)
    pulled(trk)
    before = meta(trk_home, "comment_cursor")
    fake.add_comment(foreign(fake), "waiting", "Yes", author_id="u-kim")
    pulled(trk)
    assert meta(trk_home, "comment_cursor") != before
