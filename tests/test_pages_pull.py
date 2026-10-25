"""Comments as answers: `trk pages pull`, the pull half of `trk pages tick`, `trk actor link` and
`trk pages authors` (Page publisher spec: Comments as answers; AC 6 and AC 7), against the in-process
fake page backend. No real backend is ever called."""

import json
import threading

import pytest

from fake_backend import ACTIVITY
from test_pages_flush import db, flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_setup import fake, journal, meta, setup_argv, token_file, tree  # noqa: F401 (fixtures)
from trk import pull, store


@pytest.fixture
def body(tmp_path):
    def write(text, name="body.md"):
        path = tmp_path / name
        path.write_text(text)
        return f"@{path}"

    return write


@pytest.fixture
def ask(trk, body):
    """Open an ask as `worker` (to `product`, so addressed to kim); returns its id."""

    def open_ask(parent, title="Which unit?", role="product"):
        result = trk("ask", parent, "--to", role, title, "--question", body("Metres or feet?\n", "q.md"),
                     "--as", "worker", "--json")
        assert result.code == 0, result.err
        return result.json()["item"]["id"]

    return open_ask


def slug(trk_home, item_id):
    return page_row(trk_home, item_id)["slug"]


def pulled(trk):
    result = trk("pages", "pull", "--json")
    assert result.code == 0, result.err
    return result.json()


def item(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


RECEIVED = "Reply received. worker will confirm it as the answer or follow up here."


def confirm(trk, ask_id, comment_id, actor="worker", outcome="Metres."):
    """The asker confirms a reply as the ask's answer (tr-3vgrb)."""
    result = trk("answer", ask_id, "--from-comment", comment_id, "--outcome", outcome, "--as", actor)
    assert result.code == 0, result.err
    return result


def replies(fake, comment_id):
    return [(r["message"], r["authorKind"]) for r in fake.comments[comment_id]["replies"]]


def dispositions(trk_home):
    conn = db(trk_home)
    try:
        return {r["comment_id"]: (r["disposition"], r["ask_id"])
                for r in conn.execute("SELECT * FROM page_comments")}
    finally:
        conn.close()


def outbox(trk_home):
    conn = db(trk_home)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM page_replies ORDER BY id")]
    finally:
        conn.close()


def actor_row(trk_home, actor_id):
    conn = db(trk_home)
    try:
        return conn.execute("SELECT * FROM actors WHERE id = ?", (actor_id,)).fetchone()
    finally:
        conn.close()


@pytest.fixture(params=["pages", "activity"])
def mode(request, trk, trk_home, live, fake, monkeypatch):
    """The disposition table runs twice: listing page by page, and reading the cross-page activity log
    (when the backend offers it). In activity mode the first pull seeds `comment_cursor` before the test writes any
    comment, and the per-page read is never used again."""
    if request.param == "activity":
        fake.activity = True
        fake.grants.add(ACTIVITY)
        assert pulled(trk)["activity"]["seeded"] is True
        assert meta(trk_home, "comment_cursor") is not None

        def per_page(*args):
            raise AssertionError("the per-page read ran in activity mode")

        monkeypatch.setattr(pull, "_read", per_page)
        fake.calls.clear()
    return request.param


TABLE = pytest.mark.usefixtures("mode")  # a disposition-table test: run in both modes


@pytest.fixture
def asked(trk, trk_home, live, fake, ask):
    """AC 6's setting: kim linked to u-kim, one ask on D to product, every page published."""
    assert trk("actor", "link", "kim", "--service-user", "u-kim").code == 0
    a = ask(live["D"])
    flush(trk)
    fake.calls.clear()
    return {**live, "A": a}


# ----- AC 6 (amended by tr-3vgrb): comments are replies; the asker confirms one; the box goes --------


@TABLE
def test_ac6_comments_are_replies_the_asker_confirms_one_and_a_new_author_is_registered(trk, trk_home, asked, fake):
    a = asked["A"]
    since = journal(trk_home)[-1]["seq"]
    first = fake.add_comment(slug(trk_home, asked["P"]), f"ask-{a}", "Metres. SI everywhere.\nThanks.",
                             author="Kim Reader", author_id="u-kim")
    second = fake.add_comment(slug(trk_home, asked["J"]), f"ask-{a}", "Feet, surely", author="Lee Park",
                              author_id="u-new")
    result = trk("pages", "tick", "--json")
    assert result.code == 0, result.err
    report = result.json()
    assert [(e["comment"], e["disposition"]) for e in report["pull"]["comments"]] == \
        [(first, "reply"), (second, "reply")]
    rows = journal(trk_home, since)
    assert [(r["actor"], json.loads(r["data"])["comment_id"]) for r in rows if r["change"] == "ask.replied"] == \
        [("kim", first), ("lee-park", second)]
    assert not [r for r in rows if r["change"] == "ask.answered"]
    assert item(trk, a)["status"] == "open" and f"open-ask:{a}" in item(trk, asked["D"])["why_not"]
    assert replies(fake, first) == [(RECEIVED, "assistant")] and replies(fake, second) == [(RECEIVED, "assistant")]

    (added,) = [r for r in rows if r["change"] == "actor.added"]
    assert added["actor"] == "publisher" and json.loads(added["data"]) == \
        {"id": "lee-park", "kind": "human", "display": "Lee Park"}
    assert actor_row(trk_home, "lee-park")["service_user_id"] == "u-new"

    confirm(trk, a, first)
    (answered,) = [r for r in journal(trk_home, since) if r["change"] == "ask.answered"]
    data = json.loads(answered["data"])
    assert answered["item"] == a and answered["actor"] == "kim"
    assert data["proxy"] is False and data["via"] == "page" and data["confirmed_by"] == "worker"
    assert data["slug"] == slug(trk_home, asked["P"]) and data["comment_id"] == first
    assert data["outcome"] == "Metres."
    shown = item(trk, a)
    assert shown["status"] == "closed" and shown["resolution"] == "done"
    assert trk("section", "get", a, "answer").out.rstrip("\n") == "Metres. SI everywhere.\nThanks."

    pulled(trk)
    assert replies(fake, first) == [(RECEIVED, "assistant"),
                                    ("Recorded as the answer to Which unit?, by kim (for kim). Ask closed.",
                                     "assistant")]
    assert fake.comments[first]["status"] == "open" and fake.comments[second]["status"] == "open"
    assert replies(fake, second) == [(RECEIVED, "assistant"),  # the other reply hears how it closed
                                     ("Ask closed: kim's reply was recorded as the answer.", "assistant")]

    assert f"open-ask:{a}" not in item(trk, asked["D"])["why_not"]
    flush(trk)
    for key in ("D", "J", "P"):
        assert f'data-comment-id="ask-{a}"' not in fake.body(slug(trk_home, asked[key]))


@TABLE
def test_a_second_pull_acts_on_nothing_again(trk, trk_home, asked, fake):
    first = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    pulled(trk)
    again = pulled(trk)
    assert again["comments"] == [] and again["replies"]["sent"] == []
    assert len(fake.comments[first]["replies"]) == 1
    assert len(fake.tool_calls("reply")) == 1


# ----- tr-jmmjh: the tracker replies and never resolves a person's thread ------------------------------


def never_resolved(fake):
    """No reply the tracker sent asked to resolve its thread, but a closing reply (Pub 17), and no other call did."""
    return (all(call["resolve"] is False for call in fake.tool_calls("reply")
                if not call["body"].startswith("Closed: "))
            and not fake.tool_calls("resolve_comment"))


@TABLE
def test_every_reply_leaves_the_persons_thread_open(trk, trk_home, asked, fake, ask, body):
    """The answer, "Already answered", "Withdrawn", "Which ask?" and the routed comment's "Sent to" alike."""
    a = asked["A"]
    answer = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Metres.", author_id="u-kim")
    pulled(trk)
    confirm(trk, a, answer)
    again = fake.add_comment(slug(trk_home, asked["J"]), f"ask-{a}", "Feet.", author_id="u-kim")
    a2 = ask(asked["T"], "Which colour?")
    assert trk("withdraw", a2, "--reason", "not needed", "--as", "worker").code == 0
    gone = fake.add_comment(slug(trk_home, asked["T"]), f"ask-{a2}", "Blue.", author_id="u-kim")
    pulled(trk)
    ask(asked["T"], "A3"), ask(asked["J"], "A4")
    flush(trk)
    which = fake.add_comment(slug(trk_home, asked["J"]), "waiting", "Yes.", author_id="u-kim")
    off = fake.add_comment(slug(trk_home, asked["J"]), "header", "Yes.", author_id="u-kim")
    pulled(trk)
    assert [dispositions(trk_home)[c][0] for c in (answer, again, gone, which, off)] == \
        ["reply", "already-answered", "withdrawn", "which-ask", "routed"]
    for c in (answer, again, gone):  # the box left the page: a closing reply, then the thread resolves (Pub 17)
        assert replies(fake, c)[-1][0].startswith("Closed: ") and fake.comments[c]["status"] == "resolved"
        assert len(replies(fake, c)) == (3 if c == answer else 2)
    for c in (which, off):  # not on a box that left: one reply, still open
        assert len(replies(fake, c)) == 1 and fake.comments[c]["status"] != "resolved"
    assert never_resolved(fake) and {r["resolve"] for r in outbox(trk_home)} == {0}


@TABLE
def test_a_person_resolving_their_thread_later_changes_nothing(trk, trk_home, asked, fake, body):
    a = asked["A"]
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Metres.", author_id="u-kim")
    pulled(trk)
    confirm(trk, a, c)
    pulled(trk)
    since, shown, before = journal(trk_home)[-1]["seq"], item(trk, a), dispositions(trk_home)
    fake.comments[c]["status"] = "resolved"  # the person resolves it in the web UI
    fake._event("status", fake.comments[c], "2026-10-11T13:00:00.000Z", status="resolved")
    report = pulled(trk)
    assert report["comments"] == [] and report["replies"]["sent"] == []
    assert journal(trk_home, since) == [] and item(trk, a) == shown and dispositions(trk_home) == before
    assert len(replies(fake, c)) == 2
    # resolved after a reopen, it is still no answer to the reopened ask
    assert trk("reopen", a, "--reason", "need units", "--as", "worker").code == 0
    pulled(trk)
    fake.comments[c]["status"] = "resolved"
    fake._event("status", fake.comments[c], "2026-10-11T23:00:00.000Z", status="resolved")
    pulled(trk)
    assert item(trk, a)["status"] == "open" and dispositions(trk_home) == before


def test_an_older_outbox_row_asking_to_resolve_is_sent_without_resolving(trk, trk_home, asked, fake):
    """A row queued by a build that still resolved goes out as a plain reply."""
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    conn = db(trk_home)
    with conn:
        conn.execute("INSERT INTO page_replies (slug, comment_id, body, resolve, created_at) VALUES (?, ?, ?, 1, ?)",
                     (slug(trk_home, asked["D"]), c, "Recorded earlier.", "2026-10-11T12:00:00.000Z"))
        conn.execute("INSERT INTO page_comments (comment_id, slug, handle, ask_id, disposition, seen_at) "
                     "VALUES (?, ?, ?, ?, 'answer', ?)", (c, slug(trk_home, asked["D"]), f"ask-{asked['A']}",
                                                          asked["A"], "2026-10-11T12:00:00.000Z"))
    conn.close()
    report = pulled(trk)
    assert report["replies"]["sent"] == [c] and never_resolved(fake)
    assert fake.comments[c]["status"] == "open" and [r["state"] for r in outbox(trk_home)] == ["sent"]


# ----- AC 7: a comment on `waiting` with several asks, or none --------------------------------------


@TABLE
def test_ac7_waiting_with_two_asks_or_none_is_told_and_stays_open(trk, trk_home, live, fake, ask):
    a2, a3 = ask(live["J"], "A2"), ask(live["T"], "A3")
    flush(trk)
    on_j = fake.add_comment(slug(trk_home, live["J"]), "waiting", "Yes")
    pulled(trk)
    assert replies(fake, on_j) == [("Which ask? Comment on its box instead.", "assistant")]
    assert fake.comments[on_j]["status"] == "open"
    assert dispositions(trk_home)[on_j] == ("which-ask", None)

    assert trk("withdraw", a3, "--reason", "not needed", "--as", "worker").code == 0
    on_t = fake.add_comment(slug(trk_home, live["T"]), "waiting", "Yes")
    pulled(trk)
    assert replies(fake, on_t) == [("Nothing is waiting here.", "assistant")]
    assert fake.comments[on_t]["status"] == "open"
    assert item(trk, a2)["status"] == "open"


# ----- the disposition table ------------------------------------------------------------------------


@TABLE
def test_replies_are_recorded_oldest_first_whatever_page_they_are_on(trk, trk_home, asked, fake):
    a = asked["A"]
    later = fake.add_comment(slug(trk_home, asked["P"]), f"ask-{a}", "Feet.", author_id="u-kim",
                             created_at="2026-10-11T12:30:00.000Z")
    earlier = fake.add_comment(slug(trk_home, asked["J"]), f"ask-{a}", "Metres.",
                               author_id="u-kim", created_at="2026-10-11T12:10:00.000Z")
    pulled(trk)
    assert dispositions(trk_home) == {earlier: ("reply", a), later: ("reply", a)}
    assert [r["comment"] for r in item(trk, a)["replies"]] == [earlier, later]
    assert item(trk, a)["status"] == "open"


@TABLE
def test_a_comment_after_a_cli_answer_is_already_answered(trk, trk_home, asked, fake, body):
    a = asked["A"]
    assert trk("answer", a, "--outcome", "Metres", "--answer", body("Metres.\n"), "--as", "kim").code == 0
    late = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Feet?", author_id="u-kim")
    pulled(trk)
    assert dispositions(trk_home)[late] == ("already-answered", a)
    assert replies(fake, late) == [("Already answered by kim: Metres", "assistant")]
    assert fake.comments[late]["status"] == "open"


@TABLE
def test_a_comment_on_a_withdrawn_ask_is_told_so_and_left_open(trk, trk_home, asked, fake):
    a = asked["A"]
    assert trk("withdraw", a, "--reason", "not needed", "--as", "worker").code == 0
    late = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Metres.", author_id="u-kim")
    pulled(trk)
    assert dispositions(trk_home)[late] == ("withdrawn", a)
    assert replies(fake, late) == [("Withdrawn: not needed", "assistant")]
    assert fake.comments[late]["status"] == "open"
    assert item(trk, a)["resolution"] == "wont-do"


@TABLE
def test_a_waiting_comment_with_one_ask_open_is_its_reply(trk, trk_home, asked, fake):
    a = asked["A"]
    on_d = fake.add_comment(slug(trk_home, asked["D"]), "waiting", "Metres.", author_id="u-kim")
    pulled(trk)
    assert dispositions(trk_home)[on_d] == ("reply", a)
    confirm(trk, a, on_d)
    answered = [r for r in journal(trk_home) if r["change"] == "ask.answered"][-1]
    assert json.loads(answered["data"])["comment_id"] == on_d and answered["actor"] == "kim"


@TABLE
def test_a_waiting_comment_whose_one_ask_closed_since_gets_that_asks_reply(trk, trk_home, asked, fake, body,
                                                                         monkeypatch):
    a = asked["A"]
    on_d = fake.add_comment(slug(trk_home, asked["D"]), "waiting", "Metres.", author_id="u-kim",
                            created_at="2026-10-11T12:10:00.000Z")
    monkeypatch.setenv("TRK_NOW", "2026-10-11T12:20:00Z")  # answered on the CLI after the comment, before the pull
    assert trk("answer", a, "--outcome", "Metres", "--answer", body("Metres.\n"), "--as", "kim").code == 0
    pulled(trk)
    assert dispositions(trk_home)[on_d] == ("already-answered", a)
    assert replies(fake, on_d) == [("Already answered by kim: Metres", "assistant")]


@TABLE
def test_an_empty_comment_on_an_ask_is_ignored_and_registers_no_one(trk, trk_home, asked, fake):
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "  ", author="Sam", author_id="u-sam")
    pulled(trk)
    assert dispositions(trk_home)[c] == ("ignored", asked["A"])
    assert actor_row(trk_home, "sam") is None and item(trk, asked["A"])["status"] == "open"


SENT = "Sent to michael; they'll reply here."


@pytest.mark.parametrize("handle", ["header", "tiles", None, "ask-tr-nope0"])
@TABLE
def test_a_root_comment_on_anything_else_is_routed_to_the_owner(trk, trk_home, asked, fake, handle):
    """tr-yd424: not an ask box, so not an answer; it goes to the item's owner (tests/test_pages_routed.py)."""
    other = fake.add_comment(slug(trk_home, asked["P"]), handle, "Metres.", author="Sam", author_id="u-sam")
    report = pulled(trk)
    assert dispositions(trk_home)[other] == ("routed", None) and report["comments"][0]["disposition"] == "routed"
    assert replies(fake, other) == [(SENT, "assistant")]
    assert fake.comments[other]["status"] == "open"  # the thread stays open
    assert [(r["comment_id"], r["resolve"], r["state"]) for r in outbox(trk_home)] == [(other, 0, "sent")]
    assert actor_row(trk_home, "sam")["service_user_id"] == "u-sam"  # the commenter is registered, as on a box
    assert item(trk, asked["A"])["status"] == "open"


@TABLE
def test_a_routed_comment_gets_one_reply_over_repeated_pulls(trk, trk_home, asked, fake):
    c = fake.add_comment(slug(trk_home, asked["P"]), "header", "Metres.", author_id="u-kim")
    pulled(trk)
    again = pulled(trk)
    assert again["comments"] == [] and again["replies"]["sent"] == []
    assert len(replies(fake, c)) == 1 and len(fake.tool_calls("reply")) == 1


@TABLE
def test_no_reply_to_an_off_box_comment_by_an_assistant(trk, trk_home, asked, fake):
    c = fake.add_comment(slug(trk_home, asked["P"]), "header", "Metres.", author="Bot", author_id="u-owner",
                         author_kind="assistant")
    pulled(trk)
    assert dispositions(trk_home)[c] == ("ignored", None)
    assert fake.comments[c]["replies"] == [] and outbox(trk_home) == []


@TABLE
def test_a_crash_after_a_routed_comments_disposition_sends_its_one_reply_next_pull(trk, trk_home, asked, fake,
                                                                                   monkeypatch):
    c = fake.add_comment(slug(trk_home, asked["P"]), "header", "Metres.", author_id="u-kim")
    with monkeypatch.context() as patched:
        patched.setattr(pull, "_send_outbox", lambda *args, **kw: (_ for _ in ()).throw(SystemExit(9)))
        with pytest.raises(SystemExit):
            trk("pages", "pull")
    assert dispositions(trk_home)[c] == ("routed", None) and [r["state"] for r in outbox(trk_home)] == ["pending"]
    assert len([r for r in journal(trk_home) if r["change"] == "comment.routed"]) == 1
    report = pulled(trk)
    assert report["comments"] == [] and report["replies"]["sent"] == [c]
    fake.fail_next("502-after", "reply")  # nothing is pending now, so nothing is sent
    pulled(trk)
    assert len(replies(fake, c)) == 1 and [r["state"] for r in outbox(trk_home)] == ["sent"]


@TABLE
def test_a_routed_comments_reply_lost_ambiguously_is_not_sent_twice(trk, trk_home, asked, fake):
    c = fake.add_comment(slug(trk_home, asked["P"]), "header", "Metres.", author_id="u-kim")
    fake.fail_next("502-after", "reply")  # posted, then the edge answered 502
    assert pulled(trk)["replies"]["pending"] == [c]
    assert pulled(trk)["replies"]["sent"] == [c]
    assert len(replies(fake, c)) == 1 and fake.comments[c]["status"] == "open"


@TABLE
def test_unknown_authors_are_registered_with_de_duplicated_ids(trk, trk_home, asked, fake):
    a = asked["A"]
    fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Metres.", author="Lee Park", author_id="u-1")
    fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Feet.", author="Lee  Park", author_id="u-2")
    pulled(trk)
    assert actor_row(trk_home, "lee-park")["service_user_id"] == "u-1"
    assert actor_row(trk_home, "lee-park-2")["service_user_id"] == "u-2"
    assert actor_row(trk_home, "lee-park")["kind"] == "human"
    confirm(trk, a, item(trk, a)["replies"][0]["comment"])
    answered = [r for r in journal(trk_home) if r["change"] == "ask.answered"][-1]
    assert answered["actor"] == "lee-park" and json.loads(answered["data"])["proxy"] is True


@TABLE
def test_a_linked_author_answers_as_the_linked_actor(trk, trk_home, asked, fake):
    assert trk("actor", "add", "lee", "--kind", "human", "--display", "Lee").code == 0
    assert trk("actor", "link", "lee", "--service-user", "u-lee").code == 0
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author="Lee Park",
                         author_id="u-lee")
    pulled(trk)
    assert item(trk, asked["A"])["replies"][0]["actor"] == "lee"
    confirm(trk, asked["A"], c)
    pulled(trk)
    answered = [r for r in journal(trk_home) if r["change"] == "ask.answered"][-1]
    assert answered["actor"] == "lee"
    assert replies(fake, c)[1][0] == "Recorded as the answer to Which unit?, by Lee (for kim). Ask closed."
    assert actor_row(trk_home, "lee-park") is None


def test_before_change_1_an_open_asks_comment_points_at_trk_answer(trk, trk_home, asked, fake, body):
    a = asked["A"]
    fake.author_ids = False
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Metres.")
    pulled(trk)
    assert dispositions(trk_home)[c] == ("use-cli", a)
    assert replies(fake, c) == [(f"Answering by comment isn't enabled yet: answer with `trk answer {a}`.", "assistant")]
    assert fake.comments[c]["status"] == "open" and item(trk, a)["status"] == "open"
    # later comments on an answered ask are handled as with change 1
    assert trk("answer", a, "--outcome", "Metres", "--answer", body("Metres.\n"), "--as", "kim").code == 0
    late = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Feet?")
    pulled(trk)
    assert dispositions(trk_home)[late] == ("already-answered", a)
    assert fake.comments[late]["status"] == "open"


@TABLE
def test_a_crash_between_disposition_and_reply_is_recovered_by_the_next_pull(trk, trk_home, asked, fake,
                                                                             monkeypatch):
    a = asked["A"]
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Metres.", author_id="u-kim")

    def crash(*args):
        raise SystemExit(9)  # the process dies after the disposition committed

    with monkeypatch.context() as patched:
        patched.setattr(pull, "_send_outbox", crash)
        with pytest.raises(SystemExit):
            trk("pages", "pull")
    assert item(trk, a)["status"] == "open" and fake.comments[c]["replies"] == []
    assert [r["state"] for r in outbox(trk_home)] == ["pending"]
    report = pulled(trk)
    assert report["comments"] == [] and report["replies"]["sent"] == [c]
    assert len(replies(fake, c)) == 1 and fake.comments[c]["status"] == "open"
    assert [r["state"] for r in outbox(trk_home)] == ["sent"]
    assert len([r for r in journal(trk_home) if r["change"] == "ask.replied"]) == 1


@pytest.mark.parametrize("where", ["box", "misplaced"])
def test_two_pulls_racing_send_exactly_one_reply(trk, trk_home, mode, asked, fake, monkeypatch, where):
    handle = f"ask-{asked['A']}" if where == "box" else "header"
    c = fake.add_comment(slug(trk_home, asked["D"]), handle, "Metres.", author_id="u-kim")
    started, release = threading.Event(), threading.Event()
    real = fake.call
    reading = "activity" if mode == "activity" else "list_comments"

    def slow(name, arguments, **kwargs):
        if name == reading and not started.is_set():
            started.set()
            release.wait(5)  # the first pull holds pages.lock here while the second one starts
        return real(name, arguments, **kwargs)

    monkeypatch.setattr(fake, "call", slow)
    reports, errors = [], []

    def run():
        st = store.open_store()
        try:
            from trk import pages

            reports.append(pull.pull(st, pages.client(st)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            st.close()

    first = threading.Thread(target=run)
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=run)
    second.start()
    second.join(0.5)
    assert second.is_alive()  # waiting for pages.lock
    release.set()
    first.join(10)
    second.join(10)
    assert errors == []
    assert len(fake.tool_calls("reply")) == 1 and len(replies(fake, c)) == 1
    assert sorted(len(r["comments"]) for r in reports) == [0, 1]


def test_the_disposition_check_runs_inside_its_own_transaction(trk, trk_home, asked, fake):
    """Two pulls that read the same comment (as if pages.lock were bypassed) dispose of it once."""
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    comment = fake._list_comments({"slug": slug(trk_home, asked["D"])})["comments"][0]
    first, second = store.open_store(), store.open_store()
    try:
        assert pull._dispose(first, asked["D"], slug(trk_home, asked["D"]), comment)["disposition"] == "reply"
        assert pull._dispose(second, asked["D"], slug(trk_home, asked["D"]), comment) is None
    finally:
        first.close()
        second.close()
    assert len(outbox(trk_home)) == 1 and dispositions(trk_home) == {c: ("reply", asked["A"])}


# ----- AC 4: only comments after the ask's latest opening count ------------------------------------


@TABLE
def test_a_comment_from_before_the_asks_opening_is_no_answer(trk, trk_home, asked, fake):
    a = asked["A"]
    old = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Metres.", author_id="u-kim",
                           created_at="2026-10-11T08:00:00.000Z")
    pulled(trk)
    assert dispositions(trk_home)[old] == ("ignored", a)
    assert item(trk, a)["status"] == "open" and fake.comments[old]["replies"] == []


@TABLE
def test_after_a_reopen_only_a_comment_after_it_is_a_reply(trk, trk_home, asked, fake, body, monkeypatch):
    a = asked["A"]
    assert trk("answer", a, "--outcome", "Metres", "--answer", body("Metres.\n"), "--as", "kim").code == 0
    monkeypatch.setenv("TRK_NOW", "2026-10-11T12:20:00Z")
    assert trk("reopen", a, "--reason", "need units", "--as", "worker").code == 0
    between = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Feet.", author_id="u-kim",
                               created_at="2026-10-11T12:10:00.000Z")
    after = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Millimetres.", author_id="u-kim",
                             created_at="2026-10-11T12:30:00.000Z")
    pulled(trk)
    assert dispositions(trk_home) == {between: ("already-answered", a), after: ("reply", a)}
    assert replies(fake, between) == [("Already answered by kim: Metres", "assistant")]
    assert [r["comment"] for r in item(trk, a)["replies"]] == [after]
    confirm(trk, a, after, outcome="Millimetres.")
    assert item(trk, a)["outcome"] == "Millimetres."


# ----- AC 5: the outbox's failures -----------------------------------------------------------------


def test_a_refused_reply_is_failed_and_recorded_on_the_page(trk, trk_home, asked, fake):
    d_slug = slug(trk_home, asked["D"])
    c = fake.add_comment(d_slug, f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    fake.grants.discard("reply")  # refused outright, not ambiguously
    report = pulled(trk)
    assert report["replies"]["failed"] == [c]
    assert [r["state"] for r in outbox(trk_home)] == ["failed"]
    assert replies(fake, c) == [] and fake.comments[c]["status"] == "open"
    assert "tool_not_allowed" in page_row(trk_home, asked["D"])["last_error"]
    assert pulled(trk)["replies"] == {"sent": [], "failed": [], "pending": []}  # failed rows are not resent


@TABLE
def test_an_ambiguous_reply_failure_reads_the_thread_before_sending_again(trk, trk_home, asked, fake):
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    fake.fail_next("502-after", tool="reply")  # the reply landed, the answer was lost
    report = pulled(trk)
    assert report["replies"]["pending"] == [c] and [r["state"] for r in outbox(trk_home)] == ["pending"]
    assert len(replies(fake, c)) == 1
    fake.calls.clear()
    report = pulled(trk)
    assert report["replies"]["sent"] == [c]
    assert fake.tool_calls("reply") == [] and len(fake.tool_calls("list_comments")) >= 1
    assert len(replies(fake, c)) == 1 and [r["state"] for r in outbox(trk_home)] == ["sent"]


@TABLE
def test_a_reply_lost_before_the_origin_saw_it_is_sent_again(trk, trk_home, asked, fake):
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    fake.fail_next("502", tool="reply")
    assert pulled(trk)["replies"]["pending"] == [c]
    assert fake.comments[c]["replies"] == []
    assert pulled(trk)["replies"]["sent"] == [c]
    assert len(replies(fake, c)) == 1 and fake.comments[c]["status"] == "open"


@TABLE
def test_a_pending_reply_whose_page_no_longer_shows_an_ask_reads_that_page(trk, trk_home, asked, fake, body):
    a, kim_slug = asked["A"], slug(trk_home, "needs-you:kim")
    assert trk("answer", a, "--outcome", "Metres", "--answer", body("Metres.\n"), "--as", "kim").code == 0
    update, sneaked = fake._update, []

    def comment_first(arguments):  # after the last pull-before-drop's listing: just before kim's page drops the box
        if arguments["slug"] == kim_slug and not sneaked:
            sneaked.append(fake.add_comment(kim_slug, f"ask-{a}", "Metres, yes.", author_id="u-kim"))
        return update(arguments)

    fake._update = comment_first
    flush(trk)  # the answered ask leaves every page
    (c,) = sneaked
    fake.fail_next("502", tool="reply")
    assert pulled(trk)["replies"]["pending"] == [c]  # listed once more after its drop; the reply is lost
    assert pull.pages_to_read(db(trk_home)) == []
    fake.calls.clear()
    assert pulled(trk)["replies"]["sent"] == [c, c]  # the lost reply, then the closing reply that follows it
    assert fake.tool_calls("list_comments") == [{"slug": kim_slug}]
    assert [m.split(".")[0] for m, _ in replies(fake, c)] == ["Already answered by kim: Metres", "Closed: Metres"]
    assert fake.comments[c]["status"] == "resolved"


# ----- reading, the token, the CLI -------------------------------------------------------------------


def test_the_pull_reads_the_pages_that_show_or_showed_an_ask(trk, trk_home, asked, fake):
    pulled(trk)
    listed = [args["slug"] for args in fake.tool_calls("list_comments")]
    # Not T, which never showed one, nor michael's Needs-you page: the ask waits on kim.
    assert listed == [slug(trk_home, asked[k]) for k in ("P", "J", "D")] + [slug(trk_home, "needs-you:kim")]


def test_a_listing_failure_is_recorded_on_its_page_and_cleared_by_the_next(trk, trk_home, asked, fake):
    fake.fail_next("500", tool="list_comments")
    report = pulled(trk)
    assert [e["item"] for e in report["errors"]] == [asked["P"]]
    assert page_row(trk_home, asked["P"])["last_error"].startswith("list_comments:")
    pulled(trk)
    assert page_row(trk_home, asked["P"])["last_error"] is None


def test_a_page_whose_comments_cant_be_read_still_flushes_on_time(trk, trk_home, asked, fake, monkeypatch):
    """A listing that fails never pauses the page's flush (audit of ledger-7): the tick after D falls due
    publishes it although every listing in that tick fails."""
    assert trk("edit", asked["D"], "--title", "D renamed").code == 0  # at 09:00, so D is due at 09:01
    monkeypatch.setenv("TRK_NOW", "2026-10-11T09:02:00Z")
    fake.fail_next("500", tool="list_comments", times=100)  # every attempt, retries included
    result = trk("pages", "tick", "--json")
    assert result.code == 0, result.err
    report = result.json()
    assert asked["D"] in [e["item"] for e in report["pull"]["errors"]]
    assert asked["D"] in report["updated"]
    row = page_row(trk_home, asked["D"])
    assert row["due_at"] is None and row["last_error_at"] is None
    assert "D renamed" in fake.body(slug(trk_home, asked["D"]))


def test_a_pull_error_does_not_hide_a_dirty_page_from_the_flusher(trk, trk_home, asked, fake):
    from trk import flusher

    assert trk("edit", asked["D"], "--title", "D renamed").code == 0
    trk("pages", "flush", "--json")  # marks D, not due yet
    fake.fail_next("500", tool="list_comments", times=3)
    pulled(trk)
    assert page_row(trk_home, asked["D"])["last_error"].startswith("list_comments:")
    conn = db(trk_home)
    try:
        assert flusher.next_due(conn) is not None  # flush --wait still sleeps until D falls due
    finally:
        conn.close()


def test_a_401_holds_the_pull_and_opens_the_renewal_ask(trk, trk_home, asked, fake):
    fake.token = "tok_" + "n" * 40
    report = pulled(trk)
    assert report["held"] is True and report["renewal_ask"]
    fake.requests.clear()
    again = pulled(trk)
    assert again["held"] is True and fake.requests == []  # held: no call at all


def test_the_pull_starts_with_the_expiry_check(trk, trk_home, asked, fake, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2027-09-20T09:00:00Z")
    report = pulled(trk)
    assert report["held"] is False and report["renewal_ask"]


@TABLE
def test_tick_pulls_then_flushes(trk, trk_home, asked, fake):
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    result = trk("pages", "tick")
    assert result.code == 0, result.err
    assert f"comment {c}" in result.out and "reply" in result.out
    assert item(trk, asked["A"])["status"] == "open" and item(trk, asked["A"])["replies"]


@TABLE
def test_pull_prints_what_it_did(trk, trk_home, asked, fake):
    c = fake.add_comment(slug(trk_home, asked["D"]), f"ask-{asked['A']}", "Metres.", author_id="u-kim")
    result = trk("pages", "pull")
    assert result.code == 0, result.err
    assert f"comment {c} on {slug(trk_home, asked['D'])} (ask-{asked['A']}): reply {asked['A']}" in result.out
    assert f"replies sent: {c}" in result.out


# ----- the commenter: actor link and pages authors ---------------------------------------------------


def test_actor_link_journals_actor_updated_and_relinking_moves_the_id(trk, trk_home, tree):
    since = journal(trk_home)[-1]["seq"]
    assert trk("actor", "link", "kim", "--service-user", "u-kim").code == 0
    assert trk("actor", "link", "kim", "--service-user", "u-kim").code == 0  # unchanged: nothing journaled
    assert trk("actor", "link", "michael", "--service-user", "u-kim").code == 0
    rows = journal(trk_home, since)
    assert [(r["change"], json.loads(r["data"])["id"], json.loads(r["data"])["fields"]) for r in rows] == [
        ("actor.updated", "kim", ["service_user"]), ("actor.updated", "kim", ["service_user"]),
        ("actor.updated", "michael", ["service_user"])]
    assert actor_row(trk_home, "kim")["service_user_id"] is None
    assert actor_row(trk_home, "michael")["service_user_id"] == "u-kim"
    assert "u-kim" not in trk("actors", "--json").out  # the id stays internal


def test_actor_link_refuses_an_unknown_actor_and_a_blank_id(trk, tree):
    assert trk("actor", "link", "nobody", "--service-user", "u-1").code == 3
    assert trk("actor", "link", "kim", "--service-user", " ").code == 2


@TABLE
def test_pages_authors_lists_every_author_seen_and_its_actor(trk, trk_home, asked, fake):
    a = asked["A"]
    fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Metres.", author="Kim Reader", author_id="u-kim")
    fake.add_comment(slug(trk_home, asked["D"]), "header", "Hi", author="Sam Smith", author_id="u-sam")
    fake.add_comment(slug(trk_home, asked["D"]), f"ask-{a}", "Feet", author="Lee Park", author_id="u-new")
    pulled(trk)
    assert trk("pages", "authors", "--json").json() == {"schema": 1, "authors": [
        {"author_id": "u-kim", "display": "Kim Reader", "actor": "kim"},
        {"author_id": "u-new", "display": "Lee Park", "actor": "lee-park"},
        {"author_id": "u-sam", "display": "Sam Smith", "actor": "sam-smith"}]}
    out = trk("pages", "authors").out
    assert "u-sam  Sam Smith  sam-smith" in out and "u-kim  Kim Reader  kim" in out
