"""Needs-you pages (Page publisher spec: Needs-you pages; ticket tr-n77bw): one page per person, everything
waiting on them across every root, against the in-process fake page backend. No real backend is ever
called."""

import json
import re
import threading

import pytest

from fake_backend import TOKEN
from gated import DEST
from test_pages_flush import db, flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_pull import RECEIVED, TABLE, body, confirm, dispositions, mode, pulled, replies, slug  # noqa: F401
from test_pages_setup import ME, fake, journal, setup_argv, token_file, tree  # noqa: F401 (fixtures)
from trk import needs_you, pull, store

KIM = "needs-you:kim"


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def asks_on(page_body):
    """The asks whose box a page shows, in page order."""
    return re.findall(r'data-comment-id="ask-([^"]+)"', page_body)


def blocked_on(page_body):
    return re.findall(r'data-comment-id="blocked-([^"]+)"', page_body)


def box(page_body, ask_id):
    match = re.search(rf'<div class="ask[^"]*" data-comment-id="ask-{re.escape(ask_id)}">.*?'
                      r'<p class="cta">Answer by commenting on this box</p>\n</div>', page_body, re.S)
    return match.group(0)


def page(fake, trk_home, key):
    return fake.body(slug(trk_home, key))


def waiting_ids(trk, actor):
    return [a["id"] for a in json.loads(ok(trk, "waiting", "--me", actor, "--json"))["asks"]]


@pytest.fixture
def two_roots(trk, trk_home, live, fake, body):
    """The acceptance store plus a second program Q, `engineering` held by michael, and asks under both
    roots: A1 on D and A2 (urgent) on Q to engineering, A4 on Q to parent (Q's own owner, michael), and A3
    on T to product (kim). Every page is published."""
    assert trk("actor", "link", "kim", "--service-user", "u-kim").code == 0
    assert trk("actor", "link", "michael", "--service-user", "u-mf").code == 0
    ok(trk, "roles", "set", "engineering", "michael")
    q = ok(trk, "new", "program", "Q", *DEST)

    def ask(parent, role, title, *extra):
        result = trk("ask", parent, "--to", role, title, "--question", body(f"{title}?\n", "q.md"), "--as", "worker",
                     "--json", *extra)
        assert result.code == 0, result.err
        return result.json()["item"]["id"]

    found = {**live, "Q": q, "A1": ask(live["D"], "engineering", "Which unit?"),
             "A2": ask(q, "engineering", "Ship it?", "--urgent"), "A3": ask(live["T"], "product", "Which colour?"),
             "A4": ask(q, "parent", "Which name?")}
    flush(trk)
    fake.calls.clear()
    return found


# ----- AC 1: everything waiting on michael, across roots, in `trk waiting --me` order ---------------------


def test_ac1_michaels_page_lists_what_waits_on_him_across_roots_in_waiting_order(trk, trk_home, two_roots, fake):
    body_ = page(fake, trk_home, ME)
    shown = asks_on(body_)
    assert shown == waiting_ids(trk, "michael")
    assert shown[0] == two_roots["A2"] and sorted(shown[1:]) == sorted(two_roots[k] for k in ("A1", "A4"))  # urgent first
    assert two_roots["A3"] not in asks_on(body_)  # kim's
    for key in ("A1", "A2", "A4"):  # the very box its program's page shows, rendered from the same store
        program = ok(trk, "pages", "render", two_roots["P" if key == "A1" else "Q"], "--out", "-")
        assert box(body_, two_roots[key]) == box(program, two_roots[key])
    # Each ask's root program sits beside its box, linking that program's page.
    for key, root in (("A1", "P"), ("A2", "Q")):
        before = body_[:body_.index(f'data-comment-id="ask-{two_roots[key]}"')]
        assert before.rstrip().rsplit("\n", 2)[-2].startswith(
            f'<p class="root">Program: <a href="{page_row(trk_home, two_roots[root])["url"]}">')
    assert "Needs you: michael" in body_ and 'data-comment-id="waiting"' in body_


def test_ac1_the_page_is_born_shared_with_its_own_tag_and_journals_no_page_created(trk, trk_home, live, fake):
    since = journal(trk_home)[-1]["seq"]
    flush(trk)
    (published,) = [args for args in fake.tool_calls("publish") if f"trk:{ME}" in args["tags"]]
    assert published["team"] == "tracker" and published["title"] == "Needs you: michael"
    assert [r["item"] for r in journal(trk_home, since) if r["change"] == "page.created"] == \
        [live[k] for k in ("P", "J", "D", "T")]
    assert page(fake, trk_home, ME) == ok(trk, "pages", "render", ME, "--out", "-") + "\n"
    assert trk("pages", "render", "needs-you:nobody").code == 3


def test_ac1_kim_gets_their_own_page_and_agents_get_none(trk, trk_home, two_roots, fake):
    assert asks_on(page(fake, trk_home, KIM)) == waiting_ids(trk, "kim") == [two_roots["A3"]]
    keys = [row["item"] for row in trk("pages", "--json").json()["pages"] if row["kind"] == "needs-you"]
    assert keys == [KIM, ME]  # worker, an agent, has none; the publisher agent neither


# ----- AC 2: a comment on the Needs-you page answers, once, wherever else the box is -------------------


@TABLE
def test_ac2_a_comment_on_the_needs_you_box_is_a_reply_the_asker_confirms(trk, trk_home, two_roots, fake):
    a1 = two_roots["A1"]
    c = fake.add_comment(slug(trk_home, ME), f"ask-{a1}", "Metres.", author="Michael Fork", author_id="u-mf")
    report = trk("pages", "tick", "--json").json()
    assert [(e["comment"], e["disposition"], e["ask"]) for e in report["pull"]["comments"]] == [(c, "reply", a1)]
    flush(trk)
    assert "awaiting confirmation" in box(page(fake, trk_home, ME), a1)
    confirm(trk, a1, c)
    trk("pages", "tick")
    (answered,) = [r for r in journal(trk_home) if r["change"] == "ask.answered"]
    assert answered["actor"] == "michael" and json.loads(answered["data"])["slug"] == slug(trk_home, ME)
    assert replies(fake, c) == [(RECEIVED, "assistant"),
                                ("Recorded as the answer to Which unit?, by michael (for michael). Ask closed.",
                                 "assistant")]
    assert fake.comments[c]["status"] == "open"
    flush(trk)
    for key in (ME, two_roots["D"], two_roots["J"], two_roots["P"]):
        assert a1 not in asks_on(page(fake, trk_home, key))


@TABLE
@pytest.mark.parametrize("first_on", ["needs-you", "program"])
def test_ac2_with_comments_on_both_pages_each_is_a_reply_once_and_a_later_one_is_told(trk, trk_home, two_roots,
                                                                                      fake, first_on):
    a1 = two_roots["A1"]
    slugs = [slug(trk_home, ME), slug(trk_home, two_roots["P"])]
    if first_on == "program":
        slugs.reverse()
    first = fake.add_comment(slugs[0], f"ask-{a1}", "Metres.", author="Michael Fork", author_id="u-mf")
    second = fake.add_comment(slugs[1], f"ask-{a1}", "Feet.", author="Kim Reader", author_id="u-kim")
    trk("pages", "tick")
    assert dispositions(trk_home) == {first: ("reply", a1), second: ("reply", a1)}
    assert len([r for r in journal(trk_home) if r["change"] == "ask.replied"]) == 2
    confirm(trk, a1, first)
    third = fake.add_comment(slugs[1], f"ask-{a1}", "Yards.", author="Kim Reader", author_id="u-kim")
    trk("pages", "tick")
    assert replies(fake, third) == [("Already answered by michael: Metres.", "assistant")]  # the first is his
    assert replies(fake, second)[-1] == ("Ask closed: michael's reply was recorded as the answer.", "assistant")
    assert len(fake.tool_calls("reply")) == 5  # 2 received, recorded, closed, already answered


def test_ac2_two_ticks_racing_over_both_pages_reply_once_each(trk, trk_home, mode, two_roots, fake, monkeypatch):
    """The live tick loop and a hand-run tick at once: pages.lock and `page_comments` keep it to one answer."""
    a1 = two_roots["A1"]
    on_me = fake.add_comment(slug(trk_home, ME), f"ask-{a1}", "Metres.", author_id="u-mf")
    on_p = fake.add_comment(slug(trk_home, two_roots["P"]), f"ask-{a1}", "Feet.", author_id="u-kim")
    started, release = threading.Event(), threading.Event()
    real = fake.call
    reading = "activity" if mode == "activity" else "list_comments"

    def slow(name, arguments, **kwargs):
        if name == reading and not started.is_set():
            started.set()
            release.wait(5)  # the first pull holds pages.lock here while the second one starts
        return real(name, arguments, **kwargs)

    monkeypatch.setattr(fake, "call", slow)
    monkeypatch.setenv("TRK_ARTIFACT_TOKEN", TOKEN)
    reports, errors = [], []

    def run():
        st = store.open_store()
        try:
            from trk import pages

            reports.append(pull.pull(st, pages.client(st, foreground=True)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            st.close()

    first = threading.Thread(target=run)
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=run)
    second.start()
    release.set()
    first.join(10)
    second.join(10)
    assert errors == []
    assert len([r for r in journal(trk_home) if r["change"] == "ask.replied"]) == 2
    assert dispositions(trk_home) == {on_me: ("reply", a1), on_p: ("reply", a1)}
    assert len(replies(fake, on_me)) == 1 and len(replies(fake, on_p)) == 1
    assert sorted(len(r["comments"]) for r in reports) == [0, 2]


@TABLE
def test_ac2_a_comment_on_the_needs_you_waiting_box_takes_its_one_ask_or_asks_which(trk, trk_home, two_roots, fake):
    which = fake.add_comment(slug(trk_home, ME), "waiting", "Yes.", author_id="u-mf")  # three asks wait on michael
    only = fake.add_comment(slug(trk_home, KIM), "waiting", "Blue.", author_id="u-kim")  # one waits on kim
    trk("pages", "tick")
    assert dispositions(trk_home) == {which: ("which-ask", None), only: ("reply", two_roots["A3"])}
    assert replies(fake, which) == [("Which ask? Comment on its box instead.", "assistant")]


@TABLE
def test_ac2_a_waiting_comment_counts_the_box_as_it_stood_when_it_was_written(trk, trk_home, two_roots, fake,
                                                                             monkeypatch):
    """kim's box held one ask (A3) when the comment was written; readdressing A1 to kim afterwards, before the
    tick, does not make it two."""
    c = fake.add_comment(slug(trk_home, KIM), "waiting", "Blue.", author_id="u-kim",
                         created_at="2026-10-11T10:00:00.000Z")
    monkeypatch.setenv("TRK_NOW", "2026-10-11T10:05:00Z")
    ok(trk, "readdress", two_roots["A1"], "--to", "product", "--as", "worker")
    trk("pages", "tick")
    assert dispositions(trk_home)[c] == ("reply", two_roots["A3"])
    assert sorted(waiting_ids(trk, "kim")) == sorted([two_roots["A1"], two_roots["A3"]])


@TABLE
def test_a_comment_off_a_box_on_a_needs_you_page_is_routed_to_its_person(trk, trk_home, two_roots, fake):
    c = fake.add_comment(slug(trk_home, KIM), "header", "Blue.", author_id="u-kim")  # one ask (A3) waits on kim
    trk("pages", "tick")
    assert dispositions(trk_home)[c] == ("routed", None)
    assert replies(fake, c) == [("Sent to kim; they'll reply here.", "assistant")]
    assert "<h2>Waiting on you</h2>" in page(fake, trk_home, KIM)


# ----- AC 3: blocked on michael -----------------------------------------------------------------------


def test_ac3_items_blocked_by_what_michael_holds_are_listed_and_linked(trk, trk_home, two_roots, fake, body):
    d, t, j = two_roots["D"], two_roots["T"], two_roots["J"]
    sections = ("--section", f"current-behaviour={body('x\n', 'c.md')}", "--section",
                f"desired-behaviour={body('y\n', 'd.md')}", "--criterion", "It works.")
    t2 = ok(trk, "new", "work-item", "T2", "--form", "self-contained", "--parent", j, "--owner", "kim", *sections)
    t3 = ok(trk, "new", "work-item", "T3", "--form", "self-contained", "--parent", j, *sections)
    ok(trk, "link", d, "blocks", t3)   # an open decision michael owns (inherited) blocks T3
    ok(trk, "link", t2, "blocks", t3)  # kim's ticket blocks it too, but that is kim's, not michael's
    flush(trk)
    mine, kims = page(fake, trk_home, ME), page(fake, trk_home, KIM)
    # D waits on A1 (michael's), Q on A2 and A4, T3 on D; T waits on A3, which is kim's.
    assert blocked_on(mine) == [d, t3, two_roots["Q"]]
    assert blocked_on(kims) == [t, t3]  # A3 on T, and kim's own ticket T2 on T3
    row = re.search(rf'<tr data-comment-id="blocked-{t3}">.*?</tr>', mine).group(0)
    assert f'<a href="{page_row(trk_home, t3)["url"]}">T3</a>' in row
    assert f'<a href="{page_row(trk_home, d)["url"]}">D</a>' in row and "T2" not in row
    assert f'<a href="{page_row(trk_home, two_roots["P"])["url"]}">P</a>' in row  # its program


def test_ac3_a_closed_blocker_takes_its_row_away(trk, trk_home, two_roots, fake, body):
    ok(trk, "answer", two_roots["A2"], "--outcome", "Yes", "--answer", body("Yes.\n"))
    ok(trk, "answer", two_roots["A4"], "--outcome", "Q2", "--answer", body("Q2.\n"))
    flush(trk)
    assert two_roots["Q"] not in blocked_on(page(fake, trk_home, ME))


# ----- AC 4: readdressing, role holders and closes reach the page; an empty page stays -----------------


def test_ac4_readdressing_moves_the_ask_between_pages_on_the_next_flush(trk, trk_home, two_roots, fake):
    ok(trk, "readdress", two_roots["A1"], "--to", "product", "--as", "worker")
    flush(trk)
    assert two_roots["A1"] not in asks_on(page(fake, trk_home, ME))
    assert asks_on(page(fake, trk_home, KIM)) == waiting_ids(trk, "kim")
    assert two_roots["A1"] in waiting_ids(trk, "kim")


def test_ac4_a_new_role_holder_takes_the_asks_on_the_next_flush(trk, trk_home, two_roots, fake):
    ok(trk, "roles", "set", "engineering", "kim")
    flush(trk)
    assert asks_on(page(fake, trk_home, ME)) == waiting_ids(trk, "michael") == [two_roots["A4"]]
    assert asks_on(page(fake, trk_home, KIM)) == waiting_ids(trk, "kim")
    assert len(waiting_ids(trk, "kim")) == 3


def test_ac4_an_empty_page_says_nothing_waits_and_stays_published(trk, trk_home, two_roots, fake, body):
    before = page_row(trk_home, KIM)
    ok(trk, "answer", two_roots["A3"], "--outcome", "Blue", "--answer", body("Blue.\n"), "--as", "kim")
    flush(trk)
    after = page_row(trk_home, KIM)
    assert (after["slug"], after["url"]) == (before["slug"], before["url"]) and after["version"] > before["version"]
    kims = page(fake, trk_home, KIM)
    assert asks_on(kims) == [] and "Nothing is waiting on you" in kims
    assert after["due_at"] is None
    assert KIM in [row["item"] for row in trk("pages", "--json").json()["pages"]]


def test_a_write_no_person_page_shows_marks_none_of_them(trk, trk_home, two_roots, fake, body):
    ok(trk, "section", "set", two_roots["T"], "what-to-build", "--file", body("Changed.\n")[1:])
    flush(trk)
    sent = {args["slug"] for args in fake.tool_calls("update")}
    assert slug(trk_home, two_roots["T"]) in sent and not sent & {slug(trk_home, ME), slug(trk_home, KIM)}
    conn = db(trk_home)
    try:  # not even marked: rendering them would have found nothing to send
        assert conn.execute("SELECT count(*) FROM pages WHERE item_id LIKE 'needs-you:%' AND due_at IS NOT NULL"
                            ).fetchone()[0] == 0
    finally:
        conn.close()


def test_the_people_with_a_page(trk, trk_home, two_roots):
    conn = db(trk_home)
    try:
        assert needs_you.people(conn) == ["kim", "michael"]
    finally:
        conn.close()


# ----- tr-6nghv: a page's own ask boxes link the page itself after its first publish -------------------


def test_a_first_publish_links_the_pages_own_ask_boxes(trk, trk_home, two_roots, fake):
    """A2 sits directly under Q, so its box ends "On: Q": after the flush that first published Q's page, the
    box links Q's own page there, byte-identical to the box on every other page that shows it, and a second
    flush writes nothing."""
    a2, q = two_roots["A2"], two_roots["Q"]
    conn = db(trk_home)
    try:
        slugs = [row["slug"] for row in conn.execute("SELECT slug FROM pages WHERE slug IS NOT NULL")]
    finally:
        conn.close()
    boxes = {s: box(fake.body(s), a2) for s in slugs if f'data-comment-id="ask-{a2}"' in fake.body(s)}
    assert {slug(trk_home, q), slug(trk_home, ME)} <= set(boxes)
    assert f'href="{page_row(trk_home, q)["url"]}"' in boxes[slug(trk_home, q)]
    assert len(set(boxes.values())) == 1, boxes
    report = flush(trk)
    assert (report["published"], report["updated"]) == ([], []) and fake.tool_calls("update") == []


# ----- tr-p9mpr: the pages that show a person's asks link their Needs-you page --------------------------


def handles_on(page_body):
    return set(re.findall(r'data-comment-id="([^"]+)"', page_body))


def test_every_waiting_box_links_its_addressees_needs_you_page(trk, trk_home, two_roots, fake):
    mine, kims = page_row(trk_home, ME)["url"], page_row(trk_home, KIM)["url"]
    for key in ("P", "J", "D"):
        assert f'engineering (<a href="{mine}">michael</a>)' in box(page(fake, trk_home, two_roots[key]), two_roots["A1"])
    assert f'product (<a href="{kims}">kim</a>)' in box(page(fake, trk_home, two_roots["T"]), two_roots["A3"])
    for ask in ("A1", "A2", "A3", "A4"):  # the same element on every page that shows it
        pages_ = [two_roots[k] for k in ("P", "J", "D", "T", "Q")] + [ME, KIM]
        showing = [s for s in (slug(trk_home, k) for k in pages_)
                   if f'data-comment-id="ask-{two_roots[ask]}"' in fake.body(s)]
        assert len(showing) >= 2 and len({box(fake.body(s), two_roots[ask]) for s in showing}) == 1, ask
    report = flush(trk)
    assert (report["published"], report["updated"]) == ([], []) and fake.tool_calls("update") == []


def test_an_addressee_without_a_published_page_is_plain_text(trk, live, body):
    a1 = trk("ask", live["D"], "--to", "product", "Which unit?", "--question", body("Which unit?\n", "q.md"),
             "--as", "worker", "--json").json()["item"]["id"]
    html = ok(trk, "pages", "render", live["D"], "--out", "-")
    assert "product (kim)" in box(html, a1)
    assert "Needs you" not in html


def test_the_top_level_page_links_every_published_needs_you_page(trk, trk_home, two_roots, fake):
    links = (f'<dt>Needs you</dt><dd><a href="{page_row(trk_home, KIM)["url"]}">kim</a>, '
             f'<a href="{page_row(trk_home, ME)["url"]}">michael</a></dd>')
    for root in ("P", "Q"):
        assert links in page(fake, trk_home, two_roots[root])
    for below in ("J", "D", "T"):
        assert "Needs you" not in page(fake, trk_home, two_roots[below])


def test_a_needs_you_pages_first_publish_marks_the_pages_that_link_it(trk, trk_home, two_roots, fake, body):
    """eve's page is first published by the flush that publishes her program R; nothing else in that flush
    marks P, yet P's header links her page afterwards, by a patch of `header` alone, with P's handles
    unchanged, and a second flush writes nothing."""
    p = two_roots["P"]
    before = handles_on(page(fake, trk_home, p))
    ok(trk, "actor", "add", "eve", "--kind", "human")
    r = ok(trk, "new", "program", "R", "--owner", "eve", *DEST)
    a5 = trk("ask", r, "--to", "parent", "Which day?", "--question", body("Which day?\n", "q.md"), "--as", "worker",
             "--json").json()["item"]["id"]
    flush(trk)
    eve = page_row(trk_home, "needs-you:eve")["url"]
    assert eve and f'<a href="{eve}">eve</a>' in page(fake, trk_home, p)
    assert handles_on(page(fake, trk_home, p)) == before
    sent = [args for args in fake.tool_calls("update") if args["slug"] == slug(trk_home, p)]
    assert [[edit["section"] for edit in args["edits"]] for args in sent] == [["header"]]
    assert box(page(fake, trk_home, r), a5) == box(page(fake, trk_home, "needs-you:eve"), a5)
    assert f'parent (<a href="{eve}">eve</a>)' in box(page(fake, trk_home, r), a5)
    fake.calls.clear()
    report = flush(trk)
    assert (report["published"], report["updated"]) == ([], []) and fake.tool_calls("update") == []


@TABLE
def test_a_page_comment_reply_has_no_body_and_bumps_rev(trk, trk_home, two_roots, fake):
    a1 = two_roots["A1"]
    before = trk("show", a1, "--json").json()["item"]["rev"]
    fake.add_comment(slug(trk_home, ME), f"ask-{a1}", "Metres.", author="Michael Fork", author_id="u-mf")
    trk("pages", "tick")
    shown = trk("show", a1, "--json").json()["item"]
    [reply] = shown["replies"]
    assert reply["body"] is None and reply["excerpt"] == "Metres."
    assert shown["rev"] > before and shown["awaiting"] == "asker"
