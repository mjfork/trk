"""Resolving the threads of closed asks (Page publisher spec Pub 17, amended 2026-10-19 by tr-hr642): the
pull's closing step and `trk pages tidy`, against the in-process fake artifact service."""

import sqlite3

import pytest

from fake_artifacts import ACTIVITY
from test_pages_flush import flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_pull import (TABLE, asked, ask, body, dispositions, item, mode, outbox,  # noqa: F401
                             pulled, replies, slug)
from test_pages_setup import fake, journal, meta, setup_argv, token_file, tree  # noqa: F401 (fixtures)
from trk import pull

CLOSED = "Closed: Metres. Recorded on Which unit? ({a})"


def say(fake, trk_home, asked, text="Metres or feet?", on="D", handle=None, **kw):
    return fake.add_comment(slug(trk_home, asked[on]), handle or f"ask-{asked['A']}", text, **kw)


def answer(trk, body, ask_id, actor="kim"):
    assert trk("answer", ask_id, "--outcome", "Metres.", "--answer", body("Metres.\n"), "--as", actor).code == 0


def resolves(fake):
    """The threads a closing reply asked to resolve (the token has no `resolve_artifact_comment`)."""
    assert not fake.tool_calls("resolve_artifact_comment")
    return [c["commentId"] for c in fake.tool_calls("reply_to_artifact_comment") if c["resolve"]]


def forget_since(trk_home, monkeypatch):
    """A store from before this step ran: no `closings_from` stamp yet, and the clock moves on, so the next
    pass stamps a time after every close so far (the tests' clock is frozen)."""
    monkeypatch.setenv("TRK_NOW", "2026-10-11T10:00:00Z")
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        conn.execute("DELETE FROM meta WHERE key = 'closings_from'")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def started(trk, trk_home, asked, mode):
    """The first pull that runs the step has happened (it stamps `closings_from`), before any ask closes."""
    pulled(trk)
    assert meta(trk_home, "closings_from")
    return asked


# ----- ac-1: the pull closes the threads on a box that has left the page ---------------------------


@TABLE
def test_ac1_the_pull_replies_and_resolves_the_threads_once_the_box_has_left(trk, trk_home, body, started, fake):
    a = started["A"]
    c1, c2 = say(fake, trk_home, started), say(fake, trk_home, started, "Feet, surely", on="J", author_id="u-lee",
                                              author="Lee Park")
    outside = say(fake, trk_home, started, "A note on the page", handle="waiting-note")
    pulled(trk)
    answer(trk, body, a)
    pulled(trk)
    assert resolves(fake) == [] and fake.comments[c1]["status"] == "open"  # the box is still on the page

    flush(trk)  # D, J and P drop the box; a page sent earlier in the flush may be closed by a later pull-before-drop
    pulled(trk)
    closing = CLOSED.format(a=a)
    for comment in (c1, c2):
        assert replies(fake, comment)[-1] == (closing, "assistant")
        assert fake.comments[comment]["status"] == "resolved"
    assert fake.comments[outside]["status"] != "resolved"
    assert not [m for m, _ in replies(fake, outside) if m.startswith("Closed")]
    assert sorted(resolves(fake)) == sorted([c1, c2])

    calls = len(fake.calls)
    pulled(trk)
    assert [n for n, _ in fake.calls[calls:] if n == "reply_to_artifact_comment"] == []
    assert len([r for r in outbox(trk_home) if r["body"] == closing]) == 2
    assert all(call["resolve"] == (call["body"] == closing) for call in fake.tool_calls("reply_to_artifact_comment"))


@TABLE
def test_threads_on_open_asks_are_never_closed(trk, trk_home, ask, body, started, fake, live):
    b = ask(live["J"], "Which weight?")
    flush(trk)
    on_open = fake.add_comment(slug(trk_home, live["J"]), f"ask-{b}", "Kilos?")
    on_closed = say(fake, trk_home, started)
    pulled(trk)
    answer(trk, body, started["A"])
    flush(trk)
    pulled(trk)
    assert fake.comments[on_closed]["status"] == "resolved"
    assert fake.comments[on_open]["status"] != "resolved" and resolves(fake) == [on_closed]
    assert all(CLOSED.split(".")[0] not in m for m, _ in replies(fake, on_open))


def test_a_withdrawn_ask_closes_its_threads_too(trk, trk_home, started, fake):
    c = say(fake, trk_home, started)
    pulled(trk)
    assert trk("withdraw", started["A"], "--reason", "no longer needed", "--as", "worker").code == 0
    flush(trk)
    pulled(trk)
    assert replies(fake, c)[-1][0] == f"Closed: withdrawn: no longer needed. Recorded on Which unit? ({started['A']})"
    assert fake.comments[c]["status"] == "resolved"


# ----- crash safety: a reply is never sent twice, a resolve is retried until done ------------------


def closed(trk, trk_home, body, started, fake):
    """A comment on the box and the ask answered; the pages are not sent yet. The flush that follows closes
    the first page's thread from its next page's pull-before-drop pass, so a fault set before it bites there."""
    c = say(fake, trk_home, started)
    pulled(trk)
    answer(trk, body, started["A"])
    pulled(trk)  # sends the "Ask closed" note on the thread, so a fault set next bites the closing reply alone
    return c


def closing_replies(fake, comment, started):
    return [m for m, _ in replies(fake, comment) if m == CLOSED.format(a=started["A"])]


def marker(trk_home, comment):
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        return pull.closings(conn).get(comment)
    finally:
        conn.close()


def test_the_closing_reply_resolves_the_thread_in_the_same_call_and_never_calls_resolve(trk, trk_home, body, started,
                                                                                         fake):
    c = closed(trk, trk_home, body, started, fake)
    flush(trk)
    assert fake.comments[c]["status"] == "resolved" and resolves(fake) == [c]
    assert marker(trk_home, c)["state"] == "resolved" and "resolve_artifact_comment" not in dict(fake.calls)


def test_an_ambiguous_closing_reply_is_not_sent_twice_and_resolves_the_thread(trk, trk_home, body, started, fake):
    c = closed(trk, trk_home, body, started, fake)
    fake.fail_next("502-after", tool="reply_to_artifact_comment")  # posted (and resolved), then the edge answered 502
    flush(trk)
    pulled(trk)
    pulled(trk)
    assert len(closing_replies(fake, c, started)) == 1 and fake.comments[c]["status"] == "resolved"
    assert marker(trk_home, c)["state"] == "resolved"


def test_an_ambiguous_closing_reply_that_left_the_thread_open_is_given_up_and_reported(trk, trk_home, body, started,
                                                                                         fake):
    c = closed(trk, trk_home, body, started, fake)
    fake.fail_next("502-after", tool="reply_to_artifact_comment")
    flush(trk)
    fake.comments[c]["status"] = "open"  # the lost call posted the reply but the thread is not resolved
    errors = [e["error"] for e in pulled(trk)["errors"]]
    assert any("not resolved" in e for e in errors) and marker(trk_home, c)["state"] == "gone"
    assert len(closing_replies(fake, c, started)) == 1
    assert pulled(trk)["errors"] == [] and len(closing_replies(fake, c, started)) == 1


def test_a_closing_reply_the_service_does_not_resolve_is_final(trk, trk_home, body, started, fake, monkeypatch):
    c = closed(trk, trk_home, body, started, fake)
    real = fake._reply_to_artifact_comment

    def refuse_to_resolve(a):  # the token's owner may not resolve here: the reply lands, resolved false plus a note
        fake.give_away(fake.comments[a["commentId"]]["slug"])
        return real(a)

    monkeypatch.setattr(fake, "_reply_to_artifact_comment", refuse_to_resolve)
    flush(trk)
    errors = [e["error"] for e in pulled(trk)["errors"]]
    assert fake.comments[c]["status"] != "resolved" and marker(trk_home, c)["state"] == "gone"
    assert len(closing_replies(fake, c, started)) == 1
    pulled(trk)
    assert len(closing_replies(fake, c, started)) == 1 and len(resolves(fake)) == 1


def test_a_thread_whose_closing_reply_was_refused_is_not_resolved_until_it_is_resent(trk, trk_home, body, started, fake):
    c = closed(trk, trk_home, body, started, fake)
    fake.grants.discard("reply_to_artifact_comment")
    flush(trk)
    pulled(trk)
    closing = [r for r in outbox(trk_home) if r["body"] == CLOSED.format(a=started["A"])]
    assert [r["state"] for r in closing] == ["failed"] and resolves(fake) == []
    assert fake.comments[c]["status"] != "resolved"
    fake.grants.add("reply_to_artifact_comment")
    assert trk("pages", "reply", "resend", str(closing[0]["id"])).code == 0
    pulled(trk)
    assert fake.comments[c]["status"] == "resolved" and len(closing_replies(fake, c, started)) == 1


def test_a_closing_reply_that_cannot_be_sent_does_not_hold_another_page_from_dropping_the_box(
        trk, trk_home, body, started, fake):
    closed(trk, trk_home, body, started, fake)
    fake.fail_next("502-after", tool="reply_to_artifact_comment", times=50)
    report = flush(trk)
    for key in ("D", "J", "P"):
        assert f'data-comment-id="ask-{started["A"]}"' not in fake.body(slug(trk_home, started[key]))
    assert not [e for e in report.get("errors", []) if "pull-before-drop" in str(e)]


def test_a_thread_the_service_cannot_find_is_given_up_not_retried_forever(trk, trk_home, body, started, fake, monkeypatch):
    from fake_artifacts import _error

    c = closed(trk, trk_home, body, started, fake)
    real = fake._reply_to_artifact_comment
    monkeypatch.setattr(fake, "_reply_to_artifact_comment",
                        lambda a: _error("not_found") if a["body"].startswith("Closed: ") else real(a))
    flush(trk)
    pulled(trk)
    assert marker(trk_home, c)["state"] == "gone"
    assert len([call for call in fake.tool_calls("reply_to_artifact_comment") if call["body"].startswith("Closed: ")]) == 1


def test_a_pending_closing_reply_is_dropped_when_the_ask_is_reopened(trk, trk_home, body, started, fake):
    c = closed(trk, trk_home, body, started, fake)
    fake.fail_next("502", tool="reply_to_artifact_comment", times=50)  # the closing reply is queued, never sent
    flush(trk)
    assert [r["state"] for r in outbox(trk_home) if r["body"] == CLOSED.format(a=started["A"])] == ["pending"]
    assert marker(trk_home, c)["state"] == "queued"
    assert trk("reopen", started["A"], "--reason", "need units", "--as", "worker").code == 0
    fake.faults.clear()
    pulled(trk)
    assert closing_replies(fake, c, started) == [] and fake.comments[c]["status"] != "resolved"
    assert marker(trk_home, c) is None
    assert [r for r in outbox(trk_home) if r["body"] == CLOSED.format(a=started["A"])] == []
    assert resolves(fake) == []


# ----- history is tidy's, not the pull's -------------------------------------------------------------


def test_asks_closed_before_the_step_first_ran_are_left_to_tidy(trk, trk_home, body, asked, fake, monkeypatch):
    c = say(fake, trk_home, asked)
    pulled(trk)
    answer(trk, body, asked["A"])
    forget_since(trk_home, monkeypatch)  # this store's first pass of the new build runs after the close
    flush(trk)
    pulled(trk)
    assert resolves(fake) == [] and fake.comments[c]["status"] == "outdated"
    assert meta(trk_home, "closings_from")


# ----- ac-2: trk pages tidy ---------------------------------------------------------------------------


def tidy(trk, key, *extra):
    result = trk("pages", "tidy", key, "--json", *extra)
    assert result.code == 0, result.err
    return result.json()


def test_ac2_tidy_resolves_the_threads_of_closed_asks_on_a_needs_you_page_only(trk, trk_home, ask, body, asked, fake,
                                                                                live, monkeypatch):
    b = ask(live["D"], "Which weight?")
    flush(trk)
    page = "needs-you:kim"
    kim = page_row(trk_home, page)["slug"]
    on_closed = fake.add_comment(kim, f"ask-{asked['A']}", "On kim's page")
    on_open = fake.add_comment(kim, f"ask-{b}", "Kilos, on kim's page")
    on_note = fake.add_comment(kim, "needs-you", "a note")
    on_waiting = fake.add_comment(kim, "waiting", "which?")
    pulled(trk)
    answer(trk, body, asked["A"])
    forget_since(trk_home, monkeypatch)  # an ask the pull's own step does not own
    before = tidy(trk, page, "--dry-run")
    assert before["closing"] == [] and (on_closed, "box-on-page") in [(s["comment"], s["reason"]) for s in before["skipped"]]
    flush(trk)
    pulled(trk)
    assert resolves(fake) == [] and fake.comments[on_closed]["status"] == "outdated"

    heard = replies(fake, on_closed)  # "Reply received", and the note that the ask closed
    dry = tidy(trk, page, "--dry-run")
    assert dry["closing"] == [on_closed] and dry["dry_run"] is True
    assert resolves(fake) == [] and replies(fake, on_closed) == heard
    assert (on_open, "ask-open") in [(s["comment"], s["reason"]) for s in dry["skipped"]]

    report = tidy(trk, page)
    assert report["closing"] == [on_closed] and report["resolved"] == [on_closed] and report["page"] == page
    assert replies(fake, on_closed) == [*heard, (CLOSED.format(a=asked["A"]), "assistant")]
    assert fake.comments[on_closed]["status"] == "resolved"
    for untouched in (on_open, on_note, on_waiting):
        assert fake.comments[untouched]["status"] != "resolved"
        assert not [m for m, _ in replies(fake, untouched) if m.startswith("Closed")]
    assert resolves(fake) == [on_closed]

    again = tidy(trk, page)
    assert again["closing"] == [] and resolves(fake) == [on_closed] and len(replies(fake, on_closed)) == len(heard) + 1
    assert (on_closed, "already-closing") in [(s["comment"], s["reason"]) for s in again["skipped"]]


def test_tidy_skips_what_a_person_resolved_and_what_is_already_closing(trk, trk_home, body, asked, fake, monkeypatch):
    kim = page_row(trk_home, "needs-you:kim")["slug"]
    old = fake.add_comment(kim, f"ask-{asked['A']}", "Metres?")
    person_resolved = fake.add_comment(kim, f"ask-{asked['A']}", "Thanks")
    fake.comments[person_resolved]["status"] = "resolved"
    answer(trk, body, asked["A"])
    forget_since(trk_home, monkeypatch)
    flush(trk)
    report = tidy(trk, "needs-you:kim")
    assert report["closing"] == [old] and report["resolved"] == [old]
    assert [(s["comment"], s["reason"]) for s in report["skipped"]] == [(person_resolved, "resolved")]


def test_tidy_refuses_a_page_nobody_has(trk, trk_home, started):
    assert trk("pages", "tidy", "needs-you:nobody-here", "--json").code == 3


def test_the_closing_step_also_runs_in_the_flushers_pull_before_drop_and_closes_once(
        trk, trk_home, body, started, fake):
    c = say(fake, trk_home, started)
    pulled(trk)
    answer(trk, body, started["A"])
    flush(trk)  # pull-before-drop runs before each page's send; D's box is still shown when its own pass runs
    pulled(trk)
    assert replies(fake, c)[-1][0] == CLOSED.format(a=started["A"]) and fake.comments[c]["status"] == "resolved"
    assert [m for m, _ in replies(fake, c)].count(CLOSED.format(a=started["A"])) == 1


def test_tidy_reports_a_thread_with_an_earlier_reply_pending_as_skipped_in_dry_run_and_for_real(
        trk, trk_home, body, asked, fake, monkeypatch):
    kim = page_row(trk_home, "needs-you:kim")["slug"]
    c = fake.add_comment(kim, f"ask-{asked['A']}", "Metres?")
    pulled(trk)
    answer(trk, body, asked["A"])
    forget_since(trk_home, monkeypatch)
    flush(trk)
    conn = sqlite3.connect(trk_home / "store.db")
    with conn:
        conn.execute("INSERT INTO page_replies (slug, comment_id, body, resolve, state, created_at) "
                     "VALUES (?, ?, 'earlier', 0, 'pending', '2026-10-11T10:00:00.000Z')", (kim, c))
    conn.close()
    for extra in (("--dry-run",), ()):
        report = tidy(trk, "needs-you:kim", *extra)
        assert report["closing"] == [] and (c, "reply-pending") in [(s["comment"], s["reason"]) for s in report["skipped"]]


def test_tidy_closes_a_thread_no_pull_has_read(trk, trk_home, body, asked, fake, monkeypatch):
    kim = page_row(trk_home, "needs-you:kim")["slug"]
    answer(trk, body, asked["A"])
    forget_since(trk_home, monkeypatch)
    flush(trk)
    c = fake.add_comment(kim, f"ask-{asked['A']}", "Written after the last pull")
    assert dispositions(trk_home).get(c) is None  # no pull has disposed of it, so there is no page_comments row
    report = tidy(trk, "needs-you:kim")
    assert report["closing"] == [c] and report["resolved"] == [c] and report["replies"]["sent"] == [c]
    assert replies(fake, c)[-1] == (CLOSED.format(a=asked["A"]), "assistant")
    assert fake.comments[c]["status"] == "resolved" and resolves(fake) == [c]
    assert marker(trk_home, c)["state"] == "resolved" and marker(trk_home, c)["handle"] == f"ask-{asked['A']}"
