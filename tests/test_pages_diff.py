"""Affected pages, diffing and debounce (Page publisher spec: Affected pages, Timing, Recording marks,
Diffing and patching, RENDER_VERSION), against the in-process fake page backend. The real
service is never called."""

import re
import sqlite3

import pytest

from fake_backend import handles_in
from gated import section_file, ticket
from test_pages_flush import db, flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_setup import ME, fake, journal, meta, token_file, tree  # noqa: F401 (fixtures)
from trk import pages, render, store

T0 = "2026-10-11T09:00:00Z"


def at(monkeypatch, moment):
    monkeypatch.setenv("TRK_NOW", moment)


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def quiet_flush(trk):
    """A flush as the timer or flusher runs it: only what is due, no --now."""
    result = trk("pages", "flush", "--json")
    assert result.code == 0, result.err
    return result.json()


def writes_to(fake, slug):
    return [args for args in fake.tool_calls("update") if args["slug"] == slug]


def edited(args):
    return [edit["section"] for edit in args.get("edits") or []]


def dirty(trk_home):
    conn = db(trk_home)
    try:
        return {row["item_id"] for row in conn.execute("SELECT item_id FROM pages WHERE due_at IS NOT NULL")}
    finally:
        conn.close()


@pytest.fixture
def published(trk, trk_home, live, fake, monkeypatch):
    """The acceptance store with every page published and clean, the fake's call log cleared."""
    at(monkeypatch, T0)
    flush(trk)
    assert dirty(trk_home) == set()
    fake.calls.clear()
    return {key: {"id": live[key], "slug": page_row(trk_home, live[key])["slug"]} for key in live}


# ----- AC 4: an ask's box on every ancestor, and T untouched ------------------------------------------


def test_an_ask_shows_the_same_box_on_every_ancestor_and_leaves_other_pages_alone(trk, trk_home, published, fake,
                                                                                    monkeypatch):
    p, j, d, t = (published[k] for k in ("P", "J", "D", "T"))
    at(monkeypatch, "2026-10-11T09:10:00Z")
    ok(trk, "claim", d["id"], "--session", "S1", "--as", "worker")
    ask = ok(trk, "ask", d["id"], "--to", "product", "Q?", "--question", f"@{section_file('q', 'Which?\n')}",
             "--as", "worker")
    at(monkeypatch, "2026-10-11T09:20:00Z")
    quiet_flush(trk)
    assert writes_to(fake, t["slug"]) == []
    boxes = []
    for page in (d, j, p):
        body = fake.body(page["slug"])
        assert body == render.render(db(trk_home), page["id"])
        assert len(re.findall(rf'data-comment-id="ask-{ask}"', body)) == 1
        boxes.append(_element(body, f"ask-{ask}"))
        assert re.search(r"\bproduct\b", boxes[-1]) and re.search(r"\bkim\b", boxes[-1])
    assert boxes[0] == boxes[1] == boxes[2]
    assert f"child-{ask}" in _element(fake.body(d["slug"]), "children")


def _element(body, handle):
    shells = pages.parse(body)
    start, end = shells.spans[handle]
    return body[start:end]


# ----- AC 5: twenty writes in a minute are one patch; claim renew marks nothing -----------------------


def test_twenty_writes_within_a_minute_are_one_patch_of_only_the_changed_handles(trk, trk_home, published, fake,
                                                                                 monkeypatch):
    t = published["T"]
    before = pages.hashes(fake.body(t["slug"]))
    for n in range(20):
        at(monkeypatch, f"2026-10-11T09:01:{n * 3:02d}Z")
        ok(trk, "section", "set", t["id"], "what-to-build", "--file", str(section_file(f"w{n}", f"Build {n}.\n")))
        quiet_flush(trk)
    assert writes_to(fake, t["slug"]) == []
    at(monkeypatch, "2026-10-11T09:02:57Z")  # 60 s after the last write
    quiet_flush(trk)
    (write,) = writes_to(fake, t["slug"])
    after = pages.hashes(render.render(db(trk_home), t["id"]))
    changed = {handle for handle in after if after[handle] != before.get(handle)}
    assert "body" not in write and "title" not in write and write["base_version"] == 1
    assert set(edited(write)) <= changed and "what-to-build" in edited(write)
    assert fake.body(t["slug"]) == render.render(db(trk_home), t["id"])
    assert page_row(trk_home, t["id"])["version"] == 2


def test_claim_renew_marks_no_page(trk, trk_home, published, fake, monkeypatch):
    ok(trk, "claim", published["D"]["id"], "--session", "S1", "--lease", "30m", "--as", "worker")
    flush(trk)
    assert dirty(trk_home) == set()
    at(monkeypatch, "2026-10-11T09:20:00Z")  # past half of the 30-minute lease
    seq = journal(trk_home)[-1]["seq"]
    ok(trk, "claim", "renew", "--session", "S1", "--as", "worker")
    assert [row["change"] for row in journal(trk_home, seq)] == ["claim.renewed"]
    fake.calls.clear()
    quiet_flush(trk)
    assert dirty(trk_home) == set() and fake.tool_calls("update") == []
    assert meta(trk_home, "publish_cursor") == str(journal(trk_home)[-1]["seq"])


# ----- the marking table -------------------------------------------------------------------------------


def test_a_write_marks_the_item_and_its_ancestors_due_sixty_seconds_later(trk, trk_home, published, monkeypatch):
    t, j, p, d = (published[k]["id"] for k in ("T", "J", "P", "D"))
    at(monkeypatch, "2026-10-11T09:05:00Z")
    ok(trk, "section", "set", t, "what-to-build", "--file", str(section_file("w", "Build it.\n")))
    quiet_flush(trk)
    assert dirty(trk_home) == {t, j, p}
    row = page_row(trk_home, t)
    assert (row["due_at"], row["first_dirty_at"]) == ("2026-10-11T09:06:00.000Z", "2026-10-11T09:05:00.000Z")
    assert d not in dirty(trk_home)


def test_a_rename_marks_every_descendant_for_its_breadcrumbs(trk, trk_home, published, monkeypatch):
    at(monkeypatch, "2026-10-11T09:05:00Z")
    ok(trk, "edit", published["J"]["id"], "--title", "J renamed")
    quiet_flush(trk)
    assert dirty(trk_home) == {published[k]["id"] for k in ("P", "J", "D", "T")} | {ME}  # titles show there too


def test_an_edge_marks_both_ends_and_their_ancestors(trk, trk_home, published, monkeypatch):
    at(monkeypatch, "2026-10-11T09:05:00Z")
    ok(trk, "link", published["D"]["id"], "blocks", published["T"]["id"])
    quiet_flush(trk)
    assert dirty(trk_home) == {published[k]["id"] for k in ("P", "J", "D", "T")} | {ME}  # blocked on michael


def test_a_move_marks_old_and_new_ancestors_and_descendants(trk, trk_home, published, fake, monkeypatch):
    at(monkeypatch, "2026-10-11T09:05:00Z")
    j2 = ok(trk, "new", "project", "J2", "--parent", published["P"]["id"])
    flush(trk)
    at(monkeypatch, "2026-10-11T09:10:00Z")
    ok(trk, "move", published["T"]["id"], "--parent", j2)
    quiet_flush(trk)
    assert dirty(trk_home) == {published["P"]["id"], published["J"]["id"], published["T"]["id"], j2, ME}


def test_role_changes_mark_every_page(trk, trk_home, published, monkeypatch):
    at(monkeypatch, "2026-10-11T09:05:00Z")
    ok(trk, "roles", "set", "product", "michael")
    quiet_flush(trk)
    assert dirty(trk_home) == {published[k]["id"] for k in published} | {ME}


def test_marks_keep_the_latest_due_time_and_the_first_dirty_time():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE pages (item_id TEXT PRIMARY KEY, due_at TEXT, first_dirty_at TEXT)")
    conn.execute("INSERT INTO pages VALUES ('a', NULL, NULL)")
    pages.mark_pages(conn, {"a": ("2026-10-11T09:01:00.000Z", "2026-10-11T09:00:00.000Z")})
    pages.mark_pages(conn, {"a": ("2026-10-11T09:00:30.000Z", "2026-10-11T08:59:30.000Z")})
    assert tuple(conn.execute("SELECT due_at, first_dirty_at FROM pages").fetchone()) == (
        "2026-10-11T09:01:00.000Z", "2026-10-11T09:00:00.000Z")


# ----- AC 3: debounce caps ------------------------------------------------------------------------------


def test_a_page_that_never_goes_quiet_is_still_sent_at_the_five_minute_cap(trk, trk_home, published, fake,
                                                                          monkeypatch):
    t = published["T"]
    sent_at = []
    for n in range(13):  # every 30 s for 6 minutes, from 09:10:00
        minute, second = divmod(n * 30, 60)
        moment = f"2026-10-11T09:{10 + minute:02d}:{second:02d}Z"
        at(monkeypatch, moment)
        ok(trk, "section", "set", t["id"], "what-to-build", "--file", str(section_file(f"w{n}", f"Build {n}.\n")))
        before = len(writes_to(fake, t["slug"]))
        quiet_flush(trk)
        if len(writes_to(fake, t["slug"])) > before:
            sent_at.append(moment)
    assert sent_at == ["2026-10-11T09:15:00Z"]


def test_a_failed_page_waits_five_minutes_from_its_failure(trk, trk_home, published, fake, monkeypatch):
    t = published["T"]
    at(monkeypatch, "2026-10-11T09:10:00Z")
    ok(trk, "section", "set", t["id"], "what-to-build", "--file", str(section_file("w", "Build.\n")))
    fake.grants.discard("update")
    at(monkeypatch, "2026-10-11T09:11:00Z")
    report = quiet_flush(trk)
    assert t["id"] in report["failed"]
    assert page_row(trk_home, t["id"])["last_error_at"] == "2026-10-11T09:11:00.000Z"
    fake.grants.add("update")
    fake.calls.clear()
    at(monkeypatch, "2026-10-11T09:15:59Z")  # past the first mark's 5-minute cap, but not 5 min after the failure
    quiet_flush(trk)
    assert writes_to(fake, t["slug"]) == []
    at(monkeypatch, "2026-10-11T09:16:00Z")
    quiet_flush(trk)
    assert len(writes_to(fake, t["slug"])) == 1
    row = page_row(trk_home, t["id"])
    assert (row["due_at"], row["first_dirty_at"], row["last_error"]) == (None, None, None)


def test_now_sends_a_marked_page_at_once_and_item_limits_it(trk, trk_home, published, fake, monkeypatch):
    at(monkeypatch, "2026-10-11T09:05:00Z")
    ok(trk, "section", "set", published["T"]["id"], "what-to-build", "--file", str(section_file("w", "Now.\n")))
    flush(trk, "--item", published["T"]["id"])
    assert [args["slug"] for args in fake.tool_calls("update")] == [published["T"]["slug"]]
    assert dirty(trk_home) == {published["J"]["id"], published["P"]["id"]}  # the rest still wait their minute


def test_now_with_item_sends_a_failed_page_without_waiting(trk, trk_home, published, fake, monkeypatch):
    t = published["T"]
    at(monkeypatch, "2026-10-11T09:10:00Z")
    ok(trk, "section", "set", t["id"], "what-to-build", "--file", str(section_file("w", "Build.\n")))
    fake.grants.discard("update")
    flush(trk, "--item", t["id"])
    assert page_row(trk_home, t["id"])["last_error"]
    fake.grants.add("update")
    fake.calls.clear()
    flush(trk, "--item", t["id"])  # the same minute: --now ignores the pause after a failure
    assert len(writes_to(fake, t["slug"])) == 1 and page_row(trk_home, t["id"])["last_error"] is None


# ----- AC 4: diff minimality and RENDER_VERSION ------------------------------------------------------------


@pytest.fixture
def ten(trk, trk_home, live, fake, monkeypatch):
    """J with ten tickets, every page published and clean."""
    at(monkeypatch, T0)
    tickets = [live["T"]] + [ok(trk, "new", "work-item", f"T{n}", "--form", "spec-bound", "--parent", live["J"],
                                *ticket("spec-bound")) for n in range(2, 11)]
    flush(trk)
    assert dirty(trk_home) == set()
    fake.calls.clear()
    return {"J": live["J"], "P": live["P"], "D": live["D"], "tickets": tickets}


def test_closing_one_of_ten_tickets_patches_its_row_and_tiles_only(trk, trk_home, ten, fake, monkeypatch):
    closing, *siblings = ten["tickets"]
    at(monkeypatch, "2026-10-11T09:30:00Z")
    ok(trk, "close", closing, "--resolution", "wont-do", "--outcome", "Not needed.")
    flush(trk)
    (write,) = writes_to(fake, page_row(trk_home, ten["J"])["slug"])
    assert f"child-{closing}" in edited(write)
    assert set(edited(write)) <= {f"child-{closing}", "tiles", "stage-funnel", "waiting"}
    for sibling in siblings:
        assert writes_to(fake, page_row(trk_home, sibling)["slug"]) == []
    assert fake.body(page_row(trk_home, ten["J"])["slug"]) == render.render(db(trk_home), ten["J"])


def test_a_render_version_bump_republishes_every_page_once(trk, trk_home, ten, fake, monkeypatch):
    monkeypatch.setattr(render, "RENDER_VERSION", render.RENDER_VERSION + 1)
    quiet_flush(trk)
    every = [page_row(trk_home, item)["slug"] for item in [ten["P"], ten["J"], ten["D"], *ten["tickets"]]]
    for slug in every:
        (write,) = writes_to(fake, slug)
        assert "body" in write
    assert meta(trk_home, "render_version") == str(render.RENDER_VERSION)
    fake.calls.clear()
    flush(trk)
    assert fake.tool_calls("update") == []


def test_more_than_fifty_edits_send_the_whole_body():
    old = "".join(f'<p data-comment-id="h{n}">{n}</p>' for n in range(60))
    new = "".join(f'<p data-comment-id="h{n}">{n}!</p>' for n in range(60))
    assert pages.patch(new, pages.hashes(old)) == pages.WHOLE
    few = new.replace("!", "", 55)
    assert [edit["section"] for edit in pages.patch(few, pages.hashes(old))] == [f"h{n}" for n in range(55, 60)]


def test_a_changed_root_sends_the_whole_body_and_nested_handles_go_with_their_outermost_parent():
    old = '<main><div data-comment-id="a"><p data-comment-id="b">1</p><p data-comment-id="c">2</p></div></main>'
    assert pages.patch(old, pages.hashes(old)) is None
    nested = old.replace(">1<", ">one<")
    assert pages.patch(nested, pages.hashes(old)) == [{"section": "b", "replace": '<p data-comment-id="b">one</p>'}]
    dropped = old.replace('<p data-comment-id="c">2</p>', "")
    (edit,) = pages.patch(dropped, pages.hashes(old))
    assert edit["section"] == "a" and edit["replace"] == '<div data-comment-id="a"><p data-comment-id="b">1</p></div>'
    assert pages.patch(old.replace("<main>", "<main class=x>"), pages.hashes(old)) == pages.WHOLE
    assert pages.patch(old, {}) == pages.WHOLE


# ----- title updates ----------------------------------------------------------------------------------------


def test_a_rename_sets_the_page_title_in_the_same_write(trk, trk_home, published, fake, monkeypatch):
    t = published["T"]
    at(monkeypatch, "2026-10-11T09:05:00Z")
    ok(trk, "edit", t["id"], "--title", "T renamed")
    flush(trk)
    (write,) = writes_to(fake, t["slug"])  # <title> is in the root shell, so this one is a whole body
    assert write["title"] == "T renamed" and write["body"] == render.render(db(trk_home), t["id"])
    assert fake.artifacts[t["slug"]]["title"] == "T renamed"
    (other,) = writes_to(fake, published["J"]["slug"])
    assert "title" not in other


# ----- AC 5: handle stability ---------------------------------------------------------------------------------


def test_every_handle_survives_a_scripted_series_or_names_what_left_the_page(trk, trk_home, live, monkeypatch):
    at(monkeypatch, T0)
    j2 = ok(trk, "new", "project", "J2", "--parent", live["P"])
    t2 = ok(trk, "new", "work-item", "T2", "--form", "spec-bound", "--parent", live["J"], *ticket("spec-bound"))
    question = f"@{section_file('q', 'Which?\n')}"
    steps = [
        ("close", lambda: ok(trk, "close", t2, "--resolution", "wont-do", "--outcome", "Not needed.")),
        ("ask", lambda: ok(trk, "ask", live["D"], "--to", "product", "Q?", "--question", question, "--as", "worker")),
        ("answer", None),
        ("rename", lambda: ok(trk, "edit", live["T"], "--title", "T renamed")),
        ("move", lambda: ok(trk, "move", live["T"], "--parent", j2)),
        ("reopen", lambda: ok(trk, "reopen", t2)),
        ("reopen ask", lambda: ok(trk, "reopen", asked[0], "--reason", "need units", "--as", "worker")),
    ]
    page_ids = [live[k] for k in ("P", "J", "D", "T")] + [j2, t2]
    asked = []
    for name, step in steps:
        conn = db(trk_home)
        before = {item: handles_in(render.render(conn, item)) for item in page_ids}
        conn.close()
        if name == "answer":
            ok(trk, "answer", asked[0], "--outcome", "Yes.", "--answer", f"@{section_file('a', 'Yes.\n')}",
               "--as", "kim")
        else:
            out = step()
            if name == "ask":
                asked.append(out)
        conn = db(trk_home)
        for item in page_ids:
            after = set(handles_in(render.render(conn, item)))
            for handle in set(before[item]) - after:
                assert _left_the_page(conn, item, handle), (name, item, handle)
        conn.close()


def _left_the_page(conn, item_id, handle):
    """Whether a vanished handle names a thing no longer on this page: a child row of an item that
    is no longer a direct child, or the box of an ask no longer open in the subtree."""
    kind, _, other = handle.partition("-")
    if kind == "child":
        parent = conn.execute("SELECT parent_id FROM items WHERE id = ?", (other,)).fetchone()[0]
        return parent != item_id
    if kind == "ask":
        row = conn.execute("SELECT status FROM items WHERE id = ?", (other,)).fetchone()
        below = {r[0] for r in conn.execute(
            "WITH RECURSIVE sub(id) AS (SELECT id FROM items WHERE parent_id = ? "
            "UNION ALL SELECT i.id FROM items i JOIN sub ON i.parent_id = sub.id) SELECT id FROM sub", (item_id,))}
        return row[0] != "open" or other not in below
    return False


# ----- AC 6: the cursor and its marks are one transaction -------------------------------------------------


def test_a_crash_while_marking_leaves_the_cursor_and_marks_untouched(trk, trk_home, published, monkeypatch):
    at(monkeypatch, "2026-10-11T09:05:00Z")
    ok(trk, "section", "set", published["T"]["id"], "what-to-build", "--file", str(section_file("w", "One.\n")))
    ok(trk, "section", "set", published["D"]["id"], "question", "--file", str(section_file("q", "Two?\n")))
    cursor = meta(trk_home, "publish_cursor")
    real = pages._set_meta

    def crashing(conn, key, value):  # the marks are written; the cursor advance is next
        if key == "publish_cursor":
            assert conn.execute("SELECT COUNT(*) FROM pages WHERE due_at IS NOT NULL").fetchone()[0] == 4
            raise RuntimeError("crash before the cursor moved")
        return real(conn, key, value)

    monkeypatch.setattr(pages, "_set_meta", crashing)
    st = store.open_store()
    try:
        with pytest.raises(RuntimeError):
            pages.mark(st)
    finally:
        st.close()
    assert meta(trk_home, "publish_cursor") == cursor
    assert dirty(trk_home) == set()
    monkeypatch.setattr(pages, "_set_meta", real)
    quiet_flush(trk)
    assert dirty(trk_home) == {published[k]["id"] for k in ("P", "J", "D", "T")}
    assert meta(trk_home, "publish_cursor") == str(journal(trk_home)[-1]["seq"])
