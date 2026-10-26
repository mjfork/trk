"""Reopen and withdrawal on the page (Page publisher spec: Amendments to Asks and owners, On the page
afterwards, Pull-before-drop; AC 8 and AC 9), against the in-process fake page backend. The real
service is never called."""

import json

import pytest

import fake_backend
from trk import pages
from trk.top import KEY as TOP
from test_pages_flush import db, flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_pull import (RECEIVED, TABLE, actor_row, ask, asked, body, confirm, dispositions, item,  # noqa: F401
                             mode, outbox, pulled, replies, slug)
from test_pages_setup import fake, journal, setup_argv, token_file, tree  # noqa: F401 (fixtures)


def tick(trk):
    result = trk("pages", "tick", "--json")
    assert result.code == 0, result.err
    return result.json()


def shows(fake, trk_home, item_id, handle):
    return f'data-comment-id="{handle}"' in fake.body(slug(trk_home, item_id))


def heard(fake, comment_id):
    """The replies on a thread, without its closing reply (Pub 17), which the other tests don't count."""
    return [(m, k) for m, k in replies(fake, comment_id) if not m.startswith("Closed: ")]


@pytest.fixture
def answered(trk, trk_home, asked, fake):
    """AC 6's end: Kim's comment on P's page was confirmed as the answer, and the box left D, J and P."""
    a = asked["A"]
    kim = fake.add_comment(slug(trk_home, asked["P"]), f"ask-{a}", "Metres. SI everywhere.", author_id="u-kim")
    tick(trk)
    confirm(trk, a, kim)
    tick(trk)
    flush(trk)
    assert item(trk, a)["status"] == "closed" and fake.comments[kim]["status"] == "resolved"  # the box left: closed, replied and resolved
    fake.calls.clear()
    return {**asked, "kim": kim}


# ----- AC 8: the reopen, its reply on the answer's thread, the box coming back ----------------------


@TABLE
def test_ac8_reopen_journals_returns_the_box_and_posts_the_reason_on_the_answers_thread(trk, trk_home, answered,
                                                                                        fake, ask):
    a, kim = answered["A"], answered["kim"]
    since = journal(trk_home)[-1]["seq"]
    result = trk("reopen", a, "--reason", "need units", "--as", "worker")
    assert result.code == 0, result.err

    (row,) = [r for r in journal(trk_home, since) if r["item"] == a]
    assert row["change"] == "ask.reopened" and row["actor"] == "worker"
    assert (row["status_old"], row["status_new"]) == ("closed", "open")
    assert json.loads(row["data"])["reason"] == "need units"
    assert f"open-ask:{a}" in item(trk, answered["D"])["why_not"]
    (queued,) = outbox(trk_home)[-1:]
    assert (queued["slug"], queued["comment_id"], queued["body"], queued["resolve"], queued["state"]) == \
        (slug(trk_home, answered["P"]), kim, "Reopened by worker: need units", 0, "pending")

    report = tick(trk)
    assert report["pull"]["replies"]["sent"] == [kim]
    assert replies(fake, kim)[-1] == ("Reopened by worker: need units", "assistant")
    assert fake.tool_calls("reply")[-1]["resolve"] is False  # not a closing reply
    assert fake.comments[kim]["status"] == "resolved"  # closed when the box left; the service has no un-resolve
    flush(trk)
    for key in ("D", "J", "P"):
        assert shows(fake, trk_home, answered[key], f"ask-{a}")


def test_ac8_a_withdrawn_ask_stays_final(trk, trk_home, live, fake, ask):
    a3 = ask(live["T"])
    assert trk("withdraw", a3, "--reason", "not needed", "--as", "worker").code == 0
    result = trk("reopen", a3, "--reason", "need units", "--as", "worker", "--json")
    assert result.code == 1 and result.error()["rule"] == "ask-final"


def test_a_reopen_after_a_cli_answer_queues_no_reply(trk, trk_home, asked, fake, body):
    a = asked["A"]
    assert trk("answer", a, "--outcome", "Metres", "--answer", body("Metres.\n"), "--as", "kim").code == 0
    assert trk("reopen", a, "--reason", "need units", "--as", "worker").code == 0
    assert outbox(trk_home) == []


def test_the_reply_names_the_reopener_by_display_name(trk, trk_home, answered, fake):
    assert trk("actor", "edit", "worker", "--display", "Build Bot").code == 0
    assert trk("reopen", answered["A"], "--reason", "need units", "--as", "worker").code == 0
    assert outbox(trk_home)[-1]["body"] == "Reopened by Build Bot: need units"


def test_a_refused_reopen_queues_nothing(trk, trk_home, answered, fake):
    result = trk("reopen", answered["A"], "--reason", "need units", "--as", "lee", "--json")
    assert result.code == 1
    assert [r["body"] for r in outbox(trk_home) if not r["body"].startswith("Closed: ")] == \
        [RECEIVED, "Recorded as the answer to Which unit?, by kim (for kim). Ask closed."]


# ----- ac-2: a fresh root comment on the returned box is the next answer -----------------------------


@TABLE
def test_a_new_comment_on_the_returned_box_is_the_next_reply_and_the_old_thread_is_not(trk, trk_home, answered,
                                                                                       fake):
    a, kim = answered["A"], answered["kim"]
    assert trk("reopen", a, "--reason", "need units", "--as", "worker").code == 0
    flush(trk)
    fresh = fake.add_comment(slug(trk_home, answered["D"]), f"ask-{a}", "Millimetres, to the nearest one.",
                             author_id="u-kim")
    report = tick(trk)
    assert [(e["comment"], e["disposition"]) for e in report["pull"]["comments"]] == [(fresh, "reply")]
    assert [r["comment"] for r in item(trk, a)["replies"]] == [fresh]  # replies before the reopen are not shown
    confirm(trk, a, fresh, outcome="Millimetres, to the nearest one.")
    (answer,) = [r for r in journal(trk_home) if r["change"] == "ask.answered"][-1:]
    assert json.loads(answer["data"])["comment_id"] == fresh
    assert item(trk, a)["outcome"] == "Millimetres, to the nearest one."
    assert dispositions(trk_home)[kim] == ("reply", a)  # recorded once, the first time
    assert [message for message, _kind in replies(fake, kim)] == [
        RECEIVED, "Recorded as the answer to Which unit?, by kim (for kim). Ask closed.",
        f"Closed: Metres. Recorded on Which unit? ({a})", "Reopened by worker: need units"]
    assert fake.comments[fresh]["status"] == "open"


# ----- AC 9 and pull-before-drop: a comment written before the drop still gets its reply -------------


@pytest.fixture
def withdrawn(trk, trk_home, answered, fake):
    """AC 9: Kim comments on the reopened ask's box on D's page, then the ask is withdrawn and D closes wont-do before any tick."""
    a = answered["A"]
    assert trk("reopen", a, "--reason", "need units", "--as", "worker").code == 0
    tick(trk)
    flush(trk)
    late = fake.add_comment(slug(trk_home, answered["D"]), f"ask-{a}", "Feet, after all.", author_id="u-kim")
    assert trk("withdraw", a, "--reason", "Dropped the question", "--as", "worker").code == 0
    result = trk("close", answered["D"], "--resolution", "wont-do", "--outcome", "Dropped the question")
    assert result.code == 0, result.err
    assert item(trk, a)["resolution"] != "done"
    fake.calls.clear()
    after = []  # per call in fake.calls: the body of its page once the call was done
    tool = fake.call

    def recording(name, arguments, **kwargs):
        result = tool(name, arguments, **kwargs)
        if len(after) < len(fake.calls):
            after.append(fake.body(arguments["slug"]) if arguments.get("slug") in fake.artifacts else "")
        return result

    fake.call = recording
    return {**answered, "late": late, "after": after}


def _drops(fake, trk_home, withdrawn):
    """The index in fake.calls of each write that left `ask-<A>` off D's, J's or P's page."""
    handle = f'data-comment-id="ask-{withdrawn["A"]}"'
    slugs = {slug(trk_home, withdrawn[key]) for key in ("D", "J", "P")}
    return [index for index, (tool, args), body in zip(range(len(fake.calls)), fake.calls, withdrawn["after"])
            if tool == "update" and args["slug"] in slugs and handle not in body]


@TABLE
def test_ac9_the_next_tick_replies_withdrawn_before_any_page_drops_the_box(trk, trk_home, withdrawn,
                                                                                       fake):
    late = withdrawn["late"]
    tick(trk)  # within the quiet window: the pull replies, the flush sends nothing yet
    assert _drops(fake, trk_home, withdrawn) == []
    flush(trk)
    assert dispositions(trk_home)[late] == ("withdrawn", withdrawn["A"])
    assert heard(fake, late) == [("Withdrawn: Dropped the question", "assistant")]
    assert fake.comments[late]["status"] in ("outdated", "resolved")  # left open, then outdated as the box left, or closed with it
    reply_at = next(i for i, (tool, _a) in enumerate(fake.calls) if tool == "reply")
    drops = _drops(fake, trk_home, withdrawn)
    assert drops and reply_at < min(drops)


@TABLE
def test_a_flush_that_drops_an_ask_handle_pulls_first(trk, trk_home, withdrawn, fake):
    late = withdrawn["late"]
    report = flush(trk)  # no tick: the flusher alone
    assert withdrawn["D"] in report["updated"]
    assert heard(fake, late) == [("Withdrawn: Dropped the question", "assistant")]
    assert fake.comments[late]["status"] in ("outdated", "resolved")  # left open, then outdated as the box left, or closed with it
    reply_at = next(i for i, (tool, _a) in enumerate(fake.calls) if tool == "reply")
    drops = _drops(fake, trk_home, withdrawn)
    assert len(drops) == 3 and reply_at < min(drops)
    for key in ("D", "J", "P"):
        assert not shows(fake, trk_home, withdrawn[key], f"ask-{withdrawn['A']}")


def test_a_flush_that_drops_nothing_does_not_pull(trk, trk_home, asked, fake, body):
    assert trk("section", "set", asked["T"], "what-to-build", "--file", body("Changed.\n")[1:]).code == 0
    flush(trk)
    assert fake.tool_calls("list_comments") == []


def test_when_the_dropping_pages_listing_fails_the_page_waits(trk, trk_home, withdrawn, fake):
    d_slug = slug(trk_home, withdrawn["D"])
    original = fake._list_comments

    def failing_for_d(arguments):
        if arguments["slug"] == d_slug:
            fake_backend.refuse("internal_error (listing down)")
        return original(arguments)

    fake._list_comments = failing_for_d
    report = flush(trk)
    assert withdrawn["D"] in report["failed"]
    assert "pull-before-drop" in page_row(trk_home, withdrawn["D"])["last_error"]
    assert shows(fake, trk_home, withdrawn["D"], f"ask-{withdrawn['A']}")
    fake._list_comments = original
    report = flush(trk)
    assert withdrawn["D"] in report["updated"]
    assert heard(fake, withdrawn["late"]) == [("Withdrawn: Dropped the question", "assistant")]
    assert not shows(fake, trk_home, withdrawn["D"], f"ask-{withdrawn['A']}")


@TABLE
def test_a_comment_written_after_the_last_listing_but_before_the_drop_is_read_once_more(trk, trk_home, withdrawn,
                                                                                     fake):
    d_slug, handle = slug(trk_home, withdrawn["D"]), f"ask-{withdrawn['A']}"
    kim_slug = slug(trk_home, "needs-you:kim")  # the last page to drop the box, after D, J and P
    update, sneaked = fake._update, []

    def comment_first(arguments):
        if arguments["slug"] == kim_slug and not sneaked:
            sneaked.append(fake.add_comment(kim_slug, handle, "Inches!", author_id="u-kim"))
        return update(arguments)

    fake._update = comment_first
    flush(trk)
    (late,) = sneaked
    assert fake.comments[late]["status"] == "outdated" and late not in dispositions(trk_home)
    report = pulled(trk)
    assert [(e["comment"], e["disposition"]) for e in report["comments"]] == [(late, "withdrawn")]
    assert heard(fake, late) == [("Withdrawn: Dropped the question", "assistant")]
    fake.calls.clear()
    pulled(trk)
    assert not {d_slug, kim_slug} & {args["slug"] for args in fake.tool_calls("list_comments")}


@TABLE
def test_a_reply_that_fails_holds_every_drop_of_the_box_until_it_is_sent(trk, trk_home, withdrawn, fake):
    late, handle = withdrawn["late"], f"ask-{withdrawn['A']}"
    dropping = [withdrawn[k] for k in ("D", "J", "P")] + ["needs-you:kim"]
    fake.fail_next("502", tool="reply", times=4)  # each dropping page's pass tries it once
    report = flush(trk)
    assert sorted(report["failed"]) == sorted(dropping)
    assert _drops(fake, trk_home, withdrawn) == [] and heard(fake, late) == []
    for key in ("D", "J", "P"):
        assert shows(fake, trk_home, withdrawn[key], handle)
        assert page_row(trk_home, withdrawn[key])["last_error"].startswith("pull-before-drop: the reply to comment")
        assert page_row(trk_home, withdrawn[key])["last_error_at"]
    report = flush(trk)
    assert sorted(report["updated"]) == sorted(dropping)
    assert heard(fake, late) == [("Withdrawn: Dropped the question", "assistant")]
    assert fake.comments[late]["status"] in ("outdated", "resolved")  # left open, then outdated as the box left, or closed with it
    reply_at = max(i for i, (tool, args) in enumerate(fake.calls)  # the closing reply follows the drop, by design
                   if tool == "reply" and not args["body"].startswith("Closed: "))
    drops = _drops(fake, trk_home, withdrawn)
    assert len(drops) == 3 and reply_at < min(drops)


def errors(trk):
    result = trk("pages", "--errors", "--json")
    assert result.code == 0, result.err
    return result.json()


def test_a_reply_refused_for_good_holds_one_flush_and_stays_listed_after_the_drop(trk, trk_home, withdrawn, fake):
    late, d_slug = withdrawn["late"], slug(trk_home, withdrawn["D"])
    fake.grants.discard("reply")  # the reply is refused outright
    report = flush(trk)
    assert withdrawn["D"] in report["failed"] and _drops(fake, trk_home, withdrawn) == []
    assert all(shows(fake, trk_home, withdrawn[k], f"ask-{withdrawn['A']}") for k in ("J", "P"))
    error = page_row(trk_home, withdrawn["J"])["last_error"]
    assert f"the reply to comment {late} was refused for good" in error and "the next flush drops the box" in error
    fake.grants.add("reply")  # replies are allowed again; the refusal stays final
    flush(trk)  # the failed reply is never sent again, so it holds nothing now
    for key in ("D", "J", "P"):
        assert not shows(fake, trk_home, withdrawn[key], f"ask-{withdrawn['A']}")
        assert page_row(trk_home, withdrawn[key])["last_error"] is None
    (failed,) = errors(trk)["failed_replies"]
    assert (failed["item"], failed["slug"], failed["comment"], failed["ask"]) == (withdrawn["D"], d_slug, late,
                                                                                   withdrawn["A"])
    assert failed["body"] == "Withdrawn: Dropped the question" and "tool_not_allowed" in failed["reason"]
    text = trk("pages", "--errors").out
    assert f"reply {failed['reply']} failed" in text and f"comment {late}" in text and f"ask {withdrawn['A']}" in text


def test_a_failed_reply_is_dismissed_with_a_reason_or_resent(trk, trk_home, withdrawn, fake):
    fake.grants.discard("reply")
    flush(trk)
    (failed,) = errors(trk)["failed_replies"]
    reply = str(failed["reply"])
    assert trk("pages", "reply", "dismiss", reply).code == 2  # a dismissal needs a reason
    fake.grants.add("reply")
    resent = trk("pages", "reply", "resend", reply, "--json")
    assert resent.code == 0, resent.err and resent.json()["state"] == "pending"
    assert errors(trk)["failed_replies"] == []
    pulled(trk)
    assert [m for m, _k in heard(fake, withdrawn["late"])] == ["Withdrawn: Dropped the question"]
    assert [r["state"] for r in outbox(trk_home) if r["comment_id"] == withdrawn["late"] and not r["body"].startswith("Closed: ")] == ["sent"]
    assert str(failed["reply"]) not in pages.reply_failures(db(trk_home))
    refused = trk("pages", "reply", "resend", reply, "--json")
    assert refused.code == 1 and refused.error()["rule"] == "no-failed-reply"


def test_a_dismissed_failed_reply_leaves_the_errors_list(trk, trk_home, withdrawn, fake):
    fake.grants.discard("reply")
    flush(trk)
    (failed,) = errors(trk)["failed_replies"]
    done = trk("pages", "reply", "dismiss", str(failed["reply"]), "--reason", "told Kim in person", "--json")
    assert done.code == 0, done.err and done.json()["dismissed"] is True
    assert errors(trk)["failed_replies"] == []
    assert trk("pages", "reply", "dismiss", str(failed["reply"]), "--reason", "again").code == 1


def test_a_failed_reply_on_another_asks_box_does_not_hold_the_drop(trk, trk_home, withdrawn, fake, ask):
    a2 = ask(withdrawn["T"], title="Which colour?")
    flush(trk, "--item", withdrawn["T"])  # T's page only: D, J and P still show the withdrawn box
    other = fake.add_comment(slug(trk_home, withdrawn["T"]), f"ask-{a2}", "Blue.", author_id="u-kim")
    fake.grants.discard("reply")
    pulled(trk)  # records a reply to A2; the tracker's reply is refused and failed
    report = flush(trk)
    assert dispositions(trk_home)[other] == ("reply", a2)
    for key in ("D", "J", "P"):
        assert withdrawn[key] in report["updated"]
        assert not shows(fake, trk_home, withdrawn[key], f"ask-{withdrawn['A']}")


def test_a_401_in_pull_before_drop_holds_the_flush_and_drops_nothing(trk, trk_home, withdrawn, fake):
    fake.fail_next("401", tool="list_comments")
    report = flush(trk)
    assert report["held"] is True and report["renewal_ask"]
    # The top-level page drops no ask handle (it carries none), so the hold does not hold it back: the
    # withdrawal changed its "asks waiting" tile.
    assert report["updated"] == [TOP] and _drops(fake, trk_home, withdrawn) == []
    assert fake.tool_calls("reply") == []
    assert shows(fake, trk_home, withdrawn["D"], f"ask-{withdrawn['A']}")


def test_republish_that_drops_an_ask_handle_pulls_first(trk, trk_home, withdrawn, fake):
    result = trk("pages", "republish", withdrawn["D"])
    assert result.code == 0, result.err
    assert heard(fake, withdrawn["late"]) == [("Withdrawn: Dropped the question", "assistant")]
    reply_at = next(i for i, (tool, _a) in enumerate(fake.calls) if tool == "reply")
    update_at = next(i for i, (tool, _a) in enumerate(fake.calls) if tool == "update")
    assert reply_at < update_at


# ----- ac-4: status changes from re-renders are not comments --------------------------------------


def test_the_fake_marks_comments_outdated_when_their_handle_goes_and_open_when_it_returns(trk, trk_home, asked,
                                                                                        fake):
    d_slug, handle = slug(trk_home, asked["D"]), f"ask-{asked['A']}"
    on_box = fake.add_comment(d_slug, handle, "Feet.", author_id="u-kim")
    done = fake.add_comment(d_slug, handle, "Metres.", author_id="u-kim")
    fake.comments[done]["status"] = "resolved"
    shown = fake.body(d_slug)
    (start, end), = fake_backend.element_spans(shown, handle)
    fake.outside_write(d_slug, body=shown[:start] + shown[end:])
    assert (fake.comments[on_box]["status"], fake.comments[done]["status"]) == ("outdated", "resolved")
    fake.outside_write(d_slug, body=shown)
    assert (fake.comments[on_box]["status"], fake.comments[done]["status"]) == ("open", "resolved")


@TABLE
def test_status_changes_from_re_renders_are_ignored_by_the_pull(trk, trk_home, answered, fake):
    a, kim = answered["A"], answered["kim"]
    assert trk("reopen", a, "--reason", "need units", "--as", "worker").code == 0
    flush(trk)  # the box returns
    before = fake.add_comment(slug(trk_home, answered["P"]), f"ask-{a}", "Old thought.", author_id="u-kim",
                              created_at="2026-10-11T08:00:00.000Z")
    pulled(trk)
    assert dispositions(trk_home)[before] == ("already-answered", a)
    for status in ("outdated", "open", "outdated", "open"):  # re-renders dropping and restoring the box
        fake.comments[before]["status"] = status
        fake.comments[kim]["status"] = status
        assert pulled(trk)["comments"] == []
    assert item(trk, a)["status"] == "open"
    assert len(replies(fake, before)) == 1 and len(replies(fake, kim)) == 4


def test_an_unread_comment_that_went_outdated_is_still_disposed_of(trk, trk_home, asked, fake):
    a = asked["A"]
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Metres.", author_id="u-kim")
    fake.comments[c]["status"] = "outdated"
    pulled(trk)
    assert dispositions(trk_home)[c] == ("reply", a)
