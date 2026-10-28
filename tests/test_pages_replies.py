"""A person's comment on an ask box is a reply; the asker confirms the answer (tr-3vgrb, decided in
"When should a page comment close an ask?" tr-v69zt). Against the in-process fake page backend."""

import json

from test_pages_flush import flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_pull import (TABLE, actor_row, ask, asked, body, dispositions, item, mode, outbox,  # noqa: F401
                             pulled, replies, slug)
from test_pages_setup import fake, journal, meta, setup_argv, token_file, tree  # noqa: F401 (fixtures)

RECEIVED = "Reply received. worker will confirm it as the answer or follow up here."


def comment(fake, trk_home, asked, text="Metres. SI everywhere.\nThanks.", on="D", handle=None, **kw):
    kw.setdefault("author_id", "u-kim")
    return fake.add_comment(slug(trk_home, asked[on]), handle or f"ask-{asked['A']}", text, **kw)


def confirm(trk, ask_id, comment_id, *extra, actor="worker", outcome="Metres"):
    return trk("answer", ask_id, "--from-comment", comment_id, "--outcome", outcome, "--as", actor, *extra, "--json")


# ----- ac-1: a comment journals ask.replied, gets "Reply received", and the ask stays open ------------


@TABLE
def test_ac1_a_comment_on_the_box_is_a_reply_and_the_ask_stays_open_and_blocking(trk, trk_home, asked, fake):
    a = asked["A"]
    since = journal(trk_home)[-1]["seq"]
    c = comment(fake, trk_home, asked, author="Kim Reader")
    report = pulled(trk)
    assert [(e["comment"], e["disposition"], e["ask"]) for e in report["comments"]] == [(c, "reply", a)]
    assert dispositions(trk_home)[c] == ("reply", a)

    (replied,) = [r for r in journal(trk_home, since) if r["change"] == "ask.replied"]
    data = json.loads(replied["data"])
    assert replied["item"] == a and replied["actor"] == "kim" and replied["addressee"] == "kim"
    assert replied["status_old"] is None and replied["status_new"] is None
    assert data["comment_id"] == c and data["slug"] == slug(trk_home, asked["D"])
    assert data["excerpt"] == "Metres. SI everywhere. Thanks."
    assert not [r for r in journal(trk_home, since) if r["change"] == "ask.answered"]

    shown = item(trk, a)
    assert shown["status"] == "open"
    assert shown["replies"] == [{"comment": c, "actor": "kim", "at": replied["at"],
                                 "excerpt": "Metres. SI everywhere. Thanks.", "body": None}]
    assert f"open-ask:{a}" in item(trk, asked["D"])["why_not"]
    # kim's part is done, so the ask waits on its asker, not on them (UP-0036, test_replied_ask.py).
    assert [w["id"] for w in trk("waiting", "--me", "kim", "--json").json()["asks"]] == []
    assert [w["id"] for w in trk("waiting", "--me", "worker", "--json").json()["asks"]] == [a]
    assert replies(fake, c) == [(RECEIVED, "assistant")]
    assert fake.comments[c]["status"] == "open"
    assert "replied" in trk("show", a).out


@TABLE
def test_a_later_comment_on_a_replied_open_ask_records_another_reply(trk, trk_home, asked, fake):
    a = asked["A"]
    first = comment(fake, trk_home, asked, "Metres?")
    pulled(trk)
    second = comment(fake, trk_home, asked, "Feet, on reflection.", on="J", author="Lee Park", author_id="u-lee")
    pulled(trk)
    assert dispositions(trk_home)[second] == ("reply", a)
    assert [(r["comment"], r["actor"]) for r in item(trk, a)["replies"]] == [(first, "kim"), (second, "lee-park")]
    assert replies(fake, second) == [(RECEIVED, "assistant")]
    assert item(trk, a)["status"] == "open"


@TABLE
def test_a_waiting_comment_with_one_ask_open_replies_to_it(trk, trk_home, asked, fake):
    c = comment(fake, trk_home, asked, "Metres.", handle="waiting")
    pulled(trk)
    assert dispositions(trk_home)[c] == ("reply", asked["A"])
    assert item(trk, asked["A"])["status"] == "open"


# ----- ac-2: trk answer --from-comment closes the ask with the comment as the answer -----------------


@TABLE
def test_ac2_the_asker_confirms_a_reply_as_the_answer(trk, trk_home, asked, fake):
    a = asked["A"]
    c = comment(fake, trk_home, asked, author="Kim Reader")
    pulled(trk)
    since = journal(trk_home)[-1]["seq"]
    result = confirm(trk, a, c)
    assert result.code == 0, result.err

    (answered,) = [r for r in journal(trk_home, since) if r["change"] == "ask.answered"]
    data = json.loads(answered["data"])
    assert answered["actor"] == "kim"
    assert data["via"] == "page" and data["comment_id"] == c and data["slug"] == slug(trk_home, asked["D"])
    assert data["proxy"] is False and data["outcome"] == "Metres" and data["confirmed_by"] == "worker"
    shown = item(trk, a)
    assert shown["status"] == "closed" and shown["resolution"] == "done"
    assert trk("section", "get", a, "answer").out.rstrip("\n") == "Metres. SI everywhere.\nThanks."
    assert f"open-ask:{a}" not in item(trk, asked["D"])["why_not"]

    pulled(trk)  # the outbox tells the commenter
    assert replies(fake, c) == [(RECEIVED, "assistant"),
                                ("Recorded as the answer to Which unit?, by kim (for kim). Ask closed.", "assistant")]
    late = comment(fake, trk_home, asked, "Feet?")
    pulled(trk)
    assert replies(fake, late) == [("Already answered by kim: Metres", "assistant")]


def test_the_parents_owner_or_a_human_may_confirm_and_nobody_else(trk, trk_home, asked, fake):
    a = asked["A"]
    c = comment(fake, trk_home, asked)
    pulled(trk)
    refused = confirm(trk, a, c, actor="publisher")  # an agent that is neither the asker nor the owner
    assert refused.code == 1 and refused.error()["rule"] == "ask-permission"
    assert item(trk, a)["status"] == "open"
    assert confirm(trk, a, c, actor="michael").code == 0  # the parent's owner, and a human


def test_from_comment_refuses_a_comment_that_is_no_reply_to_this_ask(trk, trk_home, asked, fake, ask):
    a, other = asked["A"], ask(asked["J"], "Which colour?")
    flush(trk)
    c = comment(fake, trk_home, asked, "Blue.", handle=f"ask-{other}")
    pulled(trk)
    for comment_id in (c, "nope"):
        result = confirm(trk, a, comment_id)
        assert result.code == 1 and result.error()["rule"] == "no-reply", result.err
    assert item(trk, a)["status"] == "open"


def test_from_comment_takes_no_answer_body(trk, trk_home, asked, fake, body):
    c = comment(fake, trk_home, asked)
    pulled(trk)
    assert confirm(trk, asked["A"], c, "--answer", body("Metres.\n")).code == 2
    assert trk("answer", asked["A"], "--outcome", "Metres", "--as", "kim").code == 2  # neither: usage


def test_a_cli_answer_without_from_comment_still_closes_directly(trk, trk_home, asked, fake, body):
    comment(fake, trk_home, asked)
    pulled(trk)
    assert trk("answer", asked["A"], "--outcome", "Metres", "--answer", body("Metres.\n"), "--as", "kim").code == 0
    assert item(trk, asked["A"])["status"] == "closed"
    assert trk("section", "get", asked["A"], "answer").out == "Metres.\n"


# ----- ledger-6: an ask closed any other way tells each unconfirmed reply thread, never resolving it ----


def two_replies(trk, fake, trk_home, asked):
    first = comment(fake, trk_home, asked, "Metres?")
    second = comment(fake, trk_home, asked, "Feet.", on="J", author="Lee Park", author_id="u-lee")
    pulled(trk)
    return first, second


def told(trk, fake, threads, note):
    pulled(trk)  # the outbox sends the notes
    for c in threads:
        assert replies(fake, c) == [(RECEIVED, "assistant"), (note, "assistant")]
        assert fake.comments[c]["status"] == "open"


@TABLE
def test_a_cli_answer_tells_each_reply_thread_how_the_ask_closed(trk, trk_home, asked, fake, body):
    threads = two_replies(trk, fake, trk_home, asked)
    assert trk("answer", asked["A"], "--outcome", "Metres", "--answer", body("Metres.\n"), "--as", "kim").code == 0
    told(trk, fake, threads, "Ask closed: answered by kim via the CLI.")


@TABLE
def test_a_withdrawal_tells_each_reply_thread(trk, trk_home, asked, fake):
    threads = two_replies(trk, fake, trk_home, asked)
    assert trk("withdraw", asked["A"], "--reason", "No longer needed", "--as", "worker").code == 0
    told(trk, fake, threads, "Ask withdrawn: No longer needed.")


def test_closing_the_parent_is_refused_while_the_ask_is_open_and_tells_no_thread(trk, trk_home, asked, fake):
    threads = two_replies(trk, fake, trk_home, asked)
    result = trk("close", asked["D"], "--resolution", "wont-do", "--outcome", "Dropped", "--as", "michael", "--json")
    assert result.code == 1 and result.error()["rule"] == "open-asks"
    pulled(trk)
    for c in threads:
        assert replies(fake, c) == [(RECEIVED, "assistant")]


def test_closing_the_ask_as_a_duplicate_tells_each_reply_thread(trk, trk_home, asked, fake, ask):
    threads = two_replies(trk, fake, trk_home, asked)
    original = ask(asked["J"], "Which unit, really?")
    result = trk("close", asked["A"], "--resolution", "duplicate", "--of", original, "--outcome", "Asked twice",
                 "--as", "worker")
    assert result.code == 0, result.err
    told(trk, fake, threads, f"Ask closed as a duplicate of {original}: Asked twice.")


def test_confirming_one_reply_tells_the_other_threads(trk, trk_home, asked, fake):
    first, second = two_replies(trk, fake, trk_home, asked)
    assert confirm(trk, asked["A"], second).code == 0
    told(trk, fake, [first], "Ask closed: Lee Park's reply was recorded as the answer.")
    assert replies(fake, second)[-1][0].startswith("Recorded as the answer to Which unit?")
    assert len(replies(fake, second)) == 2


def test_a_close_tells_only_the_threads_since_the_latest_opening(trk, trk_home, asked, fake, body):
    a = asked["A"]
    before = comment(fake, trk_home, asked, "Metres?")
    pulled(trk)
    assert confirm(trk, a, before).code == 0
    assert trk("reopen", a, "--reason", "Which metres?", "--as", "worker").code == 0
    pulled(trk)
    sent = len(replies(fake, before))
    after = comment(fake, trk_home, asked, "SI metres.")
    pulled(trk)
    assert trk("withdraw", a, "--reason", "Settled elsewhere", "--as", "worker").code == 0
    told(trk, fake, [after], "Ask withdrawn: Settled elsewhere.")
    assert len(replies(fake, before)) == sent


def test_a_close_with_no_reply_queues_nothing(trk, trk_home, asked, fake):
    queued = len(outbox(trk_home))
    assert trk("withdraw", asked["A"], "--reason", "No longer needed", "--as", "worker").code == 0
    assert len(outbox(trk_home)) == queued


# ----- ac-3: the box on the program page shows the reply ---------------------------------------------


@TABLE
def test_ac3_the_box_shows_the_latest_reply_and_awaiting_confirmation(trk, trk_home, asked, fake):
    """On the program pages, which show every open ask in the subtree. The addressee's Needs-you page is
    where the ask leaves once they have replied (UP-0036): it lists what waits on them, and this does not."""
    a = asked["A"]
    comment(fake, trk_home, asked, "Metres?")
    pulled(trk)
    comment(fake, trk_home, asked, "Feet, on reflection.", on="J")
    pulled(trk)
    flush(trk)
    page = fake.body(page_row(trk_home, asked["P"])["slug"])
    box = page[page.index(f'data-comment-id="ask-{a}"'):]
    box = box[:box.index('<p class="cta">')]  # the box holds the question's own div
    assert "Feet, on reflection." in box and "awaiting confirmation" in box and "Metres?" not in box
    kims = fake.body(page_row(trk_home, "needs-you:kim")["slug"])
    assert f'data-comment-id="ask-{a}"' not in kims and "Nothing is waiting on you" in kims


def test_a_box_with_no_reply_shows_no_confirmation_line(trk, trk_home, asked, fake):
    assert "awaiting confirmation" not in fake.body(slug(trk_home, asked["P"]))


def test_unknown_author_reply_registers_them(trk, trk_home, asked, fake):
    comment(fake, trk_home, asked, "Metres.", author="Sam Doe", author_id="u-sam")
    pulled(trk)
    assert actor_row(trk_home, "sam-doe")["service_user_id"] == "u-sam"
    assert item(trk, asked["A"])["replies"][0]["actor"] == "sam-doe"
    assert outbox(trk_home)[-1]["body"] == RECEIVED


def test_a_first_comment_links_to_the_one_unlinked_human_with_the_same_name(trk, trk_home, asked, fake):
    assert trk("actor", "add", "sam", "--kind", "human", "--display", "Sam Doe").code == 0
    since = journal(trk_home)[-1]["seq"]
    comment(fake, trk_home, asked, "Metres.", author="Sam  Doe", author_id="u-sam")
    pulled(trk)
    assert actor_row(trk_home, "sam")["service_user_id"] == "u-sam"
    assert actor_row(trk_home, "sam-doe") is None
    assert item(trk, asked["A"])["replies"][0]["actor"] == "sam"
    assert outbox(trk_home)[-1]["body"] == RECEIVED
    linked = [r for r in journal(trk_home, since) if r["change"] == "actor.updated"]
    assert [json.loads(r["data"])["fields"] for r in linked] == [["service_user"]]


def test_an_ambiguous_name_registers_a_new_human_and_the_reply_says_to_merge(trk, trk_home, asked, fake):
    for actor in ("sam", "samuel"):
        assert trk("actor", "add", actor, "--kind", "human", "--display", "Sam Doe").code == 0
    comment(fake, trk_home, asked, "Metres.", author="Sam Doe", author_id="u-sam")
    pulled(trk)
    assert actor_row(trk_home, "sam")["service_user_id"] is None
    assert actor_row(trk_home, "samuel")["service_user_id"] is None
    assert actor_row(trk_home, "sam-doe")["service_user_id"] == "u-sam"
    assert outbox(trk_home)[-1]["body"] == RECEIVED + (
        " An admin should merge them if they are the same person: `trk actor link <actor> --service-user u-sam`.")


def test_a_name_held_by_a_linked_human_registers_a_new_human_and_the_reply_says_to_merge(trk, trk_home, asked,
                                                                                         fake):
    assert trk("actor", "add", "sam", "--kind", "human", "--display", "Sam Doe").code == 0
    assert trk("actor", "link", "sam", "--service-user", "u-other").code == 0
    comment(fake, trk_home, asked, "Metres.", author="Sam Doe", author_id="u-sam")
    pulled(trk)
    assert actor_row(trk_home, "sam")["service_user_id"] == "u-other"
    assert actor_row(trk_home, "sam-doe")["service_user_id"] == "u-sam"
    assert "An admin should merge them" in outbox(trk_home)[-1]["body"]


def test_actor_add_human_says_their_first_comment_links_automatically(trk):
    assert trk("init", "--me", "michael").code == 0
    added = trk("actor", "add", "sam", "--kind", "human", "--display", "Sam Doe")
    assert "first page comment links" in added.err and added.out.strip() == "sam"
    assert "first page comment" not in trk("actor", "add", "bot", "--kind", "agent").err
