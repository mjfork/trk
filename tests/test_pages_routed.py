"""A person's page comment outside an ask box is routed to the item's owner (tr-yd424, Page publisher spec,
Comments as answers) and an agent answers or acknowledges it. Against the in-process fake artifact service."""

import json

import pytest

from test_pages_flush import flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_pull import (TABLE, actor_row, asked, ask, body, dispositions, item, mode, outbox,  # noqa: F401
                             pulled, replies, slug)
from test_pages_setup import fake, journal, meta, setup_argv, token_file, tree  # noqa: F401 (fixtures)

SENT = "Sent to michael; they'll reply here."


def routed_rows(trk_home, since=0):
    return [r for r in journal(trk_home, since) if r["change"] == "comment.routed"]


def comment(fake, trk_home, asked, text="Should this also cover imports?", on="D", handle="header", **kw):
    kw.setdefault("author", "Sam Doe")
    kw.setdefault("author_id", "u-sam")
    return fake.add_comment(slug(trk_home, asked[on]), handle, text, **kw)


@TABLE
def test_ac1_a_comment_off_the_boxes_is_routed_to_the_owner_and_the_thread_stays_open(trk, trk_home, asked, fake):
    since = journal(trk_home)[-1]["seq"]
    c = comment(fake, trk_home, asked, handle="what-to-build")
    report = pulled(trk)
    assert [(e["comment"], e["disposition"], e["ask"]) for e in report["comments"]] == [(c, "routed", None)]
    assert dispositions(trk_home)[c] == ("routed", None)
    (row,) = routed_rows(trk_home, since)
    data = json.loads(row["data"])
    assert row["item"] == asked["D"] and row["owner"] == "michael" and row["addressee"] == "michael"
    assert row["actor"] == "sam-doe" and row["status_old"] is None and row["status_new"] is None
    assert data == {"slug": slug(trk_home, asked["D"]), "handle": "what-to-build", "comment_id": c,
                    "excerpt": "Should this also cover imports?"}
    assert replies(fake, c) == [(SENT, "assistant")]
    assert fake.comments[c]["status"] == "open"
    assert [(r["resolve"], r["state"]) for r in outbox(trk_home)] == [(0, "sent")]
    assert actor_row(trk_home, "sam-doe")["service_user_id"] == "u-sam"
    pulled(trk)
    assert len(routed_rows(trk_home, since)) == 1 and replies(fake, c) == [(SENT, "assistant")]  # once


@TABLE
def test_a_comment_on_the_page_itself_with_no_handle_is_routed_too(trk, trk_home, asked, fake):
    c = comment(fake, trk_home, asked, handle=None)
    pulled(trk)
    assert dispositions(trk_home)[c] == ("routed", None)
    assert json.loads(routed_rows(trk_home)[-1]["data"])["handle"] is None


@TABLE
def test_a_comment_on_a_needs_you_page_off_a_box_goes_to_that_pages_person(trk, trk_home, asked, fake):
    since = journal(trk_home)[-1]["seq"]
    page = page_row(trk_home, "needs-you:kim")["slug"]
    c = fake.add_comment(page, "needs-you", "Can I get this earlier?", author="Sam Doe", author_id="u-sam")
    pulled(trk)
    (row,) = routed_rows(trk_home, since)
    assert row["item"] is None and row["addressee"] == "kim" and row["actor"] == "sam-doe"
    assert json.loads(row["data"])["page"] == "needs-you:kim"
    assert replies(fake, c) == [("Sent to kim; they'll reply here.", "assistant")]


@TABLE
def test_agent_and_empty_comments_are_still_ignored(trk, trk_home, asked, fake):
    bot = comment(fake, trk_home, asked, author_kind="assistant", author="Claude", author_id="u-bot")
    blank = comment(fake, trk_home, asked, "  ")
    pulled(trk)
    assert dispositions(trk_home)[bot][0] == "ignored" and dispositions(trk_home)[blank][0] == "ignored"
    assert routed_rows(trk_home) == [] and replies(fake, bot) == [] and replies(fake, blank) == []


@TABLE
def test_ac2_a_comment_on_an_ask_box_is_still_a_reply_not_a_routed_comment(trk, trk_home, asked, fake):
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author="Kim Reader",
                         author_id="u-kim")
    pulled(trk)
    assert dispositions(trk_home)[c] == ("reply", asked["A"]) and routed_rows(trk_home) == []


@TABLE
def test_a_page_with_no_open_ask_is_read_only_where_the_service_offers_the_activity_log(trk, trk_home, asked, fake,
                                                                                         mode):
    """The per-page fallback reads only pages that show an open ask; the activity log covers every page, T's
    included (the 2026-10-13 comment on tr-zpgz6 sat on a page with no open ask)."""
    c = comment(fake, trk_home, asked, on="T")
    pulled(trk)
    routed = dispositions(trk_home).get(c)
    assert routed == (("routed", None) if mode == "activity" else None)


def test_ac3_show_lists_the_routed_comments_nobody_has_acknowledged(trk, trk_home, asked, fake):
    first = comment(fake, trk_home, asked, "First point", handle="what-to-build")
    second = comment(fake, trk_home, asked, "Second point", handle="header", author="Lee Park", author_id="u-lee")
    other = comment(fake, trk_home, asked, "On the job", on="J")
    pulled(trk)
    listed = item(trk, asked["D"])["comments"]
    assert [(c["comment"], c["author"], c["handle"], c["body"]) for c in listed] == [
        (first, "sam-doe", "what-to-build", "First point"), (second, "lee-park", "header", "Second point")]
    assert [c["comment"] for c in item(trk, asked["J"])["comments"]] == [other]
    assert "comments" not in item(trk, asked["A"])  # asks carry replies, not comments
    assert "comments to acknowledge" in trk("show", asked["D"]).out


def test_ac4_reply_posts_in_the_thread_and_ack_takes_it_off_the_list(trk, trk_home, asked, fake, body):
    c = comment(fake, trk_home, asked)
    pulled(trk)
    since = journal(trk_home)[-1]["seq"]
    result = trk("comment", "reply", c, "--body", body("Yes: imports too.\n"), "--as", "worker")
    assert result.code == 0, result.err
    assert [r["state"] for r in outbox(trk_home)][-1] == "pending"
    pulled(trk)
    assert replies(fake, c) == [(SENT, "assistant"), ("Yes: imports too.\n", "assistant")]
    assert fake.comments[c]["status"] == "open" and [x["comment"] for x in item(trk, asked["D"])["comments"]] == [c]

    result = trk("comment", "ack", c, "--as", "worker", "--json")
    assert result.code == 0, result.err
    (row,) = [r for r in journal(trk_home, since) if r["change"] == "comment.acknowledged"]
    assert row["actor"] == "worker" and row["item"] == asked["D"] and json.loads(row["data"]) == {"comment_id": c}
    assert item(trk, asked["D"])["comments"] == []
    again = trk("comment", "ack", c, "--as", "worker")
    assert again.code == 1 and "already acknowledged" in again.err
    assert trk("comment", "reply", c, "--body", body("x\n"), "--as", "worker").code == 1


def test_ack_can_name_the_ask_that_took_the_point_on(trk, trk_home, asked, fake):
    c = comment(fake, trk_home, asked)
    pulled(trk)
    assert trk("comment", "ack", c, "--ask", asked["A"], "--as", "worker").code == 0
    assert json.loads(journal(trk_home)[-1]["data"]) == {"ask": asked["A"], "comment_id": c}


def test_reply_and_ack_refuse_a_comment_that_was_never_routed(trk, trk_home, asked, fake, body):
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    pulled(trk)
    for argv in (("ack", c), ("reply", c, "--body", body("x\n")), ("ack", "c-nope")):
        refused = trk("comment", *argv, "--as", "worker")
        assert refused.code == 1 and "no routed comment" in refused.err


@pytest.mark.parametrize("answered", ["tracker-reply", "resolved"])
@TABLE
def test_a_thread_already_resolved_or_replied_to_is_ignored_not_routed(trk, trk_home, asked, fake, answered):
    """After a store restore rewound `page_comments`, or when somebody resolved it, the thread gets no second word."""
    since = journal(trk_home)[-1]["seq"]
    c = comment(fake, trk_home, asked)
    if answered == "tracker-reply":
        fake.add_reply(c, "Earlier word.", author="Michael Fork", author_id="u-owner", author_kind="assistant")
    else:
        fake.comments[c]["status"] = "resolved"
        fake._event("status", fake.comments[c], "2026-10-11T12:30:00.000Z", status="resolved")
    pulled(trk)
    assert dispositions(trk_home)[c] == ("ignored", None) and outbox(trk_home) == []
    assert routed_rows(trk_home, since) == [] and fake.tool_calls("reply_to_artifact_comment") == []
    assert item(trk, asked["D"])["comments"] == []
