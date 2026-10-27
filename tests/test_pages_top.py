"""The top-level page (Page publisher spec: The top-level page): one page per store listing every root item
and where it stands, the breadcrumb trail every page carries back to it, and the in-progress box each page
shows for its own subtree. Against the in-process fake page backend; no real backend is ever called."""

import re

import pytest

from gated import DEST, ticket
from test_pages_flush import db, flush, live, page_row, tag_of  # noqa: F401 (fixtures)
from test_pages_setup import ME, fake, journal, setup_argv, token_file, tree  # noqa: F401 (fixtures)
from trk import render, top

TOP = "top:"
SESSION = "11111111-2222-3333-4444-555555555555"


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out


def page_of(trk, key):
    return ok(trk, "pages", "render", key, "--out", "-")


def crumbs(body):
    """The crumb labels of a page's trail, or None when it has no trail."""
    match = re.search(r'<nav class="crumbs">(.*?)</nav>', body, re.S)
    if match is None:
        return None
    parts = [part.strip() for part in re.sub(r"<[^>]+>", "\x00", match.group(1)).split("\x00")]
    return [part for part in parts if part and part != "›"]


def in_progress(body):
    """The item ids the in-progress box lists, in page order; None when the page has no box."""
    match = re.search(r'<section class="rollup" data-comment-id="in-progress">(.*?)</section>', body, re.S)
    if match is None:
        return None
    return re.findall(r'data-comment-id="progress-([^"]+)"', match.group(1))


def tiles(body):
    """The top-level page's tiles as {label: value}."""
    found = re.findall(r'<span class="n">([^<]*)</span><span class="l">([^<]*)</span>', body)
    return {label: value for value, label in found}


def roots_listed(body):
    """The title cell of every row of the roots table, in page order. A title is plain text until its
    own page is published and a link after that, so the text is what the test reads."""
    match = re.search(r'<section class="rollup" data-comment-id="roots">(.*?)</section>', body, re.S)
    assert match is not None, "the top-level page has no roots table"
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", match.group(1), re.S)
    first = [re.match(r"\s*<td>(.*?)</td>", row, re.S) for row in rows]
    return [re.sub(r"<[^>]+>", "", cell.group(1)).strip() for cell in first if cell]


@pytest.fixture
def two_roots(trk, tree):
    """The acceptance store plus a second program Q at the top level."""
    q = ok(trk, "new", "program", "Q", *DEST).strip()
    return dict(tree, Q=q)


# ----- the page itself ------------------------------------------------------------------------------


def test_the_top_level_page_lists_every_open_root_item(trk, two_roots):
    body = page_of(trk, TOP)
    assert "<h1>All work</h1>" in body
    assert roots_listed(body) == ["P", "Q"]
    assert in_progress(body) == []  # nothing claimed or built yet


def test_the_top_level_page_counts_what_is_under_every_root(trk, two_roots):
    counts = tiles(page_of(trk, TOP))
    assert counts["tickets done"] == "0/1"
    assert counts["top-level items"] == "2"


def test_the_top_level_page_renders_the_same_bytes_at_a_later_clock(trk, two_roots, monkeypatch):
    """Nothing on it is relative to now, so a later render of the same store is byte-identical and no
    flush rewrites it. A render time in its header would have made every flush write the page."""
    first = page_of(trk, TOP)
    monkeypatch.setenv("TRK_NOW", "2026-10-11T23:59:00Z")
    assert page_of(trk, TOP) == first


def test_a_closed_root_leaves_the_top_level_page(trk, two_roots):
    """Michael, 2026-10-26: "can we have top level only show OPEN items". The root keeps its own page and
    its trail back here; what goes is its line on this page and its share of the totals."""
    ok(trk, "close", two_roots["Q"], "--resolution", "wont-do", "--outcome", "dropped")
    body = page_of(trk, TOP)
    assert roots_listed(body) == ["P"]
    assert tiles(body)["top-level items"] == "1"
    assert crumbs(page_of(trk, two_roots["Q"])) == ["All work"]


def test_the_totals_cover_only_the_roots_the_page_lists(trk, two_roots):
    """Every number on the page describes the work it shows, so a closed root takes its subtree's counts
    with it: `tickets done` is live work, not the store's history."""
    q = two_roots["Q"]
    qt = ok(trk, "new", "work-item", "QT", "--form", "self-contained", "--parent", q,
            *ticket("self-contained")).strip()
    assert tiles(page_of(trk, TOP))["tickets done"] == "0/2"
    ok(trk, "close", qt, "--resolution", "wont-do", "--outcome", "dropped")
    ok(trk, "close", q, "--resolution", "wont-do", "--outcome", "dropped")
    assert tiles(page_of(trk, TOP))["tickets done"] == "0/1"


# ----- the trail every page carries ----------------------------------------------------------------


def test_every_page_trail_starts_at_the_top_level_page(trk, tree):
    assert crumbs(page_of(trk, tree["P"])) == ["All work"]
    assert crumbs(page_of(trk, tree["J"])) == ["All work", "P"]
    assert crumbs(page_of(trk, tree["T"])) == ["All work", "P", "J"]


def test_the_trail_links_the_top_level_page_once_it_is_published(trk, trk_home, live, fake):
    assert "All work</a>" not in page_of(trk, live["P"])  # plain text before the page exists
    flush(trk)
    url = page_row(trk_home, TOP)["url"]
    assert url
    assert f'<a href="{url}">All work</a>' in fake.body(page_row(trk_home, live["T"])["slug"])


# ----- what is in progress underneath ---------------------------------------------------------------


def test_a_claim_shows_on_every_page_above_it_and_on_the_top_level_page(trk, tree):
    assert trk("claim", tree["D"], "--session", SESSION).code == 0, "D is a decision: claimable as it stands"
    assert in_progress(page_of(trk, tree["P"])) == [tree["D"]]   # every page above it
    assert in_progress(page_of(trk, tree["J"])) == [tree["D"]]   # D sits under J
    assert in_progress(page_of(trk, TOP)) == [tree["D"]]         # and the top-level page


def test_a_page_with_nothing_below_it_has_no_in_progress_box(trk, tree):
    assert in_progress(page_of(trk, tree["T"])) is None


def test_a_released_claim_leaves_the_in_progress_box(trk, tree):
    assert trk("claim", tree["D"], "--session", SESSION).code == 0
    assert trk("claim", "release", tree["D"], "--session", SESSION).code == 0
    assert in_progress(page_of(trk, tree["P"])) == []


# ----- publishing and marking ------------------------------------------------------------------------


def test_the_top_level_page_is_published_first_and_journals_no_page_created(trk, trk_home, live, fake):
    """It goes before any item page, so every page published after it is born linking it, and it is no
    item, so its own first publish journals no `page.created`."""
    since = journal(trk_home)[-1]["seq"]
    flush(trk)
    assert tag_of(fake.tool_calls("publish")[0]) == f"trk:{TOP}"
    assert [row["item"] for row in journal(trk_home, since) if row["change"] == "page.created"] \
        == [live[k] for k in ("P", "J", "D", "T")]


def test_a_write_the_top_level_page_shows_sends_it_again(trk, trk_home, live, fake):
    """A write it shows reaches it: marking covers every row, and the diff keeps the send to what changed,
    so a write it does not show (a rename deep in the tree) leaves its version where it was."""
    flush(trk)
    first = page_row(trk_home, TOP)["version"]
    assert trk("edit", live["T"], "--title", "T renamed").code == 0
    flush(trk)  # the flusher's own pass is what marks pages due
    assert page_row(trk_home, TOP)["version"] == first, "T is no root and is not in progress"
    assert trk("edit", live["P"], "--title", "P renamed").code == 0
    flush(trk)
    assert page_row(trk_home, TOP)["version"] > first
    assert "P renamed" in fake.body(page_row(trk_home, TOP)["slug"])


def test_the_top_level_page_is_in_the_page_listing_once(trk, live):
    rows = [row for row in trk("pages", "--json").json()["pages"] if row["item"] == TOP]
    assert [(row["kind"], row["title"]) for row in rows] == [("top", "All work")]


def test_pages_render_names_the_top_level_page_by_its_key(trk, tree):
    assert trk("pages", "render", TOP, "--out", "-", "--json").json()["item"] == TOP
    assert top.is_key(TOP) and top.KEY == render.TOP
