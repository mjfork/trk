"""An in-process fake of the artifact service's MCP endpoint, plugged in as the client's transport.

It speaks the wire the real service speaks (after its comment-answer change): JSON-RPC 2.0 over
`POST <base>/api/artifacts/mcp`, answers as SSE `data:` events (with a `: keepalive` comment
first, as the SDK writes them), tool errors as `isError: true` at HTTP 200 whose text is either a
code with a parenthesised detail or, for a conflict, a JSON payload. It keeps MCP sessions
(`Mcp-Session-Id`) so an expired one can be modelled; the real service runs stateless and never
sends one, which the client must also handle.

What it models, for what the tracker uses: slugs, versions and tags; team at publish (and the
refusal of `team` on a private page); `baseVersion` conflicts with `changedSections`; all-or-nothing
patches by `data-comment-id` (duplicate or unknown handles refused), the 50-edit and 2 MiB limits;
`get_artifact {handles}`; `list_my_artifacts`; comments with handles, `createdAt`, `authorId` and
`authorKind` (switch `author_ids` off to model the time before service change 1), and replies,
whose `resolve` (and `resolve_artifact_comment`, which the token is not granted) succeeds only on the owner's pages (`give_away` makes one someone else's). With `activity`
on (service change 2), `tools/list` offers `list_comment_activity`, the cross-page activity log of every
artifact the token's owner owns: a `comment` event per root comment, a `reply` event per reply (the
tracker's own included) and a `status` event per resolve, whose author fields describe the root
comment's author, as the service's own do. Its cursor is opaque, and a cursor it never issued is
`invalid_cursor`. A reply never reopens a resolved thread. A new version that drops a comment's
handle makes an open comment `outdated`, and one that puts the handle back makes it `open` again,
each with its `status` event.

Injectable faults (`fail_next`): `502` and `500` (bodiless, before the origin saw the call), `502-after`
(the origin applied the call, then the edge answered 502: the ambiguous case), `401`,
`connection` (no response at all) and `session-expired` (every live session forgotten).
`tool_not_allowed` comes from the token's grants, and a conflict from a stale `baseVersion`, which
`outside_write` makes by writing a version the tracker did not.
"""

import base64
import json
import re
from datetime import UTC, datetime, timedelta

from trk.artifacts import ConnectionFailure

BASE_URL = "https://artifacts.fake.test"
MCP_PATH = "/api/artifacts/mcp"
TOKEN = "tok_fake-secret-0123456789"
OWNER_ID = "u-owner"
OWNER_NAME = "Michael Fork"

MAX_EDITS = 50
MAX_TITLE = 200  # the real service refuses a longer title
MAX_BODY_BYTES = 2 * 1024 * 1024

TRACKER_GRANTS = frozenset({"publish_artifact", "update_artifact", "get_artifact", "list_my_artifacts",
                            "list_artifact_comments", "reply_to_artifact_comment"})  # not resolve_artifact_comment: the live token lacks it
ALWAYS_ALLOWED = frozenset({"get_publishing_guidance"})
ACTIVITY = "list_comment_activity"
ACTIVITY_LIMIT, ACTIVITY_MAX = 100, 500


class Fault:
    def __init__(self, kind, tool, times):
        self.kind, self.tool, self.times = kind, tool, times


class FakeArtifactService:
    def __init__(self, *, token=TOKEN, grants=TRACKER_GRANTS, teams=("tracker",), author_ids=True, sse=True,
                 stateful=True, base_url=BASE_URL, activity=False):
        self.token = token
        self.grants = set(grants)
        self.teams = set(teams)
        self.author_ids = author_ids
        self.sse = sse
        self.stateful = stateful
        self.base_url = base_url
        self.activity = activity  # service change 2: list_comment_activity is offered (it still needs its grant)
        self.events = []  # the activity log, oldest first
        self.artifacts = {}  # slug -> dict
        self.comments = {}  # comment id -> dict
        self.sessions = set()
        self.faults = []
        self.calls = []  # (tool, arguments) for every tools/call the origin processed
        self.requests = []  # (method, headers) for every request that reached the fake
        self._next_session = 0
        self._next_slug = 0
        self._next_comment = 0
        self._clock = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    # ----- test controls -------------------------------------------------------------------

    def fail_next(self, kind, tool=None, times=1):
        """Queue a fault for the next `times` requests (or tools/call of `tool`, when named)."""
        self.faults.append(Fault(kind, tool, times))

    def give_away(self, slug, owner="u-someone-else"):
        """Make an artifact someone else's, so a reply's `resolve` on it is refused."""
        self.artifacts[slug]["owner"] = owner

    def expire_sessions(self):
        self.sessions.clear()

    def outside_write(self, slug, *, edits=None, body=None):
        """A new version written by someone other than the tracker (a person in the web UI, another
        agent): a patch on the current version, or a whole body. Not logged as a tool call."""
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

    # ----- the transport -------------------------------------------------------------------

    def __call__(self, url, headers, body):
        """The client transport: (url, headers, request bytes) -> (status, headers, response bytes)."""
        payload = json.loads(body)
        method = payload.get("method")
        self.requests.append((method, dict(headers)))
        if url != self.base_url + MCP_PATH:
            return 404, {}, b"Not Found"
        fault = self._take_fault(method, payload)
        if fault == "connection":
            raise ConnectionFailure("connection refused")
        if fault in ("500", "502"):
            return int(fault), {}, b""
        if fault == "401" or headers.get("Authorization") != f"Bearer {self.token}":
            return 401, {"WWW-Authenticate": "Bearer"}, b'{"error":"unauthorized"}'
        if fault == "session-expired":
            self.sessions.clear()
        if method == "initialize":
            out_headers = {}
            if self.stateful:
                self._next_session += 1
                session = f"s-{self._next_session}"
                self.sessions.add(session)
                out_headers["Mcp-Session-Id"] = session
            return self._answer(payload, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                                          "serverInfo": {"name": "fake-artifacts", "version": "0"}}, out_headers)
        if self.stateful and headers.get("Mcp-Session-Id") not in self.sessions:
            error = {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Session not found"}, "id": None}
            return 404, {"Content-Type": "application/json"}, json.dumps(error).encode()
        if "id" not in payload:  # a notification
            return 202, {}, b""
        if method == "tools/list":
            return self._answer(payload, {"tools": [{"name": name} for name in sorted(self._tools())]})
        if method != "tools/call":
            error = {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "Method not found"}}
            return 200, {"Content-Type": "application/json"}, json.dumps(error).encode()
        name = payload["params"]["name"]
        arguments = payload["params"].get("arguments", {})
        result = self._tool(name, arguments)
        if fault == "502-after":
            return 502, {}, b""
        return self._answer(payload, result)

    def _take_fault(self, method, payload):
        for fault in self.faults:
            tool = payload.get("params", {}).get("name") if method == "tools/call" else None
            if fault.tool is not None and fault.tool != tool:
                continue
            if fault.tool is None and method != "tools/call" and fault.kind != "401":
                continue  # untargeted faults hit tool calls, not the handshake
            fault.times -= 1
            if fault.times == 0:
                self.faults.remove(fault)
            return fault.kind
        return None

    def _answer(self, payload, result, headers=None):
        message = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result})
        headers = dict(headers or {})
        if self.sse:
            headers["Content-Type"] = "text/event-stream"
            return 200, headers, f": keepalive\n\nevent: message\ndata: {message}\n\n".encode()
        headers["Content-Type"] = "application/json"
        return 200, headers, message.encode()

    # ----- tools ---------------------------------------------------------------------------

    def _tools(self):
        return TOOLS + ((ACTIVITY,) if self.activity else ())

    def _tool(self, name, arguments):
        if name not in self._tools():
            return _error(f"unknown_tool ({name})")
        if name not in ALWAYS_ALLOWED and name not in self.grants:
            return _error(f"tool_not_allowed ({name})")
        self.calls.append((name, arguments))
        return getattr(self, "_" + name)(arguments)

    def _get_publishing_guidance(self, arguments):
        return _json({"topic": arguments.get("topic", "core"), "guidanceVersion": "fake0001",
                      "guidance": "Fake guidance."})

    def _publish_artifact(self, a):
        if len(a.get("title") or "") > MAX_TITLE:
            return _error(f"Input validation error: Too big: expected string to have <={MAX_TITLE} characters at title")
        body = a["body"]
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            return _error(f"body_too_large (max {MAX_BODY_BYTES} bytes)")
        team = a.get("team")
        if team is not None and team not in self.teams:
            return _error(f"unknown_team ('{team}' is not a team you belong to)")
        self._next_slug += 1
        slug = f"fake-{self._next_slug:04d}"
        self.artifacts[slug] = {
            "slug": slug, "title": a["title"], "contentType": a.get("contentType", "html"), "tags": list(a.get("tags", [])),
            "team": team, "visibility": "team" if team else "private", "versions": [body], "owner": OWNER_ID,
            "updatedAt": self._tick(),
        }
        return _json(self._write_result(slug))

    def _update_artifact(self, a):
        art = self.artifacts.get(a["slug"])
        if art is None or art["owner"] != OWNER_ID:
            return _error("not_found")
        if len(a.get("title") or "") > MAX_TITLE:
            return _error(f"Input validation error: Too big: expected string to have <={MAX_TITLE} characters at title")
        if a.get("team") is not None and art["visibility"] == "private":
            return _error(f"private_artifact_cannot_join_team (this artifact is currently PRIVATE)")
        edits, body = a.get("edits"), a.get("body")
        if edits is not None and body is not None:
            return _error("edits_with_body (pass either `body` or `edits`, not both. Nothing was written.)")
        if edits is not None or body is not None:
            base = a.get("baseVersion")
            if base is None:
                return _error("base_version_required (baseVersion is required with body or edits)")
            current = len(art["versions"])
            if base != current:
                if not 1 <= base <= current:
                    return _error(f"invalid_base_version ({base})")
                return _error(json.dumps({
                    "error": "conflict", "baseVersion": base, "currentVersion": current,
                    "currentVersionCreatedAt": art["updatedAt"],
                    "changedSections": changed_sections(art["versions"][base - 1], art["versions"][-1]),
                }, indent=2))
            if edits is not None:
                if not edits or len(edits) > MAX_EDITS:
                    return _error(f"invalid_arguments (edits takes 1 to {MAX_EDITS} entries)")
                try:
                    body = apply_edits(art["versions"][-1], edits)
                except EditRefused as exc:
                    return _error(f"edit_not_applied ({exc}. Nothing was written.)")
            if len(body.encode("utf-8")) > MAX_BODY_BYTES:
                return _error(f"body_too_large (max {MAX_BODY_BYTES} bytes)")
            self._new_version(art["slug"], body)
        for key in ("title", "tags"):
            if a.get(key) is not None:
                art[key] = a[key]
        art["updatedAt"] = self._tick()
        return _json(self._write_result(art["slug"]))

    def _get_artifact(self, a):
        art = self.artifacts.get(a["slug"])
        if art is None:
            return _error("not_found")
        version = a.get("version") or len(art["versions"])
        body = art["versions"][version - 1]
        result = {"slug": art["slug"], "url": self.url(art["slug"]), "title": art["title"],
                  "contentType": art["contentType"], "version": version, "currentVersion": len(art["versions"]),
                  "tags": art["tags"], "isOwner": art["owner"] == OWNER_ID}
        if a.get("handles"):
            sections, missing = [], []
            for handle in a["handles"]:
                spans = element_spans(body, handle)
                if len(spans) == 1:
                    start, end = spans[0]
                    sections.append({"id": handle, "source": body[start:end]})
                else:
                    missing.append(handle)
            result["sections"] = sections
            if missing:
                result["missing"] = missing
        if a.get("include") == "body":
            result["body"] = body
        return _json(result)

    def _list_my_artifacts(self, a):
        rows = [{"slug": art["slug"], "title": art["title"], "status": "open", "currentVersion": len(art["versions"]),
                 "updatedAt": art["updatedAt"], "tags": art["tags"]}
                for art in self.artifacts.values() if art["owner"] == OWNER_ID]
        rows.sort(key=lambda row: row["updatedAt"], reverse=True)
        offset, limit = a.get("offset", 0), a.get("limit", 25)
        tags = sorted({tag for row in rows for tag in row["tags"]})
        return _json({"urlTemplate": self.url("{slug}"), "artifacts": rows[offset:offset + limit], "tags": tags})

    def _list_artifact_comments(self, a):
        if a["slug"] not in self.artifacts:
            return _error("not_found")
        rows = [c for c in self.comments.values()
                if c["slug"] == a["slug"] and (a.get("includeResolved") or c["status"] == "open")]
        return _json({"comments": [self._wire_comment(c) for c in rows]})

    def _reply_to_artifact_comment(self, a):
        comment = self.comments.get(a["commentId"])
        if comment is None or comment["slug"] != a["slug"]:
            return _error("not_found")
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
        result = {"replied": True, "commentId": a["commentId"], "replyId": reply_id, "resolved": resolved}
        if self.author_ids:
            result["authorId"], result["authorKind"] = OWNER_ID, "assistant"
        if a.get("resolve") and not resolved:
            result["note"] = "only the artifact's owner or the comment's author can resolve it"
        return _json(result)

    def _resolve_artifact_comment(self, a):
        """Only the artifact's owner or the comment's author may resolve; anyone else gets `not_found`, as an
        unknown id does. Resolving a resolved thread is no error."""
        comment = self.comments.get(a["commentId"])
        if comment is None or comment["slug"] != a["slug"] or \
                OWNER_ID not in (self.artifacts[a["slug"]]["owner"], comment["authorId"]):
            return _error("not_found")
        if comment["status"] != "resolved":
            comment["status"] = "resolved"
            self._event("status", comment, self._tick(), status="resolved")
        return _json({"resolved": True, "commentId": a["commentId"]})

    def _list_comment_activity(self, a):
        limit = a.get("limit") or ACTIVITY_LIMIT
        if not isinstance(limit, int) or not 1 <= limit <= ACTIVITY_MAX:
            return _error(f"invalid_arguments (limit takes 1 to {ACTIVITY_MAX})")
        start = 0
        if a.get("cursor") is not None:
            start = _cursor_position(a["cursor"])
            if start is None or start > len(self.events):
                return _error("invalid_cursor (the cursor was not issued by this service)")
        covered = [(i, e) for i, e in enumerate(self.events) if i >= start
                   and self.artifacts.get(e["slug"], {}).get("owner") == OWNER_ID]
        page, more = covered[:limit], len(covered) > limit
        end = page[-1][0] + 1 if more else len(self.events)  # the uncovered tail is passed over
        cursor = None if a.get("cursor") is None and not covered else _cursor(end)  # an empty log gives none
        return _json({"events": [e for _i, e in page], "cursor": cursor, "hasMore": more})

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
        return f"{self.base_url}/artifacts/{slug}"

    def body(self, slug):
        return self.artifacts[slug]["versions"][-1]

    def _new_version(self, slug, body):
        """Append a version. As on the service, an open comment whose handle the new version drops goes
        `outdated`, and an outdated one whose handle comes back is `open` again; a resolved one stays."""
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
        return {"slug": slug, "url": self.url(slug), "version": len(art["versions"]), "title": art["title"],
                "visibility": art["visibility"], "status": "open", "statusSetBy": "assistant", "forDate": None,
                "tags": art["tags"], "teamSlug": art["team"], "note": "fake"}

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


TOOLS = ("get_publishing_guidance", "publish_artifact", "update_artifact", "get_artifact", "list_my_artifacts",
         "list_artifact_comments", "reply_to_artifact_comment", "resolve_artifact_comment")


def _cursor(position):
    return base64.urlsafe_b64encode(f"feed:{position}".encode()).decode()


def _cursor_position(cursor):
    try:
        text = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (ValueError, UnicodeError, AttributeError):
        return None
    match = re.fullmatch(r"feed:(\d+)", text)
    return int(match.group(1)) if match else None


def _json(value):
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2)}]}


def _error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


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
