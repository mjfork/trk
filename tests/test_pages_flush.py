"""First publish: `trk pages flush --now` (Page publisher spec: First publish, Size, Amendments to Core's
item JSON `page` field, the `publishing` state and recovery by tag), against the in-process fake
page backend. No real backend is ever called."""

import json
import re
import sqlite3

import pytest

from fake_backend import MAX_BODY_BYTES
from test_pages_setup import ME, fake, journal, setup_argv, token_file, tree  # noqa: F401 (fixtures)
from trk import render, store
from trk.top import KEY as TOP


@pytest.fixture
def live(trk, trk_home, tree, fake, token_file):
    """The acceptance store, with publishing set up against the fake."""
    result = trk(*setup_argv(token_file, tree["P"]))
    assert result.code == 0, result.err
    fake.requests.clear()
    fake.calls.clear()
    return tree


def flush(trk, *extra):
    result = trk("pages", "flush", "--now", "--json", *extra)
    assert result.code == 0, result.err
    return result.json()


def db(trk_home):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    return conn


def page_row(trk_home, item_id):
    conn = db(trk_home)
    try:
        return conn.execute("SELECT * FROM pages WHERE item_id = ?", (item_id,)).fetchone()
    finally:
        conn.close()


def tag_of(args):
    return next(tag for tag in args["tags"] if tag.startswith("trk:"))


def hrefs(body):
    return re.findall(r'href="([^"]+)"', body)


# ----- AC 2: one flush publishes the subtree top-down and links it up -------------------------------


def test_one_flush_publishes_every_page_top_down_shared_on_the_team(trk, trk_home, live, fake):
    since = journal(trk_home)[-1]["seq"]
    flush(trk)
    published = fake.tool_calls("publish")
    # The top-level page goes first: every page published after it is born linking it (spec 04).
    assert [tag_of(args) for args in published] == \
        [f"trk:{TOP}"] + [f"trk:{live[k]}" for k in ("P", "J", "D", "T")] + [f"trk:{ME}"]
    assert all(args["team"] == "tracker" for args in published)
    assert all(args["tags"] == [tag_of(args)] for args in published)
    created = [row for row in journal(trk_home, since) if row["change"] == "page.created"]
    assert [row["item"] for row in created] == [live[k] for k in ("P", "J", "D", "T")]
    assert all(row["actor"] == "publisher" for row in created)
    for row in created:
        page = page_row(trk_home, row["item"])
        assert json.loads(row["data"]) == {"slug": page["slug"], "url": page["url"]}
        assert fake.artifacts[page["slug"]]["team"] == "tracker"


def test_the_same_run_patches_the_parents_so_every_link_resolves(trk, trk_home, live, fake):
    flush(trk)
    urls = {key: page_row(trk_home, live[key])["url"] for key in live}
    slugs = {key: page_row(trk_home, live[key])["slug"] for key in live}
    # P and J were patched once their children existed; D and T were born linked to their ancestors.
    assert {key: len(fake.artifacts[slugs[key]]["versions"]) for key in live} == {"P": 2, "J": 2, "D": 1, "T": 1}
    assert all("team" not in args for args in fake.tool_calls("update"))
    urls["me"] = page_row(trk_home, ME)["url"]  # a root item's page links michael's Needs-you page
    urls["top"] = page_row(trk_home, TOP)["url"]  # and every page's trail links the top-level page
    every_url = set(urls.values())
    for key, children in (("P", ["J", "me"]), ("J", ["D", "T"]), ("D", ["P", "J"]), ("T", ["P", "J"])):
        body = fake.body(slugs[key])
        links = [href for href in hrefs(body) if href.startswith(fake.base_url)]
        assert {urls[k] for k in children} <= set(links), key
        assert set(links) <= every_url, key
        assert body == render.render(db(trk_home), live[key])


def test_show_carries_the_page_and_a_second_flush_writes_nothing(trk, trk_home, live, fake):
    flush(trk)
    page = trk("show", live["T"], "--json").json()["item"]["page"]
    row = page_row(trk_home, live["T"])
    assert page == {"url": row["url"], "version": 1, "published_at": row["published_at"], "dirty": False}
    assert page["url"] == fake.url(row["slug"])
    fake.calls.clear()
    head = journal(trk_home)[-1]["seq"]
    again = flush(trk)
    assert fake.tool_calls("publish") == [] and fake.tool_calls("update") == []
    assert journal(trk_home)[-1]["seq"] == head
    assert again["published"] == [] and again["updated"] == []


def test_flush_reports_what_it_did(trk, live, fake):
    report = flush(trk)
    assert report["schema"] == 1
    assert report["published"] == [TOP] + [live[k] for k in ("P", "J", "D", "T")] + [ME]
    assert report["updated"] == [TOP, live["P"], live["J"]]  # the top page's roots links resolve too
    assert report["failed"] == []


def test_item_flag_limits_the_flush(trk, trk_home, live, fake):
    flush(trk, "--item", live["P"])
    assert [tag_of(args) for args in fake.tool_calls("publish")] == [f"trk:{live['P']}"]
    assert page_row(trk_home, live["J"]) is None


def test_flush_without_now_publishes_new_pages(trk, trk_home, live, fake):
    result = trk("pages", "flush")
    assert result.code == 0, result.err
    assert len(fake.tool_calls("publish")) == 6  # the top-level page, four items, michael's Needs-you page


def test_flush_wait_refuses_now(trk, live, fake):
    result = trk("pages", "flush", "--wait", "--now", "--json")
    assert result.code == 2
    assert fake.requests == []


# ----- AC 5: the item JSON `page` field ---------------------------------------------------------------


def test_item_json_has_a_null_page_before_the_first_publish(trk, live):
    item = trk("show", live["T"], "--json").json()["item"]
    assert "page" in item and item["page"] is None


def test_every_item_read_carries_the_page_and_asks_are_null(trk, trk_home, live, fake):
    from gated import section_file

    flush(trk)
    result = trk("claim", live["D"], "--session", "s-1", "--as", "worker", "--json")
    assert result.code == 0, result.err
    assert result.json()["claimed"]["page"]["url"] == page_row(trk_home, live["D"])["url"]
    asked = trk("ask", live["D"], "--to", "product", "Q?", "--question", f"@{section_file('q', 'Which?\n')}",
                "--as", "worker")
    assert asked.code == 0, asked.err
    ask = asked.out.strip()
    tree_items = {item["id"]: item for item in trk("tree", "--json").json()["items"]}
    assert tree_items[live["P"]]["page"]["url"] == page_row(trk_home, live["P"])["url"]
    assert tree_items[ask]["page"] is None
    assert trk("show", ask, "--json").json()["item"]["page"] is None
    ready = trk("ready", "--json").json()["items"]
    assert ready and all(item["page"]["url"] == page_row(trk_home, item["id"])["url"] for item in ready)
    waiting = trk("waiting", "--all", "--json").json()["asks"]
    assert [a["page"] for a in waiting] == [None]


def test_a_dirty_page_says_so_in_item_json(trk, trk_home, live, fake):
    flush(trk)
    conn = db(trk_home)
    with conn:
        conn.execute("UPDATE pages SET due_at = '2026-10-11T09:01:00.000Z' WHERE item_id = ?", (live["T"],))
    conn.close()
    assert trk("show", live["T"], "--json").json()["item"]["page"]["dirty"] is True


# ----- AC 2 (spec AC 10): an ambiguous 502 never leaves a duplicate ----------------------------------


def test_a_502_after_the_publish_was_applied_adopts_the_page_by_its_tag(trk, trk_home, live, fake):
    fake.fail_next("502-after", tool="publish")
    report = flush(trk)
    tags = [tag for art in fake.artifacts.values() for tag in art["tags"]]
    assert sorted(tags) == sorted([f"trk:{live[k]}" for k in live] + [f"trk:{ME}", f"trk:{TOP}"])  # one each
    assert len(fake.tool_calls("find_tagged")) >= 1
    row = page_row(trk_home, live["P"])
    assert row["state"] == "live" and row["slug"] in fake.artifacts
    assert fake.artifacts[row["slug"]]["tags"] == [f"trk:{live['P']}"]
    assert report["failed"] == []
    created = [r["item"] for r in journal(trk_home) if r["change"] == "page.created"]
    assert sorted(created) == sorted(live.values())  # journaled once per page
    assert flush(trk)["updated"] == []


def test_a_later_run_that_finds_a_publishing_row_recovers_the_page(trk, trk_home, live, fake):
    # The first run's publish reached the origin but the run died before its bookkeeping; the
    # tracker only knows it had started (state publishing, no slug).
    fake.fail_next("connection", tool="find_tagged", times=99)
    fake.fail_next("502-after", tool="publish")
    flush(trk, "--item", live["P"])
    row = page_row(trk_home, live["P"])
    assert row["state"] == "publishing" and row["slug"] is None and row["last_error"]
    assert len(fake.artifacts) == 1
    fake.faults.clear()
    fake.calls.clear()
    flush(trk, "--item", live["P"])
    assert fake.tool_calls("publish") == []
    (slug,) = fake.artifacts
    row = page_row(trk_home, live["P"])
    assert (row["slug"], row["state"], row["last_error"]) == (slug, "live", None)
    # A page found by its tag from an earlier run may hold an older render, so it gets the whole body.
    (update,) = fake.tool_calls("update")
    assert update["body"] == render.render(db(trk_home), live["P"]) and update["base_version"] == 1
    assert [r["item"] for r in journal(trk_home) if r["change"] == "page.created"] == [live["P"]]


def test_a_publishing_row_whose_page_never_arrived_is_published_again(trk, trk_home, live, fake):
    fake.fail_next("502", tool="publish")  # the edge refused before the origin saw it
    flush(trk, "--item", live["P"])
    assert page_row(trk_home, live["P"])["state"] == "publishing"
    assert fake.artifacts == {}
    flush(trk, "--item", live["P"])
    assert len(fake.artifacts) == 1
    assert page_row(trk_home, live["P"])["state"] == "live"


def test_a_refused_publish_after_an_unfinished_one_keeps_the_row_publishing(trk, trk_home, live, fake):
    fake.fail_next("502", tool="publish")
    flush(trk, "--item", live["P"])
    assert page_row(trk_home, live["P"])["state"] == "publishing"
    fake.fail_next("500", tool="publish")  # not one the edge retries, but still a 5xx: ambiguous
    fake.teams.clear()  # this time the origin refuses outright
    flush(trk, "--item", live["P"])
    row = page_row(trk_home, live["P"])
    assert row["state"] == "publishing" and "unknown_team" not in (row["last_error"] or "x")
    fake.faults.clear()
    flush(trk, "--item", live["P"])
    row = page_row(trk_home, live["P"])
    assert row["state"] == "publishing" and "unknown_team" in row["last_error"]  # the earlier one may turn up


def test_a_late_page_from_an_earlier_attempt_gets_the_whole_body(trk, trk_home, live, fake):
    # The first attempt's page reaches the origin, but the tag lookup misses it (an edge 502 on the
    # listing every time); the next run's lookup misses it too, and its own publish is ambiguous.
    fake.fail_next("connection", tool="find_tagged", times=8)
    fake.fail_next("502-after", tool="publish")
    flush(trk, "--item", live["P"])
    assert page_row(trk_home, live["P"])["state"] == "publishing" and len(fake.artifacts) == 1
    fake.faults.clear()
    (slug,) = fake.artifacts
    fake.artifacts[slug]["versions"] = ["<p>an older render</p>"]
    flush(trk, "--item", live["P"])
    assert fake.tool_calls("publish")[1:] == []  # found by its tag, so never published twice
    assert fake.body(slug) == render.render(db(trk_home), live["P"])


def test_a_refused_publish_is_recorded_and_the_others_go_on(trk, trk_home, live, fake):
    fake.teams.clear()  # the token's user is in no team called tracker: a tool error, never ambiguous
    report = flush(trk)
    assert fake.tool_calls("find_tagged") == []
    row = page_row(trk_home, live["P"])
    assert row["slug"] is None and row["state"] == "live" and "unknown_team" in row["last_error"]
    assert sorted(report["failed"]) == sorted([*live.values(), ME, TOP])
    assert trk("pages", "--errors", "--json").json()["pages"][0]["last_error"] == row["last_error"]


# ----- AC 3: no network call inside a SQLite transaction ----------------------------------------------


def test_no_network_call_happens_inside_a_sqlite_transaction(trk, live, fake, monkeypatch):
    connections = []
    real_connect = store._connect

    def tracking(path):
        conn = real_connect(path)
        connections.append(conn)
        return conn

    monkeypatch.setattr(store, "_connect", tracking)
    seen = []

    real = fake.call

    def guarded(name, arguments, **kwargs):
        open_tx = [conn for conn in connections if _in_transaction(conn)]
        seen.append(name)
        assert open_tx == [], "a backend call ran inside a SQLite transaction"
        return real(name, arguments, **kwargs)

    monkeypatch.setattr(fake, "call", guarded)
    fake.fail_next("502-after", tool="publish")
    flush(trk)
    assert len(seen) >= 7 and connections


def _in_transaction(conn):
    try:
        return conn.in_transaction
    except sqlite3.ProgrammingError:  # closed
        return False


# ----- AC 4: team at publish only, and never without one ----------------------------------------------


def test_a_new_page_is_never_published_while_the_team_is_unset(trk, trk_home, live, fake):
    conn = db(trk_home)
    with conn:
        conn.execute("UPDATE meta SET value = '' WHERE key = 'publish_team'")
    conn.close()
    report = flush(trk)
    assert fake.tool_calls("publish") == []
    assert sorted(report["failed"]) == sorted([*live.values(), ME, TOP])
    assert "team" in page_row(trk_home, live["P"])["last_error"]


# ----- Size --------------------------------------------------------------------------------------------


def test_a_page_over_two_mib_is_never_sent(trk, trk_home, live, fake):
    from gated import section_file

    for n in range(MAX_BODY_BYTES // (250 * 1024) + 1):  # a section holds at most 256 KiB
        big = section_file(f"notes-{n}", "x" * (250 * 1024))
        assert trk("section", "set", live["T"], f"notes-{n}", "--file", str(big)).code == 0
    report = flush(trk)
    assert f"trk:{live['T']}" not in [tag_of(args) for args in fake.tool_calls("publish")]
    row = page_row(trk_home, live["T"])
    assert row["last_error"] == "too-large" and row["slug"] is None
    assert live["T"] in report["failed"]
    assert next(p for p in trk("pages", "--json").json()["pages"] if p["item"] == live["T"])["dirty"] is True


# ----- the lock --------------------------------------------------------------------------------------


def test_flush_takes_the_pages_lock_for_each_page(trk, trk_home, live, fake, monkeypatch):
    import fcntl

    held = []
    real = fake.call

    def probing(name, arguments, **kwargs):
        with open(trk_home / "pages.lock", "a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)
                held.append(False)
        return real(name, arguments, **kwargs)

    monkeypatch.setattr(fake, "call", probing)
    flush(trk)
    assert held and all(held)


# ----- shells: what page_handles stores ----------------------------------------------------------------


def test_a_shell_replaces_nested_handles_with_placeholders():
    from trk import pages

    html = ('<html><head><meta charset="utf-8"><style>a > b {}</style></head><body>'
            '<section data-comment-id="tiles"><p>x</p><div data-comment-id="child-a">A<br>'
            '<span data-comment-id="inner">i</span></div></section>'
            '<header data-comment-id="header">H</header></body></html>')
    shells = pages.shells(html)
    assert shells["@root"] == ('<html><head><meta charset="utf-8"><style>a > b {}</style></head><body>'
                               '<!--tiles--><!--header--></body></html>')
    assert shells["tiles"] == '<section data-comment-id="tiles"><p>x</p><!--child-a--></section>'
    assert shells["child-a"] == '<div data-comment-id="child-a">A<br><!--inner--></div>'
    assert shells["inner"] == '<span data-comment-id="inner">i</span>'


def test_a_rendered_page_hashes_every_handle_and_only_a_changed_shell_changes(trk, live, trk_home):
    from trk import pages

    before = pages.hashes(render.render(db(trk_home), live["J"]))
    assert "@root" in before and "header" in before and f"child-{live['T']}" in before
    assert trk("edit", live["T"], "--title", "T renamed").code == 0
    after = pages.hashes(render.render(db(trk_home), live["J"]))
    changed = {handle for handle in before if before[handle] != after.get(handle)}
    assert f"child-{live['T']}" in changed and "header" not in changed and "@root" not in changed
