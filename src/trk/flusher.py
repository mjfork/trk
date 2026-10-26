"""`trk pages flush`: publish the pages that are due (Page publisher spec: First publish, Flusher, Size).

Loaded only by the `pages` verbs that send. A page is due when it has never been published (no
`pages` row, no slug, or a publish that may not have finished), or when it was marked and its time
has come; `--now` makes every dirty page due. Due pages go in depth order, ancestors first, so a
breadcrumb links up wherever the ancestor already has a page. A flush keeps passing over the due
pages until a pass creates no new page and leaves none due at once: each first publish journals
`page.created` and marks the item's own page, its parent and its descendants due at once, so the run
that creates a subtree's pages also turns their plain-text cross-links into links, the page's links to
itself included.

Each page is one unit of work under `<store>/pages.lock` (an flock). The lock is always taken
before any SQLite transaction, never inside one, and is held through the unit's bookkeeping so no
second flusher sends the same page meanwhile. No network call happens inside a SQLite transaction:
the render reads a snapshot that is closed before the call, and the bookkeeping that follows a call
is its own short `BEGIN IMMEDIATE`.

Every flush starts by marking (Affected pages): under the lock, the journal rows past
`publish_cursor` mark the pages they affect, due about 60 s after the row and never more than 5
minutes after the first mark.

A new page is published with `team` and the tag `trk:<id>`, after its row is set to `publishing`.
`publish` is never blindly retried: after an ambiguous failure, and on any later run that
finds a `publishing` row, the page is looked for by its tag and adopted. An existing page gets one
write per pass on `baseVersion`: the outermost handles whose shells changed as one patch, or the
whole body when the root shell changed or the patch would pass 50 edits, with the title when it
changed. One that renders the same is not written.

Every write carries `baseVersion`. A `conflict` refusal means something else wrote the page: the
changed handles (and only they) are read with `sections`, and every one whose server source
differs from the render goes again on `currentVersion`, with the refused edits the other writer
didn't touch. A changed section that isn't the tracker's, or a handle missing on the server, sends
the whole body instead. That is the one retry per flush: a second conflict fails the page with
`last_error: conflict`, and it stays dirty. `trk pages republish` sends the whole body on the
current version whatever the stored hashes say.

Before a page whose render drops an `ask-<id>` handle is sent, a pull pass runs under the same lock
(Pull-before-drop), so every comment on that box is read and replied to while the box still shows;
a page whose own comments could not be listed is failed instead, and sent once they can be.

Every flush starts with the credentials check (see `renewal`): while an exit 2 holds page writes it
marks but sends nothing, and an exit 2 during the run ends it and sets the hold.
"""

import fcntl
import time
from contextlib import contextmanager
from datetime import timedelta

from . import clock, items, needs_you, pages, publisher, render, renewal, spawn, top
from .errors import refused
from .store import PUBLISHER

LOCK_NAME = "pages.lock"
MAX_BODY_BYTES = 2 * 1024 * 1024
WAIT_CAP = timedelta(minutes=5)  # a page that never goes quiet is still sent this long after its first mark
FAILED_PAUSE = timedelta(minutes=5)  # a failed page is due no sooner than this after the failure
MIN_SLEEP = 1.0  # seconds: `flush --wait` never spins, even on a page due now that a pass left dirty
_sleep = time.sleep  # tests swap this for one that moves $TRK_NOW

# What a unit of work did to its page, in the order the report lists them.
PUBLISHED, UPDATED, UNCHANGED, FAILED = OUTCOMES = ("published", "updated", "unchanged", "failed")


class _Stop(Exception):
    """The token was rejected: the run ends, and every page stays dirty."""


@contextmanager
def pages_lock(st):
    """The publisher's network lock, one unit of work at a time. Never taken inside a SQLite
    transaction: taking it there could deadlock against a unit waiting for the write lock."""
    if st.conn.in_transaction:
        raise RuntimeError("pages.lock taken inside an open SQLite transaction")
    with open(st.dir / LOCK_NAME, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def flush(st, client, *, now_all=False, only=None):
    """Publish every due page (only those in `only`, when given); returns {outcome: [item ids]} and the
    token check's fields: `held`, `renewal_ask` and, when the ask could not be opened, `renewal_ask_error`."""
    report = {outcome: [] for outcome in OUTCOMES}
    _flush(st, client, report, now_all=now_all, only=only)
    return report


def _flush(st, client, report, *, now_all, only):
    """One flush into `report`; False when the token is held or was rejected, and the run must end."""
    failed, failed_replies = set(), set()
    with pages_lock(st):
        pages.mark(st)
    report.update(renewal.check(st, client))
    if report["held"]:
        return False  # an exit 2 holds every page write until setup runs again
    try:
        while due := due_pages(st.conn, now_all=now_all, only=only, skip=failed):
            for item_id in due:
                outcome = _send(st, client, item_id, now_all=now_all, failed_replies=failed_replies)
                if outcome == FAILED:
                    failed.add(item_id)
                if outcome is not None:
                    report[outcome].append(item_id)
    except _Stop:
        report.update(renewal.rejected(st, client))
        return False
    return True


def flush_wait(st, client, *, only=None, sleep=None):
    """`flush --wait`: register in flusher.pid, then loop: flush, mark what came in meanwhile, and
    sleep until the earliest moment a page that hasn't failed falls due. While an exit 2 holds page
    writes, or after one, unregister and exit. With nothing left dirty but failed pages (left
    to the timer), unregister; then, if the journal head has moved past `publish_cursor`,
    register and loop again, since a write that committed while this flusher was finishing found
    it still registered and started none. Exits at once, with an empty report, when another live
    flusher is registered. pages.lock is held only per unit of work, never while sleeping.
    Returns flush's report over the whole run."""
    sleep = _sleep if sleep is None else sleep
    report = {**{outcome: [] for outcome in OUTCOMES}, "held": False, "renewal_ask": None}
    while spawn.register(st.dir):
        try:
            while authorized := _flush(st, client, report, now_all=False, only=only):
                with pages_lock(st):
                    pages.mark(st)  # rows that committed during the pass: their writers saw this flusher
                moment = next_due(st.conn, only=only)
                if moment is None:
                    break
                sleep(max((moment - clock.now()).total_seconds(), MIN_SLEEP))
        finally:
            spawn.unregister(st.dir)
        if not authorized or not _behind(st):
            break  # held: nothing can be sent until setup has a new token, so no second round
    return report


def _behind(st):
    """Whether the journal has rows past `publish_cursor`: a write the flusher has not yet marked."""
    head = st.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM journal").fetchone()[0]
    return head > int(st.meta("publish_cursor") or 0)


def next_due(conn, *, only=None):
    """The earliest moment a dirty page whose last send hasn't failed falls due (now, for one never
    published), or None when every dirty page has failed or none is dirty. A failed send stamps
    `last_error_at`; a pull's error on `last_error` alone doesn't hold the page back."""
    now = clock.now()
    moments = []
    for item, _level, page in pages.page_rows(conn):
        if (only and item["id"] not in only) or not items.page_dirty(page) or (page and page["last_error_at"]):
            continue
        moments.append(_due_moment(page) or now)
    return min(moments, default=None)


def due_pages(conn, *, now_all, only, skip=()):
    """The page-bearing items due now: first publishes before updates, so an update in the same
    pass already links the new pages; each group ancestors first, in tree order within a depth."""
    now = clock.now()
    found = [(not items.page_unpublished(page), level, item["id"]) for item, level, page in pages.page_rows(conn)
             if item["id"] not in skip and (not only or item["id"] in only) and is_due(page, now, now_all)]
    return [item_id for *_, item_id in sorted(found, key=lambda entry: entry[:2])]


def is_due(page, now, now_all):
    """Never published (or a publish that may not have finished): due at once. Marked: due at the
    earlier of `due_at` and `first_dirty_at` + WAIT_CAP. A failed page waits FAILED_PAUSE from its
    failure. `now_all` makes every dirty page due, failed ones included."""
    if not items.page_dirty(page):
        return False
    if now_all or page is None:
        return True
    moment = _due_moment(page)
    if page["last_error_at"]:
        retry = clock.parse(page["last_error_at"]) + FAILED_PAUSE
        moment = retry if moment is None else max(moment, retry)
    return moment is None or moment <= now


def _due_moment(page):
    """When a dirty page falls due, failures aside: None (at once) for one never published, else the
    earlier of `due_at` and `first_dirty_at` + WAIT_CAP."""
    if page is None or items.page_unpublished(page):
        return None
    moments = [clock.parse(page["due_at"])]
    if page["first_dirty_at"]:
        moments.append(clock.parse(page["first_dirty_at"]) + WAIT_CAP)
    return min(moments)


def _row(conn, item_id):
    return conn.execute("SELECT * FROM pages WHERE item_id = ?", (item_id,)).fetchone()


def _send(st, client, item_id, *, now_all, failed_replies):
    """One page's unit of work: its outcome, or None when another process already sent it.
    `failed_replies` gathers the replies pull-before-drop passes failed during this flush."""
    with pages_lock(st):
        page = _row(st.conn, item_id)  # read again under the lock: another flusher may have sent it
        if not is_due(page, clock.now(), now_all):
            return None
        try:
            title, html, hashes, error = _render(st, client, item_id, page, failed_replies)
            if error:
                _failed(st, item_id, error)
                return FAILED
            if items.page_unpublished(page):
                return _first_publish(st, client, item_id, page, title, html, hashes)
            return _update(st, client, item_id, page, title, html, hashes)
        except publisher.Held as exc:
            _failed(st, item_id, str(exc))
            raise _Stop() from None
        except publisher.ServiceError as exc:
            _failed(st, item_id, str(exc))
            return FAILED
        except Exception as exc:  # noqa: BLE001 one page's bug must not end the flush with last_error null
            _failed(st, item_id, f"{type(exc).__name__}: {exc}")
            return FAILED


def _render(st, client, item_id, page, failed_replies):
    """(title, render, its shell hashes, None), or the error that fails the page. When the render drops an
    `ask-<id>` handle the published page shows, a pull pass runs first (spec: Pull-before-drop), so every
    comment written on that box is read and replied to before the box leaves the page; the
    render is then taken again, since the pass may have answered an ask. The page fails instead, and
    is retried after the failure pause, when the pass could not read its comments (neither from the
    activity log nor by listing), or when a reply on a
    comment on a dropped box is still pending or was failed by a pass in this flush (`failed_replies`,
    which this adds to), so a box stays on every page while its reply has just failed."""
    title, html = _snapshot(st, item_id)
    hashes, error = _preflight(html)
    if error or items.page_unpublished(page) or not client.COMMENTS or not _dropped_asks(st.conn, item_id, hashes):
        return title, html, hashes, error
    from . import pull  # pull imports this module

    report = pull.before_drop(st, client)
    failed_replies.update(report["replies"]["failed"])
    if item_id in report["unread"]:
        errors = [e["error"] for e in report["errors"] if e["item"] == item_id] or ["its comments were not read"]
        return title, html, hashes, f"pull-before-drop: {errors[-1]}"
    title, html = _snapshot(st, item_id)
    hashes, error = _preflight(html)
    if error:
        return title, html, hashes, error
    unsent = pull.unsent_replies(st.conn, _dropped_asks(st.conn, item_id, hashes), failed_replies)
    for row, state in unsent:
        if state == "pending":
            return title, html, hashes, f"pull-before-drop: the reply to comment {row['comment_id']} is not sent " \
                                        f"yet; the box stays until it is"
    if unsent:
        row = unsent[0][0]
        reason = (pages.reply_failures(st.conn).get(str(row["id"])) or {}).get("reason") or "refused"
        return title, html, hashes, (f"pull-before-drop: the reply to comment {row['comment_id']} was refused for good "
                                     f"({reason}) and is not sent again; the next flush drops the box anyway. "
                                     f"`trk pages --errors` lists reply {row['id']} until `trk pages reply resend` "
                                     f"or `dismiss` settles it")
    return title, html, hashes, None


def _dropped_asks(conn, item_id, hashes):
    """The asks whose `ask-<id>` handle the page last published and this render no longer has."""
    return {handle[len("ask-"):] for handle in pages.stored_hashes(conn, item_id)
            if handle.startswith("ask-") and handle not in hashes}


def _preflight(html):
    """(the render's shell hashes, None), or (None, the error) for a render too large to send or
    malformed; checked before any call, so a bad render costs its own page only."""
    if len(html.encode("utf-8")) > MAX_BODY_BYTES:
        return None, "too-large"
    try:
        return pages.hashes(html), None
    except ValueError as exc:
        return None, f"render: {exc}"


MAX_TITLE = 200  # the longest title a page adapter need take


def page_title(title):
    """`title` as an adapter takes it: at most MAX_TITLE characters, cut at a word boundary with an
    ellipsis. The page body keeps the whole title."""
    if len(title) <= MAX_TITLE:
        return title
    cut = title[:MAX_TITLE - 1]
    if not title[MAX_TITLE - 1].isspace() and " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip() + "\u2026"


def _snapshot(st, item_id):
    """The item's title (as `page_title` caps it) and render, read in one read transaction that ends
    before any network call."""
    st.conn.execute("BEGIN")
    try:
        if top.is_key(item_id):
            return page_title(top.title(st.conn)), top.render_page(st.conn)
        if needs_you.is_key(item_id):
            actor_id = needs_you.actor_of(item_id)
            return page_title(needs_you.title(st.conn, actor_id)), needs_you.render_page(st.conn, actor_id)
        return page_title(items.get(st.conn, item_id)["title"]), render.render(st.conn, item_id)
    finally:
        st.conn.execute("COMMIT")


def _first_publish(st, client, item_id, page, title, html, hashes):
    team = st.meta("publish_team")
    if not team:
        _failed(st, item_id, "no team to publish on: run `trk pages setup --team <slug>`; a page born "
                             "without one would be private for good")
        return FAILED
    was_publishing = page is not None and page["state"] == "publishing"
    if was_publishing:
        # An earlier run started this publish and never recorded how it ended: look before sending again.
        found = client.find_tagged(tag(item_id))
        if found is not None:
            return _adopt(st, client, item_id, found, title, html, hashes, known_body=False)
    _set_state(st, item_id, "publishing")
    try:
        result = client.publish(title=title, body=html, team=team, tags=[tag(item_id)])
    except publisher.ServiceError as exc:
        if not getattr(exc, "ambiguous", False) or isinstance(exc, publisher.Held):
            if not was_publishing:  # refused outright, and no earlier attempt may still turn up
                _set_state(st, item_id, "live")
            raise
        found = client.find_tagged(tag(item_id))
        if found is None:
            raise  # the row stays `publishing`: the next run looks again before publishing
        # Only this unit's own publish can be a version-1 page found now, unless an earlier
        # attempt's page turned up late; then it may hold an older render.
        return _adopt(st, client, item_id, found, title, html, hashes,
                      known_body=found["version"] == 1 and not was_publishing)
    _published(st, item_id, result["slug"], result["url"], result["version"], hashes, title, created=True)
    return PUBLISHED


def _adopt(st, client, item_id, found, title, html, hashes, *, known_body):
    """Take over a page found by its tag. Unless it is known to hold this render, send the whole
    body, and the title with it."""
    version = found["version"]
    if not known_body:
        version = client.update(found["slug"], base_version=version, body=html, title=title)["version"]
    _published(st, item_id, found["slug"], found["url"], version, hashes, title, created=True)
    return PUBLISHED


def _update(st, client, item_id, page, title, html, hashes):
    """One write for a published page: the outermost changed handles as one patch, or the whole
    body when the root shell changed or the patch is too long, plus the title when it changed
    (alone, when nothing else did). A page that renders the same is not written."""
    stored = pages.stored_hashes(st.conn, item_id)
    change = pages.patch(html, stored)
    new_title = title if stored.get(pages.TITLE) != pages.title_hash(title) else None
    if change is None and new_title is None:
        _clean(st, item_id)
        return UNCHANGED
    version = _send_change(client, page, html, change, new_title)
    _published(st, item_id, page["slug"], page["url"], version, hashes, title, created=False)
    return UPDATED


def republish(st, client, item_id):
    """`trk pages republish`: the whole body and the title, whatever the stored hashes say, which are
    then refreshed. It goes on the stored version, and on `currentVersion` after a conflict. A repair a person runs, so a failure is recorded on the
    page and refused (rule `republish-failed`) rather than left for `trk pages --errors`. While an exit 2
    holds page writes it is refused before any call, and an exit 2 to it sets the hold and opens the
    renewal ask as a flush's would."""
    if renewal.held(st):
        ask = renewal.ensure_ask(st, token_rejected=True)["renewal_ask"]
        raise refused("republish-failed", f"page writes are held: the page adapter refused (exit 2); run "
                                          f"`trk pages setup` once it is fixed ({ask or 'no renewal ask'})", item=item_id)
    with pages_lock(st):
        page = _row(st.conn, item_id)
        if items.page_unpublished(page):
            raise refused("no-page", f"{item_id} has no published page to republish: "
                                     f"`trk pages flush --now --item {item_id}` publishes it", item=item_id)
        try:
            title, html, hashes, error = _render(st, client, item_id, page, set())
            if error is None:
                version = _send_change(client, page, html, pages.WHOLE, title)
        except publisher.Held as exc:
            _failed(st, item_id, str(exc))
            ask = renewal.rejected(st, client)["renewal_ask"]
            raise refused("republish-failed", f"republishing {item_id} failed: {exc}; renewal ask: "
                                              f"{ask or 'none opened'}", item=item_id) from None
        except publisher.ServiceError as exc:
            error = str(exc)
        if error:
            _failed(st, item_id, error)
            raise refused("republish-failed", f"republishing {item_id} failed: {error}", item=item_id)
        _published(st, item_id, page["slug"], page["url"], version, hashes, title, created=False)
        return {"item": item_id, "slug": page["slug"], "version": version}


def _send_change(client, page, html, change, title):
    """One `update` on the page's stored version: `change` (WHOLE, edits or None) and the title
    when given; its new version. A conflict (spec: Conflicts) means someone else wrote the page,
    and the change is sent once more on the current version, with only the changed handles read; a
    second conflict fails the page with `last_error: conflict`."""
    try:
        return _update_once(client, page["slug"], page["version"], html, change, title)
    except publisher.Refused as exc:
        if exc.error != "conflict" or not isinstance((exc.payload or {}).get("currentVersion"), int):
            raise
        conflict = exc.payload
    current, changed = conflict["currentVersion"], _changed_handles(conflict.get("changedSections"))
    if change != pages.WHOLE:
        parsed = pages.parse(html)
        if not changed or any(handle not in parsed.spans for handle in changed):
            # Someone else's section, or a change outside every handle (the head, the layout): only
            # the whole body puts the page back.
            change = pages.WHOLE
        else:
            change = pages.resend(parsed, change or [], changed, client.sections(page["slug"], handles=changed))
    if change is None and title is None:
        return current  # the server already holds this render: adopt its version, write nothing
    try:
        return _update_once(client, page["slug"], current, html, change, title)
    except publisher.Refused as exc:
        if exc.error == "conflict":
            raise publisher.ServiceError("conflict") from None
        raise


def _changed_handles(sections):
    """The handle ids of a conflict's `changedSections`: an adapter may send `{id, change}` objects, and an
    entry that is a plain string is already an id."""
    handles = []
    for entry in sections or []:
        handle = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(handle, str) and handle not in handles:
            handles.append(handle)
    return handles


def _update_once(client, slug, base_version, html, change, title):
    content = {} if change is None else {"body": html} if change == pages.WHOLE else {"edits": change}
    return client.update(slug, base_version=base_version, title=title, **content)["version"]


def tag(item_id):
    return f"trk:{item_id}"


# ----- bookkeeping: each its own short BEGIN IMMEDIATE, after the call --------------------------------


def _ensure_row(conn, item_id):
    conn.execute("INSERT INTO pages (item_id) VALUES (?) ON CONFLICT (item_id) DO NOTHING", (item_id,))


def _set_state(st, item_id, state):
    with st.write_lock():
        _ensure_row(st.conn, item_id)
        st.conn.execute("UPDATE pages SET state = ? WHERE item_id = ?", (state, item_id))


def _failed(st, item_id, error):
    """Record a failed attempt on the page. It keeps its marks and its state, and stays dirty."""
    with st.write_lock():
        _ensure_row(st.conn, item_id)
        st.conn.execute("UPDATE pages SET last_error = ?, last_error_at = ? WHERE item_id = ?",
                        (error, clock.stamp(clock.now()), item_id))


def _clean(st, item_id):
    with st.write_lock():
        st.conn.execute("UPDATE pages SET due_at = NULL, first_dirty_at = NULL, last_error = NULL, "
                        "last_error_at = NULL WHERE item_id = ?", (item_id,))


def _published(st, item_id, slug, url, version, hashes, title, *, created):
    """A successful send: the new version, the render's shell hashes and the title's. A first publish also
    journals `page.created` (actor `publisher`) and marks the page itself, the parent and every
    descendant due at once, since their plain-text links to this page can now become links (the page's
    own ask boxes say "On: <it>"), and so does every person's Needs-you page. A page with no link to
    itself renders the same on that pass and is not written. Only that row is journaled; the rest is
    unjournaled publisher bookkeeping. A person's page is no item, so its first publish journals nothing;
    it marks every published page due at once instead, itself included, since any ask box can name the
    person and every top-level page links each person's page. Those that don't render the same and
    are not written."""
    # Neither a person's page nor the top-level page is an item: no `page.created` row for either.
    if not created or needs_you.is_key(item_id) or top.is_key(item_id):
        with st.write_lock():
            now = clock.stamp(clock.now())
            if _dropped_asks(st.conn, item_id, hashes):  # the next pull lists the page once more
                pages.set_reread(st.conn, pages.reread(st.conn) | {item_id})
            _record(st.conn, item_id, slug, url, version, hashes, title, now)
            if created:
                st.conn.execute("UPDATE pages SET due_at = ?, first_dirty_at = COALESCE(first_dirty_at, ?) "
                                "WHERE slug IS NOT NULL AND (due_at IS NULL OR due_at > ?)", (now, now, now))
        return
    with st.transaction(PUBLISHER) as tx:
        conn, now = tx.conn, clock.stamp(tx.now)
        _record(conn, item_id, slug, url, version, hashes, title, now)
        tx.journal("page.created", items.feed_fields(conn, item_id), data={"slug": slug, "url": url})
        parent = items.get(conn, item_id)["parent_id"]
        below = [row["id"] for row, _ in items.outline(conn, item_id, include_closed=True)[1:]]
        for other in [item_id] + ([parent] if parent else []) + below + needs_you.page_keys(conn) + [top.KEY]:
            conn.execute("UPDATE pages SET due_at = ?, first_dirty_at = COALESCE(first_dirty_at, ?) "
                         "WHERE item_id = ? AND (due_at IS NULL OR due_at > ?)", (now, now, other, now))


def _record(conn, item_id, slug, url, version, hashes, title, now):
    _ensure_row(conn, item_id)
    conn.execute("UPDATE pages SET slug = ?, url = ?, version = ?, state = 'live', published_at = ?, due_at = NULL, "
                 "first_dirty_at = NULL, last_error = NULL, last_error_at = NULL WHERE item_id = ?",
                 (slug, url, version, now, item_id))
    conn.execute("DELETE FROM page_handles WHERE item_id = ?", (item_id,))
    conn.executemany("INSERT INTO page_handles (item_id, handle, shell_hash) VALUES (?, ?, ?)",
                     [(item_id, handle, digest)
                      for handle, digest in sorted({**hashes, pages.TITLE: pages.title_hash(title)}.items())])
