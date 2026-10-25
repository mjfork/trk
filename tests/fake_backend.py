"""An in-process fake page adapter with comments (see `trk.publisher` and `trk.adapter`).

`runner()` gives an `adapter._runner` that serves the command `fake-adapter --token-file PATH` in this
process through `trk.adapter.serve`, as a real adapter program would: the client reads its token from
that file on every run, and the fake checks it.

It models what the publisher relies on: slugs, versions and tags; team at publish; `base_version`
conflicts with `changedSections`; all-or-nothing patches by `data-comment-id` (duplicate or unknown
handles refused), the 50-edit and 2 MiB limits; `sections`; `find_tagged`; comments with handles,
`createdAt`, `authorId` and `authorKind` (switch `author_ids` off to model a backend that gives no
author ids), and replies, whose `resolve` succeeds only on the owner's pages (`give_away` makes one
someone else's). With `activity` on, `offers_activity` is True and `activity` is the cross-page log of
every page the token's owner owns: a `comment` event per root comment, a `reply` event per reply (the
tracker's own included) and a `status` event per resolve, whose author fields describe the root
comment's author. Its cursor is opaque, and a cursor it never issued is `invalid_cursor`. A reply never
reopens a resolved thread. A new version that drops a comment's handle makes an open comment `outdated`,
and one that puts the handle back makes it `open` again, each with its `status` event.

Injectable faults (`fail_next`): `502` and `500` (before the backend saw the call), `502-after` (the
backend applied the call, then the answer was lost: the ambiguous case), `401` and `connection` (no
answer at all). Reads and `update` are retried after a 502 or a connection failure (recorded in
`sleeps`), as a backend over a network would; `publish` and `reply` never are. `tool_not_allowed` comes
from the token's grants, and a conflict from a stale `base_version`, which `outside_write` makes by
writing a version the tracker did not.
"""

import base64
import json
import re
from datetime import UTC, datetime, timedelta

from trk import adapter
from trk.publisher import ConnectionFailure, Held, HttpFailure, Refused

BASE_URL = "https://pages.fake.test"
EXPIRES = "2027-10-01"
TOKEN = SECRET = "tok_fake-secret-0123456789"
OWNER_ID = "u-owner"
OWNER_NAME = "Michael Fork"

MAX_EDITS = 50
MAX_TITLE = 200  # the fake refuses a longer title
MAX_BODY_BYTES = 2 * 1024 * 1024

TRACKER_GRANTS = frozenset({"publish", "update", "sections", "find_tagged", "list_comments", "reply"})
ALWAYS_ALLOWED = frozenset({"check", "offers_activity"})
ACTIVITY = "activity"
ACTIVITY_LIMIT, ACTIVITY_MAX = 100, 500
BACKOFF = (5, 20, 60)
RETRIED = frozenset({"check", "update", "sections", "find_tagged", "list_comments", "offers_activity", "activity"})


class Fault:
    def __init__(self, kind, tool, times):
        self.kind, self.tool, self.times = kind, tool, times


class _Refusal(Exception):
    def __init__(self, text):
        super().__init__(text)
        self.text = text


class FakeBackend:
    """The service's state and the test controls. Registered as a backend class: calling it with
    (settings, token) gives a client bound to this state."""

    NAME = "fake"
    COMMENTS = True
    COMMAND = "fake-adapter"

    def __init__(self, *, token=SECRET, grants=TRACKER_GRANTS, teams=("tracker",), author_ids=True,
                 base_url=BASE_URL, activity=False, expires=EXPIRES):
        self.token = token
        self.expires = expires  # what `describe` says the credentials' expiry is
        self.grants = set(grants)
        self.teams = set(teams)
        self.author_ids = author_ids
        self.base_url = base_url
        self.activity = activity  # `offers_activity` is True (`activity` still needs its grant)
        self.events = []  # the activity log, oldest first
        self.artifacts = {}  # slug -> dict
        self.comments = {}  # comment id -> dict
        self.faults = []
        self.calls = []  # (call, arguments) for every call the backend processed
        self.requests = []  # every call that reached the fake, retries included
        self.sleeps = []  # the retry backoff's waits
        self._next_slug = 0
        self._next_comment = 0
        self._clock = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    def __call__(self, settings, token):
        return FakeClient(self, settings, token)

    def runner(self):
        """An `adapter._runner` serving `fake-adapter --token-file PATH` from this state."""
        return adapter.in_process(self._from_argv)

    def _from_argv(self, argv):
        if argv[:1] != [self.COMMAND] or len(argv) != 3 or argv[1] != "--token-file":
            raise Held(f"usage: {self.COMMAND} --token-file PATH")
        try:
            with open(argv[2], encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as exc:
            raise Held(f"cannot read the token file {argv[2]}: {exc.strerror or exc}") from None
        return FakeClient(self, {"base_url": self.base_url}, token)

    # ----- test controls -------------------------------------------------------------------

    def fail_next(self, kind, tool=None, times=1):
        """Queue a fault for the next `times` calls (of `tool`, when named)."""
        self.faults.append(Fault(kind, tool, times))

    def give_away(self, slug, owner="u-someone-else"):
        """Make a page someone else's, so a reply's `resolve` on it is refused."""
        self.artifacts[slug]["owner"] = owner

    def outside_write(self, slug, *, edits=None, body=None):
        """A new version written by someone other than the tracker (a person in the web UI, another
        agent): a patch on the current version, or a whole body. Not logged as a call."""
        art = self.artifacts[slug]
        self._new_version(slug, body if body is not None else apply_edits(art["versions"][-1], edits))
        art["updatedAt"] = self._tick()
        return len(art["versions"])

    def tool_calls(self, tool=None):
        return [args for name, args in self.calls if tool is None or name == tool]

    def add_comment(self, slug, handle, body, *, author="Kim Reader", author_id="u-kim", author_kind="human",
                    created_at=None):
        """A root comment by a person, as if typed in the web UI; returns its id."""
        self._next_comment += 1
        comment_id = f"c{self._next_comment}"
        self.comments[comment_id] = {
            "id": comment_id, "slug": slug, "handle": handle, "comment": body, "author": author,
            "authorId": author_id, "authorKind": author_kind, "status": "open",
            "createdAt": created_at or self._tick(), "replies": [],
        }
        self._event("comment", self.comments[comment_id], self.comments[comment_id]["createdAt"], body=body)
        return comment_id

    def add_reply(self, comment_id, body, *, author="Kim Reader", author_id="u-kim", author_kind="human"):
        """A person's reply in a comment's thread, as if typed in the web UI; returns its id."""
        root = self.comments[comment_id]
        self._next_comment += 1
        reply = {"id": f"c{self._next_comment}", "author": author, "authorId": author_id, "authorKind": author_kind,
                 "message": body, "createdAt": self._tick()}
        root["replies"].append(reply)
        self._event("reply", root, reply["createdAt"], comment_id=reply["id"], body=body,
                    author=(author, author_id, author_kind))
        return reply["id"]

    # ----- the calls -----------------------------------------------------------------------

    def call(self, name, arguments, *, token, base_url):
        """One call from a client: faults, the token, the grants, then the handler."""
        delays = iter(BACKOFF if name in RETRIED else ())
        while True:
            self.requests.append(name)
            fault = self._take_fault(name)
            if fault == "connection":
                failure = ConnectionFailure(f"{name}: cannot reach the page backend: connection refused")
            elif fault == "502":
                failure = HttpFailure(f"{name}: HTTP 502 from the page backend", 502, ambiguous=True)
            else:
                result = self._handle(name, arguments, fault, token, base_url)
                if fault != "502-after":
                    return result
                failure = HttpFailure(f"{name}: HTTP 502 from the page backend", 502, ambiguous=True)
            delay = next(delays, None)
            if delay is None:
                raise failure
            self.sleeps.append(delay)

    def _handle(self, name, arguments, fault, token, base_url):
        if fault == "500":
            raise HttpFailure(f"{name}: HTTP 500 from the page backend", 500, ambiguous=True)
        if fault == "401" or token != self.token:
            raise Held(f"{name}: the host rejected the token (HTTP 401)")
        if base_url != self.base_url:
            raise HttpFailure(f"{name}: HTTP 404 from the page backend", 404)
        if name not in ALWAYS_ALLOWED and name not in self.grants:
            raise Refused(name, f"tool_not_allowed ({name})")
        if name != "offers_activity":  # the probe is no page call
            self.calls.append((name, arguments))
        try:
            return getattr(self, "_" + name)(arguments)
        except _Refusal as exc:
            raise Refused(name, exc.text) from None

    def _take_fault(self, name):
        for fault in self.faults:
            if fault.tool is not None and fault.tool != name:
                continue
            if fault.tool is None and name in ALWAYS_ALLOWED and fault.kind != "401":
                continue  # untargeted faults hit the page calls, not the setup check or the activity probe
            fault.times -= 1
            if fault.times == 0:
                self.faults.remove(fault)
            return fault.kind
        return None

    def _check(self, a):
        return {"ok": True}

    def _offers_activity(self, a):
        return self.activity

    def _publish(self, a):
        if len(a.get("title") or "") > MAX_TITLE:
            raise _Refusal(f"Input validation error: Too big: expected string to have <={MAX_TITLE} characters at title")
        body = a["body"]
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise _Refusal(f"body_too_large (max {MAX_BODY_BYTES} bytes)")
        team = a.get("team")
        if not team:
            raise _Refusal("no_team (a page with no team would be private for good)")
        if team not in self.teams:
            raise _Refusal(f"unknown_team ('{team}' is not a team you belong to)")
        self._next_slug += 1
        slug = f"fake-{self._next_slug:04d}"
        self.artifacts[slug] = {
            "slug": slug, "title": a["title"], "tags": list(a.get("tags", [])), "team": team, "versions": [body],
            "owner": OWNER_ID, "updatedAt": self._tick(),
        }
        return self._write_result(slug)

    def _update(self, a):
        art = self.artifacts.get(a["slug"])
        if art is None or art["owner"] != OWNER_ID:
            raise _Refusal("not_found")
        if len(a.get("title") or "") > MAX_TITLE:
            raise _Refusal(f"Input validation error: Too big: expected string to have <={MAX_TITLE} characters at title")
        edits, body = a.get("edits"), a.get("body")
        if edits is not None and body is not None:
            raise _Refusal("edits_with_body (pass either `body` or `edits`, not both. Nothing was written.)")
        if edits is not None or body is not None:
            base = a.get("base_version")
            current = len(art["versions"])
            if base != current:
                if not isinstance(base, int) or not 1 <= base <= current:
                    raise _Refusal(f"invalid_base_version ({base})")
                raise _Refusal(json.dumps({
                    "error": "conflict", "baseVersion": base, "currentVersion": current,
                    "changedSections": changed_sections(art["versions"][base - 1], art["versions"][-1]),
                }, indent=2))
            if edits is not None:
                if not edits or len(edits) > MAX_EDITS:
                    raise _Refusal(f"invalid_arguments (edits takes 1 to {MAX_EDITS} entries)")
                try:
                    body = apply_edits(art["versions"][-1], edits)
                except EditRefused as exc:
                    raise _Refusal(f"edit_not_applied ({exc}. Nothing was written.)") from None
            if len(body.encode("utf-8")) > MAX_BODY_BYTES:
                raise _Refusal(f"body_too_large (max {MAX_BODY_BYTES} bytes)")
            self._new_version(art["slug"], body)
        if a.get("title") is not None:
            art["title"] = a["title"]
        art["updatedAt"] = self._tick()
        return self._write_result(art["slug"])

    def _sections(self, a):
        art = self.artifacts.get(a["slug"])
        if art is None:
            raise _Refusal("not_found")
        body = art["versions"][-1]
        sections, missing = [], []
        for handle in a["handles"]:
            spans = element_spans(body, handle)
            if len(spans) == 1:
                start, end = spans[0]
                sections.append({"id": handle, "source": body[start:end]})
            else:
                missing.append(handle)
        return {"sections": sections, **({"missing": missing} if missing else {})}

    def _find_tagged(self, a):
        for art in sorted(self.artifacts.values(), key=lambda art: art["updatedAt"], reverse=True):
            if art["owner"] == OWNER_ID and a["tag"] in art["tags"]:
                return {"slug": art["slug"], "url": self.url(art["slug"]), "version": len(art["versions"])}
        return None

    def _list_comments(self, a):
        if a["slug"] not in self.artifacts:
            raise _Refusal("not_found")
        return {"comments": [self._wire_comment(c) for c in self.comments.values() if c["slug"] == a["slug"]]}

    def _reply(self, a):
        comment = self.comments.get(a["comment_id"])
        if comment is None or comment["slug"] != a["slug"]:
            raise _Refusal("not_found")
        self._next_comment += 1
        reply_id = f"c{self._next_comment}"
        reply = {"id": reply_id, "author": OWNER_NAME, "authorId": OWNER_ID, "authorKind": "assistant",
                 "message": a["body"], "createdAt": self._tick()}
        comment["replies"].append(reply)
        self._event("reply", comment, reply["createdAt"], comment_id=reply_id, body=a["body"],
                    author=(OWNER_NAME, OWNER_ID, "assistant"))
        resolved = False
        if a.get("resolve"):
            resolved = OWNER_ID in (self.artifacts[a["slug"]]["owner"], comment["authorId"])
            if resolved:
                comment["status"] = "resolved"
                self._event("status", comment, self._tick(), status="resolved")  # the root's author, not the resolver
        result = {"replied": True, "commentId": a["comment_id"], "replyId": reply_id, "resolved": resolved}
        if self.author_ids:
            result["authorId"], result["authorKind"] = OWNER_ID, "assistant"
        if a.get("resolve") and not resolved:
            result["note"] = "only the page's owner or the comment's author can resolve it"
        return result

    def _activity(self, a):
        limit = a.get("limit") or ACTIVITY_LIMIT
        if not isinstance(limit, int) or not 1 <= limit <= ACTIVITY_MAX:
            raise _Refusal(f"invalid_arguments (limit takes 1 to {ACTIVITY_MAX})")
        start = 0
        if a.get("cursor") is not None:
            start = _cursor_position(a["cursor"])
            if start is None or start > len(self.events):
                raise _Refusal("invalid_cursor (the cursor was not issued by this backend)")
        covered = [(i, e) for i, e in enumerate(self.events) if i >= start
                   and self.artifacts.get(e["slug"], {}).get("owner") == OWNER_ID]
        page, more = covered[:limit], len(covered) > limit
        end = page[-1][0] + 1 if more else len(self.events)  # the uncovered tail is passed over
        cursor = None if a.get("cursor") is None and not covered else _cursor(end)  # an empty log gives none
        return {"events": [e for _i, e in page], "cursor": cursor, "hasMore": more}

    def _event(self, kind, root, at, *, comment_id=None, body=None, status=None, author=None):
        """One activity row. A status event's author fields describe the root comment's author."""
        name, author_id, author_kind = author or (root["author"], root["authorId"], root["authorKind"])
        event = {"type": kind, "at": at, "slug": root["slug"], "commentId": comment_id or root["id"],
                 "rootCommentId": root["id"], "handle": root["handle"], "author": name, "authorId": author_id,
                 "authorKind": author_kind}
        if body is not None:
            event["body"] = body
        if status is not None:
            event["status"] = status
        self.events.append(event)

    # ----- helpers -------------------------------------------------------------------------

    def url(self, slug):
        return f"{self.base_url}/pages/{slug}"

    def body(self, slug):
        return self.artifacts[slug]["versions"][-1]

    def _new_version(self, slug, body):
        """Append a version. An open comment whose handle the new version drops goes `outdated`, and an
        outdated one whose handle comes back is `open` again; a resolved one stays."""
        self.artifacts[slug]["versions"].append(body)
        present = set(handles_in(body))
        for c in self.comments.values():
            if c["slug"] != slug or not c["handle"]:
                continue
            if c["status"] == "open" and c["handle"] not in present:
                c["status"] = "outdated"
                self._event("status", c, self._tick(), status="outdated")
            elif c["status"] == "outdated" and c["handle"] in present:
                c["status"] = "open"
                self._event("status", c, self._tick(), status="open")

    def _write_result(self, slug):
        art = self.artifacts[slug]
        return {"slug": slug, "url": self.url(slug), "version": len(art["versions"]), "title": art["title"]}

    def _wire_comment(self, c):
        row = {"id": c["id"], "comment": c["comment"], "author": c["author"], "status": c["status"],
               "createdAt": c["createdAt"]}
        if self.author_ids:
            row["authorId"], row["authorKind"] = c["authorId"], c["authorKind"]
        if c["replies"]:
            row["replies"] = [
                {"author": f"you (via {r['author']})" if r["authorKind"] == "assistant" else r["author"],
                 **({"authorId": r["authorId"], "authorKind": r["authorKind"]} if self.author_ids else {}),
                 "message": r["message"], "createdAt": r["createdAt"]}
                for r in c["replies"]]
        if c["handle"]:
            row["handle"] = c["handle"]
        return row

    def _tick(self):
        self._clock += timedelta(seconds=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeClient:
    """What `trk pages` calls: the backend interface, over the fake's state."""

    NAME, COMMENTS = FakeBackend.NAME, FakeBackend.COMMENTS

    def __init__(self, service, settings, token):
        self.service, self.base_url, self._token = service, settings.get("base_url"), token

    @property
    def expires(self):
        return self.service.expires

    def _call(self, name, **arguments):
        return self.service.call(name, arguments, token=self._token, base_url=self.base_url)

    def check(self):
        return self._call("check")

    def publish(self, *, title, body, team, tags):
        return self._call("publish", title=title, body=body, team=team, tags=list(tags))

    def update(self, slug, *, base_version, edits=None, body=None, title=None):
        arguments = {"slug": slug, "base_version": base_version}
        for key, value in (("edits", edits), ("body", body), ("title", title)):
            if value is not None:
                arguments[key] = value
        return self._call("update", **arguments)

    def sections(self, slug, *, handles):
        return self._call("sections", slug=slug, handles=list(handles))

    def find_tagged(self, tag):
        return self._call("find_tagged", tag=tag)

    def list_comments(self, slug):
        return self._call("list_comments", slug=slug)

    def offers_activity(self):
        return self._call("offers_activity")

    def activity(self, cursor, *, limit):
        return self._call("activity", cursor=cursor, limit=limit)

    def reply(self, slug, comment_id, body, *, resolve):
        return self._call("reply", slug=slug, comment_id=comment_id, body=body, resolve=resolve)


def _cursor(position):
    return base64.urlsafe_b64encode(f"feed:{position}".encode()).decode()


def _cursor_position(cursor):
    try:
        text = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (ValueError, UnicodeError, AttributeError):
        return None
    match = re.fullmatch(r"feed:(\d+)", text)
    return int(match.group(1)) if match else None


class EditRefused(Exception):
    pass


_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)\b[^>]*>")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def element_spans(body, handle):
    """(start, end) of the outer source of every element carrying data-comment-id="<handle>"."""
    spans = []
    marker = f'data-comment-id="{handle}"'
    for match in _TAG.finditer(body):
        if match.group(1) or marker not in match.group(0):
            continue
        name = match.group(2).lower()
        if name in VOID or match.group(0).endswith("/>"):
            spans.append((match.start(), match.end()))
            continue
        depth = 0
        for inner in _TAG.finditer(body, match.start()):
            if inner.group(2).lower() != name:
                continue
            depth += -1 if inner.group(1) else 1
            if depth == 0:
                spans.append((match.start(), inner.end()))
                break
    return spans


def handles_in(body):
    return re.findall(r'data-comment-id="([^"]+)"', body)


def apply_edits(body, edits):
    """Apply a patch all or nothing, in order, as the service does."""
    for edit in edits:
        if "section" in edit:
            handle = edit["section"]
            spans = element_spans(body, handle)
            if not spans:
                raise EditRefused(f"unknown section {handle!r}")
            if len(spans) > 1:
                raise EditRefused(f"duplicated handle {handle!r}")
            start, end = spans[0]
            body = body[:start] + edit["replace"] + body[end:]
        else:
            count = body.count(edit["find"])
            if count != 1:
                raise EditRefused(f"find text occurs {count} times, not once")
            body = body.replace(edit["find"], edit["replace"])
    return body


def changed_sections(old, new):
    """Handles whose outer source differs between two versions (present in either)."""
    changed = []
    for handle in dict.fromkeys(handles_in(old) + handles_in(new)):
        a, b = element_spans(old, handle), element_spans(new, handle)
        source_a = [old[s:e] for s, e in a]
        source_b = [new[s:e] for s, e in b]
        if source_a != source_b:
            changed.append(handle)
    return changed


def refuse(text):
    """For a test's stand-in handler: the backend refuses the call with `text`."""
    raise _Refusal(text)
