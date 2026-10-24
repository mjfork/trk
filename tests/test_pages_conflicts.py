"""Conflicts and `trk pages republish` (Page publisher spec: Conflicts; Reads and verbs), against the
in-process fake artifact service. The real service is never called."""

import fake_artifacts
from gated import section_file
from test_pages_diff import at, dirty, ok, published, writes_to  # noqa: F401 (fixtures)
from test_pages_flush import db, flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_setup import fake, journal, meta, token_file, tree  # noqa: F401 (fixtures)
from trk import flusher, pages, render


def outer(body, handle):
    start, end = pages.parse(body).spans[handle]
    return body[start:end]


def rewrite(fake, slug, handle, text):
    """Someone other than the tracker replaces one handled section's content on the page."""
    old = outer(fake.body(slug), handle)
    new = old[:old.index(">") + 1] + f"<p>{text}</p>" + old[old.rindex("</"):]
    return fake.outside_write(slug, edits=[{"section": handle, "replace": new}])


def tracker_change(trk, item_id, n=1):
    ok(trk, "section", "set", item_id, "problem", "--file", str(section_file(f"p{n}", f"Problem {n}.\n")))


def expected(trk_home, item_id):
    return render.render(db(trk_home), item_id)


# ----- ac-1: a stale baseVersion reads the changed handles and re-sends on currentVersion ----------------


def test_a_stale_base_version_reads_the_changed_handles_and_resends_on_the_current_version(
        trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    base = page_row(trk_home, j["id"])["version"]
    current = rewrite(fake, j["slug"], "solution", "Edited in the web UI.")
    tracker_change(trk, j["id"])
    flush(trk)
    stale, resend = writes_to(fake, j["slug"])
    assert stale["baseVersion"] == base and [e["section"] for e in stale["edits"]] == ["problem"]
    (read,) = fake.tool_calls("get_artifact")
    assert read == {"slug": j["slug"], "handles": ["solution"]}
    assert resend["baseVersion"] == current and "body" not in resend
    assert sorted(e["section"] for e in resend["edits"]) == ["problem", "solution"]
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])
    row = page_row(trk_home, j["id"])
    assert (row["version"], row["last_error"], row["due_at"]) == (current + 1, None, None)
    assert pages.stored_hashes(db(trk_home), j["id"]) == {
        **pages.hashes(fake.body(j["slug"])), pages.TITLE: pages.title_hash("J")}


def test_an_outside_change_the_render_agrees_with_is_not_sent_again(trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    tracker_change(trk, j["id"])
    wanted = outer(expected(trk_home, j["id"]), "problem")
    current = fake.outside_write(j["slug"], edits=[{"section": "problem", "replace": wanted}])
    flush(trk)
    assert [call["handles"] for call in fake.tool_calls("get_artifact")] == [["problem"]]
    assert len(writes_to(fake, j["slug"])) == 1  # only the stale write: the server already holds the render
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])
    assert page_row(trk_home, j["id"])["version"] == current
    assert dirty(trk_home) == set()


def test_a_rename_that_conflicts_sends_the_whole_body_and_the_title_on_the_current_version(
        trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    current = rewrite(fake, j["slug"], "solution", "Edited in the web UI.")
    ok(trk, "edit", j["id"], "--title", "J renamed")  # the title is in the root shell: a whole body
    flush(trk, "--item", j["id"])
    stale, resend = writes_to(fake, j["slug"])
    assert resend == {"slug": j["slug"], "baseVersion": current, "body": expected(trk_home, j["id"]),
                      "title": "J renamed"}
    assert fake.tool_calls("get_artifact") == []
    assert fake.artifacts[j["slug"]]["title"] == "J renamed"
    assert pages.stored_hashes(db(trk_home), j["id"])[pages.TITLE] == pages.title_hash("J renamed")


def test_an_ambiguous_write_whose_retry_conflicts_on_its_own_version_adopts_it(
        trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    base = page_row(trk_home, j["id"])["version"]
    tracker_change(trk, j["id"])
    fake.fail_next("502-after", tool="update_artifact")
    flush(trk)
    first, retry = writes_to(fake, j["slug"])
    assert first == retry  # the retry carries the same edits on the same base, and conflicts
    assert len(fake.tool_calls("get_artifact")) == 1
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])
    assert page_row(trk_home, j["id"])["version"] == base + 1  # the version the lost answer would have given
    assert dirty(trk_home) == set()


def test_a_whole_body_that_conflicts_is_resent_whole_without_a_read(trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    conn = db(trk_home)
    with conn:
        conn.execute("DELETE FROM page_handles WHERE item_id = ? AND handle = ?", (j["id"], pages.ROOT))
    conn.close()
    current = rewrite(fake, j["slug"], "solution", "Edited in the web UI.")
    tracker_change(trk, j["id"])
    flush(trk)
    stale, resend = writes_to(fake, j["slug"])
    assert "body" in stale and resend["baseVersion"] == current and resend["body"] == expected(trk_home, j["id"])
    assert fake.tool_calls("get_artifact") == []
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])


# ----- ac-2: a section that isn't the tracker's, or a handle gone from the server: the whole body -------


def test_a_changed_section_that_is_not_a_tracker_handle_sends_the_whole_body(trk, trk_home, published, fake,
                                                                               monkeypatch):
    j = published["J"]
    solution = outer(fake.body(j["slug"]), "solution")
    note = solution + '<aside data-comment-id="note-1">A reviewer\'s note.</aside>'
    current = fake.outside_write(j["slug"], edits=[{"section": "solution", "replace": note}])
    tracker_change(trk, j["id"])
    flush(trk)
    stale, resend = writes_to(fake, j["slug"])
    assert "edits" in stale
    assert resend["baseVersion"] == current and resend["body"] == expected(trk_home, j["id"])
    assert fake.tool_calls("get_artifact") == []  # not one of its handles: nothing to read
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])
    assert page_row(trk_home, j["id"])["version"] == current + 1


def test_an_outside_change_outside_every_handle_sends_the_whole_body(trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    body = fake.body(j["slug"])
    assert "</head>" in body
    current = fake.outside_write(j["slug"], body=body.replace("</head>", '<meta name="x-edited" content="1"></head>'))
    tracker_change(trk, j["id"])
    flush(trk)
    stale, resend = writes_to(fake, j["slug"])
    assert "edits" in stale
    assert resend["baseVersion"] == current and resend["body"] == expected(trk_home, j["id"])
    assert fake.tool_calls("get_artifact") == []  # changedSections was empty: nothing to read
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])


def test_a_tracker_handle_missing_on_the_server_sends_the_whole_body(trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    tiles = outer(fake.body(j["slug"]), "tiles")
    funnel = outer(fake.body(j["slug"]), "stage-funnel")
    current = fake.outside_write(j["slug"], edits=[{"section": "tiles", "replace": tiles.replace(funnel, "")}])
    tracker_change(trk, j["id"])
    flush(trk)
    (read,) = fake.tool_calls("get_artifact")
    assert sorted(read["handles"]) == ["stage-funnel", "tiles"]
    stale, resend = writes_to(fake, j["slug"])
    assert resend["baseVersion"] == current and resend["body"] == expected(trk_home, j["id"])
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])


# ----- ac-3: one retry per flush; a read only after a conflict ------------------------------------------


def test_a_second_conflict_in_the_same_flush_leaves_the_page_dirty_with_last_error_conflict(
        trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    base = page_row(trk_home, j["id"])["version"]
    rewrite(fake, j["slug"], "solution", "First outside edit.")
    tracker_change(trk, j["id"])
    reader = fake._get_artifact

    def read_then_someone_writes_again(arguments):
        result = reader(arguments)
        rewrite(fake, j["slug"], "solution", "Second outside edit.")
        return result

    monkeypatch.setattr(fake, "_get_artifact", read_then_someone_writes_again)
    report = flush(trk)
    assert report["failed"] == [j["id"]]
    assert len(writes_to(fake, j["slug"])) == 2 and len(fake.tool_calls("get_artifact")) == 1
    row = page_row(trk_home, j["id"])
    assert (row["last_error"], row["version"]) == ("conflict", base)
    assert row["due_at"] is not None and j["id"] in dirty(trk_home)

    monkeypatch.setattr(fake, "_get_artifact", reader)
    flush(trk)  # the next flush starts again from the stored version, reads once more, and lands
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])
    assert page_row(trk_home, j["id"])["last_error"] is None


def test_no_read_happens_without_a_conflict(trk, trk_home, published, fake, monkeypatch):
    for n in (1, 2, 3):
        at(monkeypatch, f"2026-10-11T09:0{n}:00Z")
        ok(trk, "section", "set", published["T"]["id"], "what-to-build",
           "--file", str(section_file(f"w{n}", f"Build {n}.\n")))
        flush(trk)
    assert fake.tool_calls("update_artifact")
    assert fake.tool_calls("get_artifact") == []


# ----- ac-4: `trk pages republish <id>` ------------------------------------------------------------------


def test_republish_sends_the_whole_body_ignoring_stored_hashes_and_refreshes_them(trk, trk_home, published, fake,
                                                                                    monkeypatch):
    j = published["J"]
    conn = db(trk_home)
    with conn:
        conn.execute("UPDATE page_handles SET shell_hash = 'stale' WHERE item_id = ?", (j["id"],))
    conn.close()
    base = page_row(trk_home, j["id"])["version"]
    result = trk("pages", "republish", j["id"], "--json")
    assert result.code == 0, result.err
    assert result.json() == {"schema": 1, "item": j["id"], "slug": j["slug"], "version": base + 1}
    (write,) = writes_to(fake, j["slug"])
    assert write == {"slug": j["slug"], "baseVersion": base, "body": expected(trk_home, j["id"]), "title": "J"}
    assert fake.tool_calls("get_artifact") == []
    assert pages.stored_hashes(db(trk_home), j["id"]) == {
        **pages.hashes(expected(trk_home, j["id"])), pages.TITLE: pages.title_hash("J")}
    assert page_row(trk_home, j["id"])["version"] == base + 1


def test_republish_lands_on_the_current_version_after_an_outside_write(trk, trk_home, published, fake,
                                                                         monkeypatch):
    j = published["J"]
    current = rewrite(fake, j["slug"], "solution", "Edited in the web UI.")
    tracker_change(trk, j["id"])
    ok(trk, "pages", "republish", j["id"])
    stale, resend = writes_to(fake, j["slug"])
    assert resend["baseVersion"] == current and resend["body"] == expected(trk_home, j["id"])
    assert fake.tool_calls("get_artifact") == []
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])
    row = page_row(trk_home, j["id"])
    assert (row["version"], row["due_at"], row["last_error"]) == (current + 1, None, None)


def test_republish_clears_a_failed_page(trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    tracker_change(trk, j["id"])
    fake.grants.discard("update_artifact")
    assert flush(trk, "--item", j["id"])["failed"] == [j["id"]]
    fake.grants.add("update_artifact")
    ok(trk, "pages", "republish", j["id"])
    row = page_row(trk_home, j["id"])
    assert (row["last_error"], row["last_error_at"], row["due_at"]) == (None, None, None)
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])


def test_a_failed_republish_exits_1_and_records_the_error(trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    fake.grants.discard("update_artifact")
    result = trk("pages", "republish", j["id"], "--json")
    assert (result.code, result.error()["rule"], result.error()["item"]) == (1, "republish-failed", j["id"])
    assert "tool_not_allowed" in page_row(trk_home, j["id"])["last_error"]


def test_republish_refuses_an_item_with_no_published_page(trk, trk_home, live, fake):
    result = trk("pages", "republish", live["T"], "--json")
    assert (result.code, result.error()["rule"]) == (1, "no-page")
    assert fake.tool_calls() == []


def test_republish_refuses_before_setup(trk, tree, fake):
    result = trk("pages", "republish", tree["T"], "--json")
    assert (result.code, result.error()["rule"]) == (1, "publishing-not-set-up")


def test_republish_journals_nothing(trk, trk_home, published, fake):
    before = journal(trk_home)
    ok(trk, "pages", "republish", published["P"]["id"])
    assert journal(trk_home) == before


def test_resend_sends_outermost_handles_in_document_order_and_keeps_untouched_refused_edits():
    html = ('<html><body><div data-comment-id="a"><p data-comment-id="a1">x</p></div>'
            '<div data-comment-id="b">y</div><div data-comment-id="c">z</div></body></html>')
    parsed = pages.parse(html)
    read = {"sections": [{"id": "a", "source": "<div>old</div>"}, {"id": "a1", "source": "<p>old</p>"},
                         {"id": "b", "source": parsed.outer("b")}]}
    edits = pages.resend(parsed, [{"section": "c", "replace": "…"}, {"section": "a1", "replace": "…"}],
                         ["a1", "a", "b"], read)
    assert edits == [{"section": "a", "replace": parsed.outer("a")}, {"section": "c", "replace": parsed.outer("c")}]
    assert pages.resend(parsed, [{"section": "b", "replace": "…"}], ["b"], read) is None
    assert pages.resend(parsed, [], ["a", "c"], {**read, "missing": ["c"]}) == pages.WHOLE


# ----- tr-n09qr: the service's changedSections are {id, change} objects ------------------------------------


def test_a_conflict_whose_changed_sections_are_id_change_objects_resends_by_handle_id(
        trk, trk_home, published, fake, monkeypatch):
    real = fake_artifacts.changed_sections
    monkeypatch.setattr(fake_artifacts, "changed_sections",
                        lambda old, new: [{"id": h, "change": "modified"} for h in real(old, new)])
    j = published["J"]
    current = rewrite(fake, j["slug"], "solution", "Edited in the web UI.")
    tracker_change(trk, j["id"])
    flush(trk)
    stale, resend = writes_to(fake, j["slug"])
    (read,) = fake.tool_calls("get_artifact")
    assert read == {"slug": j["slug"], "handles": ["solution"]}
    assert resend["baseVersion"] == current and "body" not in resend
    assert sorted(e["section"] for e in resend["edits"]) == ["problem", "solution"]
    assert fake.body(j["slug"]) == expected(trk_home, j["id"])
    assert page_row(trk_home, j["id"])["last_error"] is None


def test_changed_handles_accepts_objects_and_plain_strings():
    assert flusher._changed_handles([{"id": "a", "change": "added"}, "b", {"id": "a", "change": "x"}]) == ["a", "b"]
    assert flusher._changed_handles(None) == []


def test_an_unexpected_error_publishing_one_page_is_recorded_and_the_flush_goes_on(
        trk, trk_home, published, fake, monkeypatch):
    j, t = published["J"], published["T"]
    real = flusher._update

    def update(st, client, item_id, *args):
        if item_id == j["id"]:
            raise TypeError("unhashable type: 'dict'")
        return real(st, client, item_id, *args)

    monkeypatch.setattr(flusher, "_update", update)
    tracker_change(trk, j["id"])
    ok(trk, "edit", t["id"], "--title", "T renamed")
    report = flush(trk)
    assert j["id"] in report["failed"] and t["id"] in report["updated"]
    assert page_row(trk_home, j["id"])["last_error"] == "TypeError: unhashable type: 'dict'"
    assert page_row(trk_home, t["id"])["last_error"] is None
    assert writes_to(fake, t["slug"])


# ----- tr-n1t8h: the service refuses a title over 200 characters -------------------------------------------


LONG = "Deferred: " + "a very long ticket title that runs on " * 8


def test_page_title_cuts_at_a_word_boundary_with_an_ellipsis():
    assert flusher.page_title("short") == "short"
    assert flusher.page_title("x" * 200) == "x" * 200
    capped = flusher.page_title(LONG)
    assert len(capped) <= 200 and capped.endswith("…") and not capped[:-1].endswith(" ")
    assert LONG.startswith(capped[:-1]) and LONG[len(capped) - 1] == " "
    assert len(flusher.page_title("y" * 300)) == 200  # no space to cut at


def test_a_title_over_200_characters_publishes_capped_and_the_body_keeps_it_whole(
        trk, trk_home, published, fake, monkeypatch):
    j = published["J"]
    assert len(LONG) > 200
    ok(trk, "edit", j["id"], "--title", LONG)
    flush(trk)  # the rename goes through update_artifact
    art = fake.artifacts[j["slug"]]
    assert len(art["title"]) <= 200 and art["title"] == flusher.page_title(LONG)
    assert LONG in fake.body(j["slug"])
    row = page_row(trk_home, j["id"])
    assert row["last_error"] is None and j["id"] not in dirty(trk_home)
