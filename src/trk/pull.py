"""`trk pages pull`: comments as answers, and the reply outbox (Page publisher spec: Comments as answers).

Loaded only by `pages pull` and `pages tick`. One pull is one unit of work under `<store>/pages.lock`,
so two pulls never interleave. It starts with the token check (see `renewal`): while a 401 holds page
writes it calls nothing, and a 401 during the pass ends it and sets the hold.

1. **Read.** When `tools/list` offers `list_comment_activity` (service change 2), one cross-page
   activity log from `meta.comment_cursor`, paged while `hasMore`: its `comment` events on tracker
   pages are the new root comments, and its `reply` and `status` events (the tracker's own replies, and
   resolves whose author fields describe the root's author) are never read. With no cursor stored, or
   one the service calls `invalid_cursor`, the log is drained once to seed it and none of it is acted
   on. Otherwise, and also on a seeding pass or an activity call that fails (so nothing written before the
   seed or during the failure goes unread), `list_artifact_comments` for every published page that
   shows an open ask (an open ask sits in its subtree) or showed one in its last publish (an `ask-`
   handle in `page_handles`), and once more a page whose last send dropped an `ask-` handle
   (`pages.reread`). The cursor is stored after the pass's dispositions commit.
2. **Dispose.** Every root comment without a row in `page_comments`, across every page, oldest first
   by `createdAt`, gets one disposition. Each is one transaction: the check that the comment has no
   disposition yet, the store change it causes (a reply, a commenter's registration), the
   `page_comments` row and its outbox reply, so a comment is never acted on twice and a reply is never
   lost. Only `waiting` and `ask-<id>` handles answer asks; an assistant's or the tracker's own root comment is
   `ignored`.
   - A person's comment anywhere else (a section, a row, the page itself; on a Needs-you page, anywhere
     off a box) is `routed` (tr-yd424): journaled `comment.routed` to the item's effective owner (the
     page's person on a Needs-you page), told "Sent to <owner>; they'll reply here.", the thread left open.
     `trk show` lists it until `trk comment ack`; `trk comment reply` answers in the thread (`comments.py`).
   - An open ask's comment, created after the ask's latest `ask.opened` or `ask.reopened`, is a reply
     (`ask.replied`, the ask left open) by the actor the comment's `authorId` is linked to (an unlinked one
     is linked to the one unlinked human with the same display name, else registered as a human, and the
     reply then tells an admin to merge them with `trk actor link` when a human of that name exists), told "Reply received…"; the asker confirms one as the answer with
     `trk answer --from-comment` (tr-3vgrb). Without `authorId` on the wire (before service change 1) it is
     pointed at `trk answer`.
   - A later comment on an answered ask gets "Already answered…", on a withdrawn one "Withdrawn…".
   - A `waiting` comment replies to the box's one ask; with none or several it is told so and stays open.
3. **Send.** Every `pending` outbox row goes out, a reopen's "Reopened by…" among them. No reply resolves
   its thread: the person who commented resolves it, and that changes nothing here. A refusal marks the
   row `failed`, recorded on its page's `last_error`. An ambiguous failure leaves the row `pending`;
   before it is sent again the thread is read, and a reply already there with the same body marks it `sent`.

4. **Close.** When an ask has closed (answered or withdrawn) and its box has left the page (no
   `ask-<id>` row in `page_handles`), each root comment on that box gets one closing reply, "Closed:
   <outcome>. Recorded on <ask title> (<id>)", sent with `resolve: true`, so the thread resolves in the same
   call (the token has no `resolve_artifact_comment`). It goes through the outbox: the reply is a
   `page_replies` row queued with a `page_closings` marker in one transaction, so it is never queued or sent
   twice; a result that did not resolve, or `not_found`, is final (marker `gone`), and one whose ask was
   reopened before it was sent is dropped. It is queued after the pass sends its other replies, and never on a
   thread that still has an unsent reply, so it is the last word on the thread. Only asks that closed after the pull first ran with this step
   (`closings_from`) are closed by a pull; `tidy` does the rest of a page, on request.

The flusher runs the same pass (`before_drop`) before it sends a page whose render drops an `ask-<id>`
handle, so a comment written on a box before the box leaves its page is still read and replied to.
A comment's `status` (`open`, `outdated` when a re-render drops its handle, `resolved`) plays no part
in its disposition: each root comment is disposed of once, by id, whatever its status then or later.

The page bookkeeping (`page_comments`, `page_replies`, `meta.page_authors`) is publisher state and is
not journaled; replies and registrations are, through their normal rows.
"""

import json

from . import actors, artifacts, asks, clock, comments, items, needs_you, pages, render, renewal
from .errors import TrkError, refused
from .flusher import pages_lock
from .store import PUBLISHER

WAITING = "waiting"
ASK_PREFIX = "ask-"
AUTHORS_KEY = "page_authors"  # {author id: display name} for every author the pull has seen
CURSOR_KEY = "comment_cursor"  # the comment activity log's cursor, opaque, as the service gave it
ACTIVITY = "list_comment_activity"
ACTIVITY_LIMIT = 500  # the service's maximum, so a drain takes as few calls as it can
CLOSINGS_KEY = "page_closings"  # {comment id: {reply: outbox row id, state: queued | resolved | gone, handle: its ask-<id> box}}
CLOSINGS_FROM_KEY = "closings_from"  # stamp: a pull closes the threads of asks closed from then on
START = ""  # the cursor stored by a seed that was given none (an empty log): read from the oldest event

# `answer` is no longer written (a comment is a reply the asker confirms, tr-3vgrb); older rows keep it.
REPLY, ALREADY_ANSWERED, WITHDRAWN_ASK, WHICH_ASK, USE_CLI, IGNORED, ROUTED = (
    "reply", "already-answered", "withdrawn", "which-ask", "use-cli", "ignored", "routed")

WHICH = "Which ask? Comment on its box instead."
NOTHING = "Nothing is waiting here."


def pull(st, client):
    """One pull pass: read the comments, dispose of the new ones, send the outbox. Returns the report:
    the token check's fields (`held`, `renewal_ask`, maybe `renewal_ask_error`), `activity` (null when the
    service offers none, else {seeded, events}), `comments` (one entry per disposition made), `replies`
    ({sent, failed, pending}: comment ids), `resolved` (comment ids of the threads resolved) and `errors`
    (per page)."""
    report = {"held": False, "renewal_ask": None, "activity": None, "comments": [],
              "replies": {"sent": [], "failed": [], "pending": []}, "resolved": [], "errors": []}
    report.update(renewal.check(st))
    if report["held"]:
        return report  # a 401 holds every call until setup has a new token
    try:
        with pages_lock(st):
            _pass(st, client, report)
    except artifacts.Unauthorized:
        report.update(renewal.rejected(st, client))
    return report


def before_drop(st, client):
    """The pull-before-drop pass the flusher runs, under the pages.lock it already holds, before it sends a
    page whose render drops an `ask-<id>` handle: no token check (the flush made it) and no 401 handling (a
    401 propagates to the flush). Returns the pass's `comments`, `replies` and `errors`, and `unread`: the
    item ids of the pages whose comments it could not read (none when the activity log was read whole)."""
    report = {"activity": None, "comments": [], "replies": {"sent": [], "failed": [], "pending": []},
              "resolved": [], "errors": [], "unread": []}
    _pass(st, client, report)
    return report


def unsent_replies(conn, ask_ids, failed_now=()):
    """(outbox row, state) of each reply, on comments on any of `ask_ids`' boxes, that is still `pending`,
    or failed in a pass of the running flush (`failed_now`: comment ids): pull-before-drop holds the page
    back for them. A row failed before this flush is final (it is never sent again by itself, and
    `trk pages --errors` lists it until it is resent or dismissed), so it holds nothing. A closing reply
    (step 4) holds nothing either: it is queued only once its box has left its page, so there is no comment
    on it left to read."""
    if not ask_ids:
        return []
    marks = ", ".join("?" * len(ask_ids))
    rows = conn.execute(f"SELECT r.* FROM page_replies r JOIN page_comments c USING (comment_id) "
                        f"WHERE c.ask_id IN ({marks}) ORDER BY r.id", sorted(ask_ids)).fetchall()
    closing = {mark["reply"] for mark in closings(conn).values()}
    return [(r, r["state"]) for r in rows if r["id"] not in closing
            and (r["state"] == "pending" or r["comment_id"] in failed_now)]


def _pass(st, client, report):
    offered = _offers_activity(st, client, report)
    found, cursor, per_page = _read_activity(st, client, report) if offered else ([], None, True)
    threads = _read(st, client, report) if per_page else {}
    if per_page:
        report["unread"] = [item_id for item_id, slug in pages_to_read(st.conn) if slug not in threads]
    found += [(item_id, slug, comment) for slug, (item_id, comments) in threads.items() for comment in comments]
    queued_before = _last_reply_id(st.conn)
    _dispose_all(st, found, report)
    if cursor is not None:
        _store_cursor(st, cursor)
        if not per_page:
            _done_rereading(st, *pages.reread(st.conn))  # the log held every comment written before the drops
    _send_outbox(st, client, threads, queued_before, report)
    queued_closings = _last_reply_id(st.conn)  # a closing reply goes last: after the thread's earlier replies
    _queue_closings(st)
    _send_outbox(st, client, threads, queued_closings, report, after=queued_closings)


# ----- 1. read --------------------------------------------------------------------------------------


def pages_to_read(conn):
    """(item id, slug) of every published page that shows an open ask or showed one in its last
    publish, in tree order, then the Needs-you pages (keyed `needs-you:<actor>`)."""
    showing = set()
    for row in asks.waiting(conn):
        showing |= {link["id"] for link in items.lineage(conn, row["id"])}
        showing.add(needs_you.key(asks.resolve(conn, row)))  # the Needs-you page of whoever it waits on
    showed = {r[0] for r in conn.execute("SELECT DISTINCT item_id FROM page_handles WHERE handle LIKE 'ask-%'")}
    read = showing | showed | pages.reread(conn)
    return [(item["id"], page["slug"]) for item, _level, page in pages.page_rows(conn)
            if item["id"] in read and not items.page_unpublished(page)]


def _read(st, client, report):
    """{slug: (item id, the page's root comments)} for every page read. A page whose listing fails is
    left out, with the failure on its `last_error`; a 401 is recorded and raised."""
    threads = {}
    for item_id, slug in pages_to_read(st.conn):
        listed = _list(st, client, item_id, slug, report)
        if listed is not None:
            threads[slug] = (item_id, listed)
    return threads


def _list(st, client, item_id, slug, report):
    try:
        comments = client.list_artifact_comments(slug).get("comments") or []
    except artifacts.ServiceError as exc:
        _page_error(st, item_id, str(exc))
        report["errors"].append({"item": item_id, "slug": slug, "error": str(exc)})
        if isinstance(exc, artifacts.Unauthorized):
            raise
        return None
    _clear_pull_error(st, item_id, "list_artifact_comments")
    _done_rereading(st, item_id)
    return comments


def _offers_activity(st, client, report):
    """Whether `tools/list` offers the activity log (service change 2). A `tools/list` that fails is recorded
    as an activity-log failure, and the pass reads page by page."""
    try:
        offered = ACTIVITY in client.tools()
    except artifacts.Unauthorized:
        raise
    except artifacts.ServiceError as exc:
        _activity_error(st, exc, report)
        return False
    _clear_pull_error(st, None, "tools/list")
    return offered


def _read_activity(st, client, report):
    """(the new root comments as (item id, slug, comment), the cursor to store after they are disposed of
    or None, whether the per-page read runs too). A seeding pass acts on none of the log, and stores its
    cursor only when the drain finished; a failure part way keeps the events read before it."""
    stored = st.meta(CURSOR_KEY)
    seeded = stored is None
    events, cursor, failure, starts = _drain(client, stored or None)
    if not seeded and isinstance(failure, artifacts.ToolError) and failure.error == "invalid_cursor":
        _activity_error(st, failure, report)  # re-seed rather than replay: the log from the start is history
        seeded, (events, cursor, failure, starts) = True, _drain(client, None)
    if failure is not None:
        _activity_error(st, failure, report)
    elif not report["errors"]:
        _clear_pull_error(st, None, ACTIVITY)
    if seeded and failure is not None:
        cursor = None  # a seed half drained would leave history to be read as new: seed again next pass
    elif cursor is None:
        cursor = START  # an empty log gave no cursor: the seed is done all the same
    elif not seeded:
        cursor = _held(st.conn, events, starts, cursor)
    report["activity"] = {"seeded": seeded, "events": len(events)}
    return ([] if seeded else _activity_comments(st.conn, events)), cursor, seeded or failure is not None


def _drain(client, cursor):
    """(events, the cursor after the last page read, the failure that stopped it or None, and for each page
    read (its first event's index in events, the cursor it was read from)). A 401 is raised."""
    events, starts = [], []
    while True:
        try:
            page = client.list_comment_activity(cursor, limit=ACTIVITY_LIMIT)
        except artifacts.Unauthorized:
            raise
        except artifacts.ServiceError as exc:
            return events, cursor, exc, starts
        starts.append((len(events), cursor))
        events += page.get("events") or []
        moved = page.get("cursor") or cursor
        if not page.get("hasMore") or moved == cursor:
            return events, moved, None, starts
        cursor = moved


def _held(conn, events, starts, cursor):
    """The cursor to store: `cursor`, unless a page's first publish is still in doubt (`publishing`) and a
    comment event names a slug no live page has. That comment may be on the page in doubt, which a later
    flush adopts, so the cursor stays at the start of the log page holding the first such event and the
    next pass reads it again; comments already disposed of are skipped by `page_comments`."""
    if not conn.execute("SELECT 1 FROM pages WHERE state = 'publishing'").fetchone():
        return cursor
    known = _live_pages(conn)
    unknown = next((i for i, event in enumerate(events)
                    if event.get("type") == "comment" and event.get("slug") not in known), None)
    if unknown is None:
        return cursor
    held = next(before for first, before in reversed(starts) if first <= unknown)
    return START if held is None else held


def _live_pages(conn):
    """{slug: item id} of the pages the per-page read may list."""
    return {row["slug"]: row["item_id"] for row in conn.execute("SELECT * FROM pages")
            if not items.page_unpublished(row)}


def _activity_comments(conn, events):
    """The log's `comment` events on tracker pages, as root comments. Replies (the tracker's own among them)
    and status events are never read. A comment the wire doesn't mark `human` reads as no person's, so
    it is `ignored` as an assistant's is; one on another artifact the token's owner owns is passed over."""
    pages_by_slug = _live_pages(conn)
    threads = {}  # root comment id -> (replies, latest status), from the reply and status events after it
    for event in events:
        root = event.get("rootCommentId") or event.get("commentId")
        replies, status = threads.get(root, ([], "open"))
        if event.get("type") == "reply":
            replies = [*replies, {"message": event.get("body"), "authorKind": event.get("authorKind")}]
        elif event.get("type") == "status":
            status = event.get("status") or status
        threads[root] = (replies, status)
    found = []
    for event in events:
        slug = event.get("slug")
        comment_id = event.get("rootCommentId") or event.get("commentId")
        if event.get("type") != "comment" or slug not in pages_by_slug or not comment_id:
            continue
        replies, status = threads[comment_id]
        found.append((pages_by_slug[slug], slug, {
            "id": comment_id, "handle": event.get("handle"), "comment": event.get("body") or "",
            "author": event.get("author"), "authorId": event.get("authorId"),
            "authorKind": event.get("authorKind") or "unknown", "createdAt": event.get("at"),
            "status": status, "replies": replies}))
    return found


def _activity_error(st, exc, report):
    """An activity-log failure goes on the `last_error` of every page the per-page read would list, since
    those are the pages whose comments it was reading."""
    showing = pages_to_read(st.conn)
    for item_id, slug in showing:
        _page_error(st, item_id, str(exc))
        report["errors"].append({"item": item_id, "slug": slug, "error": str(exc)})
    if not showing:
        report["errors"].append({"item": None, "slug": None, "error": str(exc)})


def _store_cursor(st, cursor):
    """The activity log's cursor, in its own short write lock, once the pass's dispositions have committed."""
    if st.meta(CURSOR_KEY) == cursor:
        return
    with st.write_lock():
        pages._set_meta(st.conn, CURSOR_KEY, cursor)


# ----- 2. dispose -----------------------------------------------------------------------------------


def _dispose_all(st, entries, report):
    """Dispose of every (item id, slug, root comment) that has no disposition yet, oldest first."""
    known = {r[0] for r in st.conn.execute("SELECT comment_id FROM page_comments")}
    found = {}
    for item_id, slug, comment in entries:
        if comment.get("id") and comment["id"] not in known:
            found.setdefault(comment["id"], (item_id, slug, comment))
    _note_authors(st, [comment for _item, _slug, comment in found.values()])
    for item_id, slug, comment in sorted(found.values(), key=lambda entry: (_created(entry[2]), entry[2]["id"])):
        entry = _dispose(st, item_id, slug, comment)
        if entry is not None:
            report["comments"].append(entry)


def _created(comment):
    """A comment's `createdAt` as a comparable stamp; one without a readable one counts as written now."""
    raw = comment.get("createdAt")
    if not raw:
        return clock.stamp(clock.now())
    try:
        return clock.stamp(clock.parse(raw))
    except ValueError:
        return clock.stamp(clock.now())


def _dispose(st, item_id, slug, comment):
    """One comment's disposition, its store change and its reply, in one transaction. None when another
    pull disposed of it first."""
    comment_id = comment["id"]
    with st.transaction(PUBLISHER) as tx:
        if tx.conn.execute("SELECT 1 FROM page_comments WHERE comment_id = ?", (comment_id,)).fetchone():
            return None
        disposition, ask_id, reply, resolve = _decide(tx, item_id, slug, comment)
        now = clock.stamp(tx.now)
        body = comment.get("comment") if disposition in (REPLY, ROUTED) else None  # `answer --from-comment` and `show` read it
        tx.conn.execute("INSERT INTO page_comments (comment_id, slug, handle, ask_id, disposition, seen_at, body) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (comment_id, slug, comment.get("handle"), ask_id, disposition, now, body))
        if reply is not None:
            tx.conn.execute("INSERT INTO page_replies (slug, comment_id, body, resolve, created_at) "
                            "VALUES (?, ?, ?, ?, ?)", (slug, comment_id, reply, int(resolve), now))
    return {"comment": comment_id, "slug": slug, "handle": comment.get("handle"), "ask": ask_id,
            "disposition": disposition}


def _decide(tx, item_id, slug, comment):
    """(disposition, ask id or None, reply body or None, resolve) for one new root comment, making the
    store change the disposition causes. A comment that is read (on `waiting` or an ask's box) links its
    author to an actor, registering one when nobody is linked yet."""
    handle = comment.get("handle") or ""
    if comment.get("authorKind", "human") != "human":  # absent before service change 1, when all are people's
        return IGNORED, None, None, False  # only people comment; an assistant's (or unknown) root comment is no answer
    note = actors.merge_note(tx.conn, comment["authorId"], comment.get("author")) if comment.get("authorId") else None
    row = _ask(tx.conn, handle[len(ASK_PREFIX):]) if handle.startswith(ASK_PREFIX) else None
    if handle != WAITING and row is None:
        return _route(tx, item_id, slug, comment)
    if row is None:
        decided = _on_waiting(tx, item_id, slug, comment)
    else:
        decided = _on_ask(tx, row, slug, comment)
    if decided[0] != IGNORED:
        _commenter(tx, comment)  # linked already when it replied
        if note and decided[2]:
            decided = (*decided[:2], f"{decided[2]} {note}", *decided[3:])
    return decided


def _commenter(tx, comment):
    """The actor a comment's author id is linked to, registered now if nobody is; None before service
    change 1, when the wire carries no author id."""
    author_id = comment.get("authorId")
    if not author_id:
        return None
    return actors.by_service_user(tx.conn, author_id) or actors.register_commenter(tx, author_id, comment.get("author"))


def _ask(conn, ask_id):
    return conn.execute("SELECT * FROM items WHERE id = ? AND form = 'ask'", (ask_id,)).fetchone()


def _on_waiting(tx, item_id, slug, comment):
    """The page's waiting box as it stood when the comment was written: the asks in the subtree (on a
    Needs-you page, those addressed to its person then) opened before it and not closed until after it.
    Its one ask takes the comment as if written on its own box (a reply, or "Already answered…"/
    "Withdrawn…" when it has closed since); none or several get a reply and the comment stays open."""
    box = _box(tx.conn, item_id, _created(comment))
    if len(box) == 1:
        return _on_ask(tx, box[0], slug, comment)
    return WHICH_ASK, None, NOTHING if not box else WHICH, False


def _box(conn, item_id, created):
    """The page's waiting box as it stood at `created`: the asks in the subtree (on a Needs-you page, those
    addressed to its person then) opened before it and not closed until after it, in page order."""
    if needs_you.is_key(item_id):
        actor_id = needs_you.actor_of(item_id)
        candidates = [row for row in _all_asks(conn) if needs_you.addressee_at(conn, row, created) == actor_id]
    else:
        candidates = _asks_below(conn, item_id)
    box = [row for row in candidates if _after_opening(conn, row, created)
           and (row["status"] == "open" or (row["closed_at"] or "") > created)]
    now = clock.now()
    return sorted(box, key=lambda row: (-asks.urgency(conn, row, now)["score"], row["id"]))  # as `trk waiting`: score, then id


def _route(tx, item_id, slug, comment):
    """A person's root comment on no ask box (a section, a row, the page itself) goes to the owner: journaled
    `comment.routed` (spec 04, Comments as answers), told "Sent to <owner>; they'll reply here.", the thread
    left open. On a Needs-you page the owner is the page's person. A comment with no text, and one whose
    thread is resolved or already has a reply by an assistant (the tracker's own can't be told from other
    assistants'; as after a store restore rewound `page_comments`), is `ignored`: no row, no reply."""
    if not (comment.get("comment") or "").strip():
        return IGNORED, None, None, False
    if comment.get("status") == "resolved" or any(
            reply.get("authorKind") == "assistant" for reply in comment.get("replies") or []):
        return IGNORED, None, None, False
    owner = needs_you.actor_of(item_id) if needs_you.is_key(item_id) else items.hierarchy(tx.conn, item_id)["effective_owner"]
    comments.route(tx, item_id, slug, comment, _commenter(tx, comment), owner)
    return ROUTED, None, f"Sent to {actors.display(tx.conn, owner)}; they'll reply here.", False


def _asks_below(conn, item_id):
    """Every ask in the item's subtree, open or closed."""
    return [row for row in _all_asks(conn) if any(link["id"] == item_id for link in items.lineage(conn, row["id"]))]


def _all_asks(conn):
    return conn.execute("SELECT * FROM items WHERE form = 'ask' ORDER BY created_at, id").fetchall()


def _after_opening(conn, row, created):
    return created > clock.stamp(clock.parse(asks.opened_at(conn, row)))


def _on_ask(tx, row, slug, comment):
    conn, ask_id = tx.conn, row["id"]
    if row["status"] == "closed":
        return _on_closed(conn, row)
    if not _after_opening(conn, row, _created(comment)):
        return _before_reopening(conn, row)
    if not comment.get("authorId"):  # before service change 1: the wire says nothing about who wrote it
        return USE_CLI, ask_id, f"Answering by comment isn't enabled yet: answer with `trk answer {ask_id}`.", False
    body = comment.get("comment") or ""
    if not body.strip():
        return IGNORED, ask_id, None, False
    actor = _commenter(tx, comment)
    try:
        asks.reply_by_comment(tx, row, actor, body, slug=slug, comment_id=comment["id"])
    except TrkError as exc:
        return USE_CLI, ask_id, f"{exc.message}: answer with `trk answer {ask_id}`.", False
    return REPLY, ask_id, (f"Reply received. {actors.display(conn, row['asker'])} will confirm it as the answer "
                           "or follow up here."), False


def _on_closed(conn, row):
    """A comment on an answered ask (on any page, or after a CLI answer) or on a withdrawn one."""
    if row["resolution"] == "done":
        answerer = _latest(conn, row["id"], "ask.answered")
        return _already_answered(conn, row, answerer["actor"] if answerer else None, row["outcome"])
    return WITHDRAWN_ASK, row["id"], f"Withdrawn: {(row['outcome'] or '').removeprefix(asks.WITHDRAWN)}", False


def _before_reopening(conn, row):
    """A comment on an open ask written before its latest opening: it was written while the ask stood
    answered (it was reopened since), so it gets the reply it would have got then."""
    answered = _latest(conn, row["id"], "ask.answered")
    if answered is None:
        return IGNORED, row["id"], None, False
    return _already_answered(conn, row, answered["actor"], json.loads(answered["data"]).get("outcome"))


def _already_answered(conn, row, answerer, outcome):
    return ALREADY_ANSWERED, row["id"], f"Already answered by {actors.display(conn, answerer)}: {outcome}", False


def _latest(conn, item_id, change):
    return conn.execute("SELECT * FROM journal WHERE item = ? AND change = ? ORDER BY seq DESC LIMIT 1",
                        (item_id, change)).fetchone()


def _note_authors(st, comments):
    """Remember every author id seen, with the display name the service gave, for `trk pages authors`."""
    seen = {c["authorId"]: " ".join((c.get("author") or "").split()) for c in comments
            if c.get("authorId") and c.get("authorKind") != "assistant"}
    if not seen:
        return
    with st.write_lock():
        known = authors_seen(st)
        if all(known.get(author) == display for author, display in seen.items()):
            return
        known.update(seen)
        pages._set_meta(st.conn, AUTHORS_KEY, json.dumps(known, sort_keys=True))


def authors_seen(st):
    try:
        return json.loads(st.meta(AUTHORS_KEY) or "{}")
    except ValueError:
        return {}


def authors(st):
    """`trk pages authors`: every author id the pull has seen, with display name and linked actor."""
    return [{"author_id": author, "display": display, "actor": actors.by_service_user(st.conn, author)}
            for author, display in sorted(authors_seen(st).items())]


# ----- 3. send --------------------------------------------------------------------------------------


def _last_reply_id(conn):
    """The newest outbox row before this pass queues any: rows past it have never been sent."""
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM page_replies").fetchone()[0]


def _send_outbox(st, client, threads, queued_before, report, *, after=0):
    """Every pending reply past row `after`; rows past `queued_before` were queued by this pass and never sent."""
    rows = st.conn.execute("SELECT * FROM page_replies WHERE state = 'pending' AND id > ? ORDER BY id",
                           (after,)).fetchall()
    for row in rows:
        _send(st, client, threads, row, report, queued_now=row["id"] > queued_before)


def _send(st, client, threads, row, report, *, queued_now=False):
    """One pending reply. Before sending one an earlier pass queued, its thread is looked at (as read this
    pass, or read now when its page wasn't): a reply already there with the same body is one an earlier,
    ambiguous send left. One queued by this pass was never sent, so it goes straight out. Only a closing reply
    (its marker, step 4) is sent with `resolve: true`, and it is dropped first if its ask is no longer closed."""
    item_id = _page_item(st.conn, row["slug"])
    resent = (pages.reply_failures(st.conn).get(str(row["id"])) or {}).get("resent")  # a person asked: send it
    queued_now = queued_now or resent
    if row["slug"] not in threads and not queued_now:
        listed = _list(st, client, item_id, row["slug"], report) if item_id else None
        if listed is None and item_id:
            report["replies"]["pending"].append(row["comment_id"])
            return
        threads[row["slug"]] = (item_id, listed or [])
    listed = threads[row["slug"]][1] if row["slug"] in threads else []
    thread = next((c for c in listed if c.get("id") == row["comment_id"]), None)
    already = not resent and thread is not None and any(r.get("message") == row["body"]
                                                        for r in thread.get("replies") or [])
    closing = _closing_of(st.conn, row)  # (comment id, marker) when this row is a queued closing reply
    if closing and _reopened(st, row, *closing):
        _drop_closing(st, row, closing[0])  # the ask is open again: its thread is no one's to close
        return
    resolve = bool(closing)  # a closing reply resolves its thread in the same call; any other never does
    gone = None
    if not already:
        try:
            result = client.reply_to_artifact_comment(row["slug"], row["comment_id"], row["body"], resolve=resolve)
        except artifacts.ServiceError as exc:
            if item_id:
                _page_error(st, item_id, str(exc))
            if isinstance(exc, artifacts.Unauthorized):
                raise
            if getattr(exc, "ambiguous", False):
                report["replies"]["pending"].append(row["comment_id"])  # read the thread before sending again
                return
            _reply_state(st, row["id"], "failed", reason=str(exc))
            report["replies"]["failed"].append(row["comment_id"])
            if closing and isinstance(exc, artifacts.ToolError) and exc.error == "not_found":
                _closing_state(st, closing[0], "gone")  # the thread is gone, or not the token owner's to resolve
            return
        if closing and not (isinstance(result, dict) and result.get("resolved") is True and "note" not in result):
            gone = (result.get("note") if isinstance(result, dict) else None) or "the service did not resolve it"
    elif closing and (thread or {}).get("status") != "resolved":
        gone = "the closing reply is on the thread but the thread is not resolved"
    _reply_state(st, row["id"], "sent")
    report["replies"]["sent"].append(row["comment_id"])
    if closing:
        if gone:
            _closing_state(st, closing[0], "gone")
            report["errors"].append({"item": item_id, "slug": row["slug"], "error": f"thread {closing[0]} left open: {gone}"})
        else:
            _closing_state(st, closing[0], "resolved")
            report["resolved"].append(closing[0])


def _closing_of(conn, row):
    """(comment id, marker) when `row` is the closing reply of a thread still `queued`; else None. Read from
    the marker, never from the row's `resolve` column, so a row an older build queued with `resolve` stays inert."""
    comment_id = row["comment_id"]
    mark = closings(conn).get(comment_id)
    return (comment_id, mark) if mark and mark["reply"] == row["id"] and mark["state"] == "queued" else None


def _reopened(st, row, comment_id, mark):
    """True when the closing reply's ask no longer qualifies: it is open again, or its box is back on the page.
    The box is the marker's `handle` (a thread no pull has read has no `page_comments` row), else that row's,
    for a marker an earlier build wrote; with neither it is not told apart, so the reply goes out."""
    handle = mark.get("handle")
    if handle is None:
        found = st.conn.execute("SELECT handle FROM page_comments WHERE comment_id = ?", (comment_id,)).fetchone()
        handle = found["handle"] if found else None
    return handle is not None and closed_box(st.conn, row["slug"], handle) is None


def _drop_closing(st, row, comment_id):
    """Dismiss a never-sent closing reply and forget its marker, so a later close queues a fresh one."""
    with st.write_lock():
        st.conn.execute("DELETE FROM page_replies WHERE id = ? AND state = 'pending'", (row["id"],))
        marks = closings(st.conn)
        marks.pop(comment_id, None)
        _put_closings(st.conn, marks)


def _page_item(conn, slug):
    row = conn.execute("SELECT item_id FROM pages WHERE slug = ?", (slug,)).fetchone()
    return row["item_id"] if row else None


# ----- 4. close -------------------------------------------------------------------------------------


def closings(conn):
    """{comment id: {reply, state}} for every thread a closing reply was queued on."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (CLOSINGS_KEY,)).fetchone()
    try:
        return json.loads(row[0]) if row else {}
    except ValueError:
        return {}


def _put_closings(conn, marks):
    pages._set_meta(conn, CLOSINGS_KEY, json.dumps(marks, sort_keys=True))


def closing_body(row):
    """The last reply on a thread whose ask `row` has closed."""
    outcome = " ".join((row["outcome"] or "closed").split()).rstrip(".")
    return f"Closed: {outcome}. Recorded on {row['title']} ({row['id']})"


def closed_box(conn, slug, handle):
    """The closed ask whose box `handle` names, once that box has left the page `slug`; else None. A box
    is gone when the page is published and its last send holds no such handle. An open ask's box, a
    handle that isn't a box (`waiting`, a section) and a page not yet re-sent after the close are never gone."""
    if not handle or not handle.startswith(ASK_PREFIX):
        return None
    row = _ask(conn, handle[len(ASK_PREFIX):])
    page = conn.execute("SELECT * FROM pages WHERE slug = ?", (slug,)).fetchone()
    if row is None or row["status"] != "closed" or items.page_unpublished(page):
        return None
    shown = conn.execute("SELECT 1 FROM page_handles WHERE item_id = ? AND handle = ?",
                         (page["item_id"], handle)).fetchone()
    return None if shown else row


def _queue_closings(st):
    """Queue the closing reply of every thread, on a box that has left its page, whose ask closed since
    `closings_from`. The first pull of a build with this step only sets that stamp: asks closed before it
    are `trk pages tidy`'s, never a surprise on a page nobody asked to change."""
    since = st.meta(CLOSINGS_FROM_KEY)
    if since is None:
        with st.write_lock():
            pages._set_meta(st.conn, CLOSINGS_FROM_KEY, clock.stamp(clock.now()))
        return
    done = closings(st.conn)
    rows = st.conn.execute(
        "SELECT c.comment_id, c.slug, c.handle FROM page_comments c JOIN items i ON i.id = substr(c.handle, 5) "
        "WHERE c.handle LIKE 'ask-%' AND i.form = 'ask' AND i.status = 'closed' AND i.closed_at >= ? "
        "ORDER BY c.seen_at, c.comment_id", (since,)).fetchall()
    for row in rows:
        if row["comment_id"] not in done:
            queue_closing(st, row["slug"], row["comment_id"], row["handle"], since=since)


def queue_closing(st, slug, comment_id, handle, *, since=""):
    """One thread's closing reply and its marker, in one transaction. False when it is not queued: another
    pull queued it first, or the box has not left the page."""
    with st.transaction(PUBLISHER) as tx:
        if comment_id in closings(tx.conn):
            return False
        ask = closed_box(tx.conn, slug, handle)
        if ask is None or (ask["closed_at"] or "") < since:
            return False
        if tx.conn.execute("SELECT 1 FROM page_replies WHERE comment_id = ? AND state = 'pending'",
                           (comment_id,)).fetchone():
            return False  # the thread's earlier reply is not sent yet: the closing reply must follow it
        reply = tx.conn.execute("INSERT INTO page_replies (slug, comment_id, body, resolve, created_at) "
                                "VALUES (?, ?, ?, 0, ?)", (slug, comment_id, closing_body(ask), clock.stamp(tx.now)))
        _put_closings(tx.conn, {**closings(tx.conn), comment_id: {"reply": reply.lastrowid, "state": "queued", "handle": handle}})
    return True


def _closing_state(st, comment_id, state):
    with st.write_lock():
        marks = closings(st.conn)
        if comment_id in marks:
            marks[comment_id]["state"] = state
            _put_closings(st.conn, marks)


def tidy(st, client, key, *, dry_run=False):
    """`trk pages tidy <page-key>`: close, once, every thread on the page whose box is gone and whose ask has
    closed, whenever it closed (the pull's `closings_from` does not apply). Reads the page's threads rather
    than `page_comments`, so a thread no pull disposed of is closed too. Threads on open asks, on boxes still
    on the page, outside boxes, already resolved or already closed are left alone. Returns `page`, `slug`,
    `closing` (comment ids; with `dry_run` nothing is queued or sent), `skipped` ({comment, reason}), the
    token check's fields and the send's `replies`, `resolved` and `errors`."""
    report = {"held": False, "renewal_ask": None, "page": key, "slug": None, "closing": [], "skipped": [],
              "replies": {"sent": [], "failed": [], "pending": []}, "resolved": [], "errors": []}
    report.update(renewal.check(st))
    if report["held"]:
        return report
    page = st.conn.execute("SELECT * FROM pages WHERE item_id = ?", (key,)).fetchone()
    if items.page_unpublished(page):
        raise refused("no-page", f"{key} has no published page yet", item=key)
    report["slug"] = page["slug"]
    try:
        with pages_lock(st):
            _tidy(st, client, page, report, dry_run)
    except artifacts.Unauthorized:
        report.update(renewal.rejected(st, client))
    return report


def _tidy(st, client, page, report, dry_run):
    slug, key = page["slug"], page["item_id"]
    try:
        listed = client.list_artifact_comments(slug).get("comments") or []
    except artifacts.Unauthorized:
        raise
    except artifacts.ServiceError as exc:
        report["errors"].append({"item": key, "slug": slug, "error": str(exc)})
        return
    done = closings(st.conn)
    queued_before = _last_reply_id(st.conn)
    for comment in sorted(listed, key=lambda c: (_created(c), c.get("id") or "")):
        comment_id, handle = comment.get("id"), comment.get("handle") or ""
        if not handle.startswith(ASK_PREFIX):
            continue  # a section, a row, `waiting`, the page itself: not a box
        reason = None
        if comment_id in done:
            reason = "already-closing"
        elif comment.get("status") == "resolved":
            reason = "resolved"
        elif closed_box(st.conn, slug, handle) is None:
            ask = _ask(st.conn, handle[len(ASK_PREFIX):])
            reason = "no-such-ask" if ask is None else "ask-open" if ask["status"] != "closed" else "box-on-page"
        elif st.conn.execute("SELECT 1 FROM page_replies WHERE comment_id = ? AND state = 'pending'",
                             (comment_id,)).fetchone():
            reason = "reply-pending"  # its earlier reply is not sent yet: the closing reply must follow it
        if reason:
            report["skipped"].append({"comment": comment_id, "reason": reason})
        elif dry_run or queue_closing(st, slug, comment_id, handle):
            report["closing"].append(comment_id)
        else:
            report["skipped"].append({"comment": comment_id, "reason": "not-queued"})
    if not dry_run:
        threads = {slug: (key, listed)}
        _send_outbox(st, client, threads, queued_before, report)


# ----- bookkeeping: each its own short BEGIN IMMEDIATE, after the call --------------------------------


def _reply_state(st, reply_id, state, *, reason=None):
    """A reply's new state; a failure keeps its reason, for `trk pages --errors`."""
    with st.write_lock():
        st.conn.execute("UPDATE page_replies SET state = ?, sent_at = ? WHERE id = ?",
                        (state, clock.stamp(clock.now()) if state == "sent" else None, reply_id))
        if state == "failed":
            pages.note_reply_failure(st.conn, reply_id, reason, clock.stamp(clock.now()))
        elif state == "sent":
            pages.forget_reply_failure(st.conn, reply_id)


def _page_error(st, item_id, error):
    """A pull failure on the page's `last_error`, for `trk pages --errors`. It leaves `last_error_at` alone:
    that stamp is the flush's failure pause, and a page whose comments can't be read still publishes. A
    `pull-before-drop:` error a send left stays: it names this failure too, and says the page is held."""
    with st.write_lock():
        st.conn.execute("UPDATE pages SET last_error = ? WHERE item_id = ? AND (last_error IS NULL OR "
                        "last_error NOT LIKE 'pull-before-drop:%')", (error, item_id))


def _done_rereading(st, *item_ids):
    """Pages read after the send that dropped their ask handle (listed, or covered by the activity log)
    have had their last read for that drop."""
    if not set(item_ids) & pages.reread(st.conn):
        return
    with st.write_lock():
        pages.set_reread(st.conn, pages.reread(st.conn) - set(item_ids))


def _clear_pull_error(st, item_id, tool):
    """A call that works clears the error an earlier call of that tool left on the page (on every page
    when `item_id` is None, for the activity log), and no other."""
    where = (("WHERE item_id = ? AND last_error LIKE ?", (item_id, f"{tool}:%")) if item_id is not None
             else ("WHERE last_error LIKE ?", (f"{tool}:%",)))
    if st.conn.execute("SELECT 1 FROM pages " + where[0], where[1]).fetchone() is None:
        return  # nothing to clear: no write lock on every listing of every pass
    with st.write_lock():
        st.conn.execute("UPDATE pages SET last_error = NULL " + where[0], where[1])
