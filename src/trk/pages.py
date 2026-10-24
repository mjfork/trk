"""Publishing settings and page bookkeeping: `pages setup`, the page list, the token, shells and
diffing, and marking the pages each journal row affects.

Loaded only by the `pages` verbs. The token secret is never stored in `store.db` and never printed:
it is read from the file setup names (mode 0600 or stricter), or from $TRK_ARTIFACT_TOKEN in a
foreground run. Settings changes are journaled `config.changed`; the page bookkeeping tables and
`publish_cursor` are publisher state and are not.
"""

import hashlib
import json
import os
import re
import stat
import urllib.parse
from datetime import date, timedelta

from . import artifacts, clock, items, needs_you, render, renewal, spawn
from .errors import refused, usage
from .render import page_kind

DEFAULT_TOKEN_FILE = "~/.config/trk/token"
TOKEN_ENV = "TRK_ARTIFACT_TOKEN"

# CLI/feed spelling -> meta key, in the order setup journals them.
SETTINGS = (("publish-base-url", "publish_base_url"), ("publish-token-file", "publish_token_file"),
            ("publish-team", "publish_team"), ("publish-token-expires", "publish_token_expires"),
            ("publish-home", "publish_home"))


def is_set_up(st):
    return bool(st.meta("publish_base_url"))


def require_set_up(st):
    """flush, pull and tick refuse until setup has run (rule `publishing-not-set-up`)."""
    if not is_set_up(st):
        raise refused("publishing-not-set-up", "publishing is not set up: run `trk pages setup --base-url URL "
                                               "--token-file PATH --expires YYYY-MM-DD --home ID` first")


def token_file_path(raw):
    """The absolute path of a token file whose mode lets no one but its owner read it (rule
    `token-file-mode`); a missing file is a usage error."""
    path = os.path.abspath(os.path.expanduser(raw))
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise usage(f"cannot read the token file {path}: {exc.strerror or exc}") from None
    if not stat.S_ISREG(mode):
        raise usage(f"the token file {path} is not a regular file")
    if stat.S_IMODE(mode) & 0o077:
        raise refused("token-file-mode", f"the token file {path} has mode {stat.S_IMODE(mode):04o}; "
                                         f"it must be readable by its owner only: chmod 600 {path}")
    return path


def read_token(path, *, foreground):
    """The token secret. $TRK_ARTIFACT_TOKEN overrides the file only in a foreground run (and tests):
    the detached flusher and the timer always read the file."""
    if foreground and os.environ.get(TOKEN_ENV):
        return os.environ[TOKEN_ENV].strip()
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as exc:
        raise usage(f"cannot read the token file {path}: {exc.strerror or exc}") from None
    if not token:
        raise usage(f"the token file {path} is empty")
    return token


def client(st, *, foreground):
    """A Client for the configured service, holding the token in memory only."""
    path = token_file_path(st.meta("publish_token_file"))
    return artifacts.Client(st.meta("publish_base_url"), read_token(path, foreground=foreground))


def _check_base_url(url):
    """An https URL; plain http only to this machine, since the bearer token travels with every call."""
    parsed = urllib.parse.urlsplit(url)
    local = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if not parsed.hostname or not (parsed.scheme == "https" or local):
        raise usage(f"--base-url must be an https URL (http only for localhost), not {url!r}")
    return url.rstrip("/")


def _check_expires(text):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError(text)
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise usage(f"--expires must be a date as YYYY-MM-DD, not {text!r}") from None


def _check_home(conn, raw_id):
    """The home item's id: it must exist (exit 3) and be open (rule `home-closed`)."""
    row = items.get(conn, raw_id)
    if row["status"] != "open":
        raise refused("home-closed", f"{row['id']} is closed; the publisher's own asks need an open home item",
                      item=row["id"])
    return row["id"]


def setup(st, actor, *, base_url, token_file, expires, home, team):
    """Configure publishing: check the token file's token live with one `get_publishing_guidance` call (outside
    any transaction), then store the settings, journaling `config.changed` per changed key. The
    first setup also sets `publish_cursor` to the journal head, because every item is already dirty
    by having no `pages` row; a later setup (a token renewal) keeps the cursor it has, so no row
    waiting to be marked is skipped. The same transaction lifts a 401's hold on page writes and answers
    the open renewal ask (see `renewal.on_setup`). Returns what was configured, the keys that changed
    and what became of the renewal ask."""
    base_url = _check_base_url(base_url)
    expires = _check_expires(expires)
    team = st.meta("publish_team") if team is None else team
    if not team or not team.strip():
        raise usage("--team must name the team every page is shared on")
    path = token_file_path(token_file)
    token = read_token(path, foreground=False)  # the file the flusher and timer will read, never the override
    _check_home(st.conn, home)
    try:
        artifacts.Client(base_url, token).get_publishing_guidance()
    except artifacts.ServiceError as exc:
        raise refused("token-check-failed", f"the token check against {base_url} failed: {exc}") from None
    values = {"publish_base_url": base_url, "publish_token_file": path, "publish_team": team,
              "publish_token_expires": expires}
    changed = []
    with st.transaction(actor) as tx:
        values["publish_home"] = _check_home(tx.conn, home)  # again, under the write lock
        for name, key in SETTINGS:
            old = tx.store.meta(key)
            if old == values[key]:
                continue
            tx.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                            "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, values[key]))
            tx.journal("config.changed", {}, data={"key": name, "old": old, "new": values[key]})
            changed.append(name)
        if tx.store.meta("publish_cursor") is None:
            head = tx.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM journal").fetchone()[0]
            tx.conn.execute("INSERT INTO meta (key, value) VALUES ('publish_cursor', ?)", (str(head),))
        renewal_ask = renewal.on_setup(tx, actor, expires)
    return {"base_url": base_url, "token_file": path, "team": team, "expires": expires,
            "home": values["publish_home"], "changed": changed, "renewal_ask": renewal_ask}


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
    `get_artifact` answer for them: WHOLE when the server misses any of them, None when the server
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
