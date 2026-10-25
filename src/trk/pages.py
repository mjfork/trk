"""Publishing settings and page bookkeeping: `pages setup`, the page list, the adapter client, shells and
diffing, and marking the pages each journal row affects.

Loaded only by the `pages` verbs. trk stores no secret: the page adapter (docs/adapters.md) keeps its own
credentials, and its command line names where they are, never the secret itself. Settings changes are
journaled `config.changed`; the page bookkeeping tables, `publish_cursor`, the adapter's `describe` answer
and the setup's id are publisher state and are not.
"""

import hashlib
import json
import re
import uuid
from datetime import date, timedelta

from . import adapter, clock, items, needs_you, publisher, render, renewal, spawn
from .errors import refused, usage
from .render import page_kind

# CLI/feed spelling -> meta key, in the order setup journals them.
SETTINGS = (("publish-adapter", "publish_adapter"), ("publish-team", "publish_team"),
            ("publish-expires", "publish_expires"), ("publish-home", "publish_home"))
DESCRIBED_KEY = "publish_described"  # the adapter's `describe` answer at setup: {name, capabilities, expires?}
SETUP_KEY = "publish_setup"  # a new id per setup: a refusal to a client made before it holds nothing


def adapter_command(st):
    """The configured adapter's command line, or None before setup. A store set up before adapters
    has none until `trk pages setup --adapter COMMAND` runs."""
    return st.meta("publish_adapter") or None


def is_set_up(st):
    return adapter_command(st) is not None


def require_set_up(st):
    """flush, pull and tick refuse until setup has run (rule `publishing-not-set-up`)."""
    if not is_set_up(st):
        raise refused("publishing-not-set-up", "publishing is not set up: run `trk pages setup --adapter "
                                               "\"trk-pages-folder --dir PATH\" --home ID` (or another adapter) first")


def described(st):
    """The adapter's `describe` answer as setup stored it."""
    try:
        value = json.loads(st.meta(DESCRIBED_KEY) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def client(st):
    """A Client for the configured adapter."""
    return adapter.Client(adapter_command(st), described=described(st), setup_id=st.meta(SETUP_KEY))


def _check_command(command):
    try:
        adapter.split(command)
    except ValueError as exc:
        raise usage(f"--adapter must name the adapter's command: {exc}") from None
    return command.strip()


def _check_expires(text):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError(text)
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise refused("adapter-check-failed", f"the adapter's `expires` must be a date as YYYY-MM-DD, "
                                              f"not {text!r}") from None


def _check_home(conn, raw_id):
    """The home item's id: it must exist (exit 3) and be open (rule `home-closed`)."""
    row = items.get(conn, raw_id)
    if row["status"] != "open":
        raise refused("home-closed", f"{row['id']} is closed; the publisher's own asks need an open home item",
                      item=row["id"])
    return row["id"]


def setup(st, actor, *, command, home, team):
    """Configure publishing: check the adapter live with one `describe` call (outside any transaction),
    then store the settings, journaling `config.changed` per changed key. `command` defaults to the
    adapter already set. The expiry is the one `describe` gave, if any. The first setup also sets
    `publish_cursor` to the journal head, because every item is already dirty by having no `pages` row;
    a later setup (a renewal) keeps the cursor it has, so no row waiting to be marked is skipped. The
    same transaction lifts a hold on page writes and answers the open renewal ask (see
    `renewal.on_setup`). Returns what was configured, the keys that changed and what became of the
    renewal ask."""
    command = command or adapter_command(st)
    if not command:
        raise usage("--adapter must name the adapter's command, e.g. \"trk-pages-folder --dir PATH\"")
    command = _check_command(command)
    team = st.meta("publish_team") if team is None else team
    if not team or not team.strip():
        raise usage("--team must name the team every page is shared on")
    _check_home(st.conn, home)
    try:
        answer = adapter.Client(command).check()
    except publisher.ServiceError as exc:
        raise refused("adapter-check-failed", f"the adapter check failed: {exc}") from None
    expires = _check_expires(answer["expires"]) if answer.get("expires") else None
    values = {"publish_adapter": command, "publish_team": team, "publish_expires": expires}
    changed = []
    with st.transaction(actor) as tx:
        values["publish_home"] = _check_home(tx.conn, home)  # again, under the write lock
        for spelling, key in SETTINGS:
            old = tx.store.meta(key)
            if old == values[key] or (old in (None, "") and values[key] is None):
                continue
            if values[key] is None:
                tx.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                _set(tx.conn, key, values[key])
            tx.journal("config.changed", {}, data={"key": spelling, "old": old, "new": values[key]})
            changed.append(spelling)
        _set(tx.conn, DESCRIBED_KEY, json.dumps(answer, sort_keys=True))
        _set(tx.conn, SETUP_KEY, uuid.uuid4().hex)
        if tx.store.meta("publish_cursor") is None:
            head = tx.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM journal").fetchone()[0]
            tx.conn.execute("INSERT INTO meta (key, value) VALUES ('publish_cursor', ?)", (str(head),))
        renewal_ask = renewal.on_setup(tx, actor, expires)
    return {"adapter": command, "name": answer["name"], "capabilities": answer["capabilities"], "team": team,
            "expires": expires, "home": values["publish_home"], "changed": changed, "renewal_ask": renewal_ask}


def _set(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))


def page_rows(conn):
    """(item, level, pages row or None) for every page-bearing item, closed ones too, in tree order, then
    every person's Needs-you page (its item a stand-in: id `needs-you:<actor>`, title and kind)."""
    rows = {row["item_id"]: row for row in conn.execute("SELECT * FROM pages")}
    for item, level in items.outline(conn, include_closed=True):
        if page_kind(item) is not None:
            yield item, level, rows.get(item["id"])
    for actor_id in needs_you.people(conn):
        yield needs_you.entry(conn, actor_id), needs_you.LEVEL, rows.get(needs_you.key(actor_id))


def page_key(conn, raw):
    """The page a verb names: `needs-you:<actor>` for a person's page, else the item's id (exit 3 when
    there is no such item or actor)."""
    return needs_you.resolve_key(conn, raw) or items.get(conn, raw)["id"]


def status(st):
    """Whether publishing is set up and working: the adapter, its credentials' expiry, and the pages'
    counts. An install's check: set up, nothing failing, and a recent last publish."""
    if not is_set_up(st):
        return {"set_up": False}
    rows = listing(st.conn)
    told = described(st)
    published = [r["published_at"] for r in rows if r["published_at"]]
    return {"set_up": True, "adapter": told.get("name") or adapter_command(st).split()[0],
            "command": adapter_command(st), "capabilities": told.get("capabilities") or [],
            "team": st.meta("publish_team"), "home": st.meta("publish_home"),
            "expires": st.meta("publish_expires") or None, "pages": len(rows),
            "dirty": sum(r["dirty"] for r in rows),
            "errors": sum(bool(r["last_error"]) for r in rows),
            "failed_replies": len(failed_replies(st.conn)),
            "last_published_at": max(published) if published else None}


def listing(conn, *, dirty_only=False, errors_only=False):
    """Every page-bearing item in tree order (closed ones too), with its page bookkeeping. An item
    with no `pages` row has never been published and is dirty, due at once."""
    result = []
    for item, _level, page in page_rows(conn):
        dirty = items.page_dirty(page)
        entry = {"item": item["id"], "kind": page_kind(item), "title": item["title"],
                 "slug": page and page["slug"], "url": page and page["url"], "version": page and page["version"],
                 "published_at": page and page["published_at"], "dirty": dirty, "due_at": page and page["due_at"],
                 "last_error": page and page["last_error"]}
        if (dirty_only and not dirty) or (errors_only and not entry["last_error"]):
            continue
        result.append(entry)
    return result


# ----- shells: what a page's last publish looked like, handle by handle ----------------------------

ROOT = "@root"
_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)\b[^>]*>")
_HANDLE = re.compile(r'\sdata-comment-id="([^"]*)"')
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})


class Parsed:
    """One render's handled elements: `spans` {handle: (start, end)} of each outer HTML, `nested`
    {handle: handles directly inside it} and `top` (the handles directly under the document), all
    in document order."""

    def __init__(self, html, spans, nested, top):
        self.html, self.spans, self.nested, self.top = html, spans, nested, top

    def outer(self, handle):
        start, end = self.spans[handle]
        return self.html[start:end]


def parse(html):
    """The handled elements of one render; a handle twice on one page is a ValueError."""
    spans, nested = {}, {}
    top = []
    stack = []  # [tag name, handle or None, start, nested handles]

    def finish(frame, end):
        name, handle, start, inner = frame
        if handle is None:
            return
        if handle in spans:
            raise ValueError(f"handle {handle!r} appears twice on one page")
        spans[handle], nested[handle] = (start, end), inner
        parent = next((f for f in reversed(stack) if f[1] is not None), None)
        (parent[3] if parent is not None else top).append(handle)

    for match in _TAG.finditer(html):
        closing, name = match.group(1), match.group(2).lower()
        handle = None if closing else _HANDLE.search(match.group(0))
        if not closing and (name in _VOID or match.group(0).endswith("/>")):
            finish([name, handle and handle.group(1), match.start(), []], match.end())
        elif not closing:
            stack.append([name, handle and handle.group(1), match.start(), []])
        elif any(frame[0] == name for frame in stack):
            while stack:
                frame = stack.pop()
                if frame[0] == name:
                    finish(frame, match.end())
                    break
                finish(frame, match.start())  # an element left open ends where its parent does
    while stack:
        finish(stack.pop(), len(html))
    return Parsed(html, spans, nested, top)


def shells(html):
    """{handle: shell} for one render. A handle's shell is its element's outer HTML with every nested
    handled element replaced by a placeholder naming its handle; the root shell `@root` is the whole
    document with every top-level handle replaced the same way (spec: Diffing and patching)."""
    return _shells(parse(html))


def _shells(parsed):
    html, spans = parsed.html, parsed.spans

    def cut(start, end, inner):
        parts, pos = [], start
        for handle in inner:
            inner_start, inner_end = spans[handle]
            parts += [html[pos:inner_start], f"<!--{handle}-->"]
            pos = inner_end
        return "".join(parts + [html[pos:end]])

    result = {ROOT: cut(0, len(html), parsed.top)}
    for handle, (start, end) in spans.items():
        result[handle] = cut(start, end, parsed.nested[handle])
    return result


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hashes(html):
    """{handle: sha256 of its shell}, `@root` included: what `page_handles` stores per publish."""
    return _hashes(parse(html))


def _hashes(parsed):
    return {handle: _digest(shell) for handle, shell in _shells(parsed).items()}


def stored_hashes(conn, item_id):
    return {row["handle"]: row["shell_hash"]
            for row in conn.execute("SELECT handle, shell_hash FROM page_handles WHERE item_id = ?", (item_id,))}


# ----- diffing: what a page's next write carries -----------------------------------------------------

TITLE = "@title"  # page_handles also keeps the hash of the title the page was last given
MAX_EDITS = 50
WHOLE = "whole"


def title_hash(title):
    return _digest(title)


def patch(html, stored):
    """What to send so the page holds `html`, given the shell hashes of its last publish: None when
    every shell is unchanged, WHOLE when the root shell changed (or nothing is stored) or the patch
    would take more than MAX_EDITS edits, else the edits. A patch replaces the outermost handles
    that need sending (shell changed, or new), each carrying its nested handles with it; a handle
    whose own shell is unchanged is recursed into, never sent. A vanished handle is covered by its
    parent's shell, whose placeholder list changed."""
    parsed = parse(html)
    new = _hashes(parsed)
    old = {handle: digest for handle, digest in stored.items() if handle != TITLE}
    if new == old:
        return None
    if old.get(ROOT) != new[ROOT]:
        return WHOLE
    edits = []

    def visit(handle):
        if old.get(handle) != new[handle]:
            edits.append({"section": handle, "replace": parsed.outer(handle)})
        else:
            for inner in parsed.nested[handle]:
                visit(inner)

    for handle in parsed.top:
        visit(handle)
    return WHOLE if len(edits) > MAX_EDITS else edits


def resend(parsed, sent, changed, read):
    """What to send on the current version after a conflict (spec: Conflicts), given the refused
    edits (`sent`), the conflict's `changedSections` (every one a handle of this render) and the
    `sections` answer for them: WHOLE when the server misses any of them, None when the server
    already holds this render everywhere it could differ, else the edits. Sent again: every
    changed handle whose server source differs from the render, and every refused edit the other
    writer didn't touch (the server still holds the tracker's last publish there). A handle inside
    another one being sent goes with it."""
    sources = {section["id"]: section["source"] for section in read.get("sections") or []}
    if read.get("missing") or set(changed) - set(sources):
        return WHOLE
    send = {handle for handle in changed if sources[handle] != parsed.outer(handle)}
    send |= {edit["section"] for edit in sent} - set(changed)
    parent = {inner: outer for outer, nested in parsed.nested.items() for inner in nested}

    def inside_another(handle):
        while (handle := parent.get(handle)) is not None:
            if handle in send:
                return True
        return False

    edits = [{"section": handle, "replace": parsed.outer(handle)}
             for handle in sorted(send, key=lambda h: parsed.spans[h][0]) if not inside_another(handle)]
    return WHOLE if len(edits) > MAX_EDITS else edits or None


# ----- affected pages: the publisher's cursor into the journal -----------------------------------------

QUIET = timedelta(seconds=60)  # a mark makes its page due this long after the row
EVERY = "every page"

# The row kinds live in `spawn`, whose check after every write must agree with this table:
# ITEM_ROWS and ITEM_PREFIXES mark the item and every ancestor (`item.updated` changing the title
# also every descendant), EVERY_ROWS every page, TREE_ROWS as below.


def affected(conn, row):
    """The items whose pages one journal row marks (Affected pages' table), or EVERY. `page.created`
    marks nothing here: the transaction that journals it marks the parent and descendants due at
    once itself. Asks have no `pages` row, so marking one is a no-op."""
    change, item = row["change"], row["item"]
    if change in spawn.EVERY_ROWS:
        return EVERY
    if item is None:
        return set()
    data = json.loads(row["data"])
    if change in spawn.ITEM_ROWS or change.startswith(spawn.ITEM_PREFIXES):
        found = _ancestry(conn, item)
        if change == "item.updated" and "title" in data.get("fields", []):
            found |= _descendants(conn, item)
        return found
    if change == "owner.changed":
        return _ancestry(conn, item) | _descendants(conn, item)
    if change == "item.moved":
        old_parent = data["old"]["parent"]
        found = _ancestry(conn, item) | _descendants(conn, item)
        return found | (_ancestry(conn, old_parent) if old_parent else set())
    if change in ("edge.added", "edge.removed"):
        return _ancestry(conn, data["src"]) | _ancestry(conn, data["dst"])
    return set()  # claim.renewed, actor.added, config.changed, page.created, unknown kinds


PERSON_FIELDS = frozenset({"title", "urgent", "subtype"})  # an item's fields a Needs-you page shows


def _touches_people(conn, row):
    """Whether a journal row can change what a Needs-you page shows: an ask row (what waits on whom), a
    close or reopen (blockers come and go), an edge, a move or an owner change (blocks and owners), and
    an update to an ask or to a title, urgency or subtype. `role.set` and `actor.updated` mark every
    page already; an item's `page.created` marks the person pages when it is written."""
    change, item = row["change"], row["item"]
    if item is None:
        return False
    if change.startswith("ask.") or change in spawn.TREE_ROWS or change in ("item.closed", "item.reopened"):
        return True
    if change != "item.updated":
        return False
    found = conn.execute("SELECT form FROM items WHERE id = ?", (item,)).fetchone()
    return (found is not None and found["form"] == "ask") or \
        bool(PERSON_FIELDS & set(json.loads(row["data"]).get("fields", [])))


def _ancestry(conn, item_id):
    """The item and every ancestor."""
    return {row["id"] for row in items.lineage(conn, item_id)}


def _descendants(conn, item_id):
    return {r[0] for r in conn.execute(
        "WITH RECURSIVE down(id) AS (SELECT id FROM items WHERE parent_id = ? UNION ALL "
        "SELECT i.id FROM items i JOIN down ON i.parent_id = down.id) SELECT id FROM down", (item_id,))}


def mark_pages(conn, marks):
    """Apply {item id: (due_at, row at)}: `due_at` = the later of the two, `first_dirty_at` set only
    when null. Items without a `pages` row are already dirty and are left alone."""
    conn.executemany(
        "UPDATE pages SET due_at = CASE WHEN due_at IS NULL OR due_at < :due THEN :due ELSE due_at END, "
        "first_dirty_at = COALESCE(first_dirty_at, :at) WHERE item_id = :item",
        [{"item": item, "due": due, "at": moment} for item, (due, moment) in marks.items()])


def mark(st):
    """Read the journal from `publish_cursor` and mark the pages each row affects, advancing the
    cursor in the same transaction, so a crash re-marks rather than loses. When `RENDER_VERSION`
    differs from `meta.render_version`, every page is also due at once and loses its stored root
    hash, so it gets its whole body once. The caller holds pages.lock, so no unit of work's
    bookkeeping can clear a mark this run makes."""
    with st.write_lock():
        conn = st.conn
        now = clock.stamp(clock.now())
        if st.meta("render_version") != str(render.RENDER_VERSION):
            conn.execute("UPDATE pages SET due_at = ?, first_dirty_at = COALESCE(first_dirty_at, ?)", (now, now))
            conn.execute("DELETE FROM page_handles WHERE handle = ?", (ROOT,))
            _set_meta(conn, "render_version", str(render.RENDER_VERSION))
        cursor = int(st.meta("publish_cursor") or 0)
        rows = conn.execute("SELECT * FROM journal WHERE seq > ? ORDER BY seq", (cursor,)).fetchall()
        if not rows:
            return
        every = None
        people = set(needs_you.page_keys(conn))
        marks = {}
        for row in rows:
            found = affected(conn, row)
            if found == EVERY:
                if every is None:
                    every = [r[0] for r in conn.execute("SELECT item_id FROM pages")]
                found = every
            if found is not every and people and _touches_people(conn, row):
                found = found | people
            due = clock.stamp(clock.parse(row["at"]) + QUIET)
            for item in found:
                first = marks.get(item, (None, row["at"]))[1]
                marks[item] = (due, first)  # rows ascend, so the last due is the latest
        mark_pages(conn, marks)
        _set_meta(conn, "publish_cursor", str(rows[-1]["seq"]))


REREAD_KEY = "pages_reread"  # JSON list: item ids whose page dropped an ask-<id> handle, to be listed once more


def reread(conn):
    """The item ids whose page a send took an `ask-<id>` handle off, and whose comments the pull lists once
    more: a comment written between pull-before-drop's listing and the send would otherwise go unread."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (REREAD_KEY,)).fetchone()
    try:
        return set(json.loads(row["value"])) if row else set()
    except ValueError:
        return set()


REPLY_FAILURES_KEY = "reply_failures"  # JSON {outbox row id: {reason, at, dismissed?}} for failed replies


def reply_failures(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (REPLY_FAILURES_KEY,)).fetchone()
    try:
        return json.loads(row["value"]) if row else {}
    except ValueError:
        return {}


def note_reply_failure(conn, reply_id, reason, at):
    """Why an outbox reply failed for good, inside the caller's write transaction: `trk pages --errors` lists
    it, with its page, comment and ask, until it is resent or dismissed."""
    failures = reply_failures(conn)
    failures[str(reply_id)] = {"reason": reason, "at": at}
    _set_meta(conn, REPLY_FAILURES_KEY, json.dumps(failures, sort_keys=True))


def forget_reply_failure(conn, reply_id):
    """A reply sent after all (a resend) leaves no failure behind."""
    failures = reply_failures(conn)
    if failures.pop(str(reply_id), None) is not None:
        _set_meta(conn, REPLY_FAILURES_KEY, json.dumps(failures, sort_keys=True))


def failed_replies(conn):
    """Every failed outbox reply nobody has dismissed or resent, oldest first: what a person must follow up,
    since a failed reply is never sent again by itself."""
    failures = reply_failures(conn)
    rows = conn.execute("SELECT r.*, c.ask_id, c.handle, p.item_id FROM page_replies r "
                        "LEFT JOIN page_comments c USING (comment_id) LEFT JOIN pages p ON p.slug = r.slug "
                        "WHERE r.state = 'failed' ORDER BY r.id").fetchall()
    result = []
    for row in rows:
        noted = failures.get(str(row["id"])) or {}
        if noted.get("dismissed"):
            continue
        result.append({"reply": row["id"], "item": row["item_id"], "slug": row["slug"], "comment": row["comment_id"],
                       "ask": row["ask_id"], "body": row["body"], "resolve": bool(row["resolve"]),
                       "reason": noted.get("reason"), "failed_at": noted.get("at")})
    return result


def settle_reply(st, reply_id, action, reason=None):
    """`trk pages reply resend|dismiss <id>`: a failed reply goes back to `pending` (the next pull sends it,
    reading its thread first), or is dismissed with a reason and leaves `trk pages --errors`."""
    with st.write_lock():
        row = st.conn.execute("SELECT * FROM page_replies WHERE id = ?", (reply_id,)).fetchone()
        failures = reply_failures(st.conn)
        noted = failures.get(str(reply_id)) or {}
        if row is None or row["state"] != "failed" or noted.get("dismissed"):
            raise refused("no-failed-reply", f"no failed reply {reply_id} to {action}: `trk pages --errors` lists them")
        if action == "resend":  # sent as it stands, even when an earlier attempt left the same body there
            st.conn.execute("UPDATE page_replies SET state = 'pending' WHERE id = ?", (reply_id,))
            failures[str(reply_id)] = {**noted, "resent": True}
        else:
            failures[str(reply_id)] = {**noted, "dismissed": True, "dismiss_reason": reason,
                                       "dismissed_at": clock.stamp(clock.now())}
        _set_meta(st.conn, REPLY_FAILURES_KEY, json.dumps(failures, sort_keys=True))
    return {"reply": reply_id, "comment": row["comment_id"], "state": "pending" if action == "resend" else "failed",
            "dismissed": action == "dismiss"}


def set_reread(conn, item_ids):
    """Inside the caller's write transaction."""
    _set_meta(conn, REREAD_KEY, json.dumps(sorted(item_ids)))


def _set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))
