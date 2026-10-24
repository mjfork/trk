"""The artifact service client: JSON-RPC over `POST <base>/api/artifacts/mcp`, stdlib only.

Loaded only by the `pages` verbs that talk to the service. Every call goes through a transport,
`(url, headers, body bytes) -> (status, headers, body bytes)`; the default one uses urllib, and
tests plug in an in-process fake. The token goes into the Authorization header and nowhere else:
no error message ever carries it.

Each Client initializes once (`initialize`, then `notifications/initialized`) and echoes the
`Mcp-Session-Id` it was given, if any. A session the service rejects as unknown is replaced by one
new initialize, and the call is retried once. HTTP 502, 503, 504 and connection errors are retried
after 5, 20 and 60 s for reads and `update_artifact` (safe by `baseVersion`); `publish_artifact`
and `reply_to_artifact_comment` are never retried blindly, and their callers recover instead.
"""

import hmac
import http.client
import json
import re
import time
import urllib.error
import urllib.request

MCP_PATH = "/api/artifacts/mcp"
PROTOCOL_VERSION = "2025-03-26"
BACKOFF = (5, 20, 60)
RETRY_STATUSES = frozenset({502, 503, 504})
SESSION_NOT_FOUND = -32001  # the MCP SDK's JSON-RPC code for an unknown session (with HTTP 404)
TIMEOUT_SECONDS = 120
_sleep = time.sleep  # the backoff clock; tests replace it


class ServiceError(Exception):
    """A call to the artifact service failed. The message never carries the token."""


class Unauthorized(ServiceError):
    """HTTP 401: the token was rejected (revoked, expired or mistyped)."""

    SUFFIX = "the artifact service rejected the token (HTTP 401)"  # ends every page's last_error it causes


class HttpFailure(ServiceError):
    """Any other HTTP failure. `ambiguous` is set when the origin may have applied the call (a
    5xx from the edge on a write), so the caller must look before sending it again."""

    def __init__(self, message, status, ambiguous=False):
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class ConnectionFailure(ServiceError):
    """No HTTP answer at all: refused, reset, timed out or unresolvable. Always ambiguous."""

    ambiguous = True


class ToolError(ServiceError):
    """A tool refused (`isError: true` at HTTP 200). `error` is its code (`conflict`,
    `tool_not_allowed`, `not_found`…), `payload` the parsed JSON when the text was JSON (a conflict
    carries currentVersion and changedSections), and `missing_grant` the tool a token lacks."""

    def __init__(self, tool, text):
        self.tool = tool
        self.text = text
        self.payload = _json_object(text)
        if self.payload is not None:
            self.error = str(self.payload.get("error", "error"))
            self.detail = None
        else:
            match = re.match(r"\s*([a-z][a-z_]*)\s*(?:\((.*)\))?", text, re.S)
            self.error = match.group(1) if match else "error"
            self.detail = match.group(2) if match else None
        self.missing_grant = self.detail if self.error == "tool_not_allowed" else None
        super().__init__(self.summary())

    def summary(self):
        """One line for `last_error`: the code and its detail, without the service's long advice."""
        if self.payload is not None:
            return f"{self.tool}: {self.error}"
        first = self.text.strip().splitlines()[0] if self.text.strip() else self.error
        return f"{self.tool}: {first[:200]}"


def _json_object(text):
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def request_body(request_id, method, params):
    """A JSON-RPC request, or a notification when `request_id` is None."""
    message = {"jsonrpc": "2.0", **({"id": request_id} if request_id is not None else {}), "method": method,
               "params": params}
    return json.dumps(message)


def parse_sse(text, request_id):
    """The JSON-RPC message answering `request_id` from an SSE stream: events are separated by blank
    lines, `data:` lines within one event are joined by newlines, and `:` lines are comments."""
    messages = []
    for event in re.split(r"\r?\n\r?\n", text):
        data = [line[5:].removeprefix(" ") for line in event.splitlines() if line.startswith("data:")]
        if data:
            messages.append(json.loads("\n".join(data)))
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise ServiceError("the service's event stream carried no answer to the request")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the bearer token to wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(url, headers, body):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, dict(exc.headers or {}), exc.read()
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise ConnectionFailure(f"cannot reach the artifact service: {reason}") from None


class _SessionRejected(Exception):
    pass


class Client:
    """The calls the publisher makes. One Client is one run's MCP session."""

    def __init__(self, base_url, token, *, transport=None, sleep=None):
        self.endpoint = base_url.rstrip("/") + MCP_PATH
        self._token = token
        self._transport = transport if transport is not None else urllib_transport
        self._sleep = sleep if sleep is not None else _sleep
        self._session = None
        self._initialized = False
        self._next_id = 0
        self._tools = None

    def uses(self, token):
        """True when this client sends `token`; the secret itself never leaves the client."""
        return hmac.compare_digest(self._token, token)

    # ----- the publisher's calls ---------------------------------------------------------------

    def get_publishing_guidance(self):
        """Always allowed for any token: the setup check."""
        return self.call("get_publishing_guidance", {}, retry=True)

    def publish_artifact(self, *, title, body, team, tags):
        """A new page, shared on `team` from birth. Refused without a team, because a page born
        private can never be shared by a token. Never retried: on an ambiguous failure the caller
        looks for the page by its tag first."""
        if not team:
            raise ServiceError("refusing to publish a new page with no team: it would be private for good")
        return self.call("publish_artifact", {"title": title, "contentType": "html", "body": body, "team": team,
                                              "tags": list(tags)}, retry=False)

    def update_artifact(self, slug, *, base_version, edits=None, body=None, title=None):
        """A patch (`edits`) or a whole `body` on `base_version`; never sends `team`."""
        arguments = {"slug": slug, "baseVersion": base_version}
        if edits is not None:
            arguments["edits"] = edits
        if body is not None:
            arguments["body"] = body
        if title is not None:
            arguments["title"] = title
        return self.call("update_artifact", arguments, retry=True)

    def get_artifact(self, slug, *, handles):
        return self.call("get_artifact", {"slug": slug, "handles": list(handles)}, retry=True)

    def list_my_artifacts(self, *, limit=100, offset=0):
        return self.call("list_my_artifacts", {"status": "all", "limit": limit, "offset": offset}, retry=True)

    def find_tagged(self, tag, *, limit=100):
        """The caller's artifact carrying `tag`, as {slug, url, version}, or None. Pages through
        `list_my_artifacts`; the url comes from its `urlTemplate`, else from `get_artifact`."""
        offset = 0
        while True:
            listed = self.list_my_artifacts(limit=limit, offset=offset)
            rows = listed.get("artifacts") or []
            for row in rows:
                if tag in (row.get("tags") or []):
                    template = listed.get("urlTemplate")
                    url = (template.replace("{slug}", row["slug"]) if template
                           else self.get_artifact(row["slug"], handles=[])["url"])
                    return {"slug": row["slug"], "url": url, "version": row["currentVersion"]}
            if len(rows) < limit:
                return None
            offset += limit

    def list_artifact_comments(self, slug):
        return self.call("list_artifact_comments", {"slug": slug, "includeResolved": True, "scope": "all"}, retry=True)

    def list_comment_activity(self, cursor, *, limit):
        """One page of the cross-page activity log (service change 2) from `cursor` (None: the oldest
        event): {events, cursor, hasMore}."""
        # scope all: a connector call defaults to claude, which hides every reviewer's answer (tr-8eq14)
        arguments = {"limit": limit, "scope": "all"} if cursor is None else {"cursor": cursor, "limit": limit, "scope": "all"}
        return self.call("list_comment_activity", arguments, retry=True)

    def tools(self):
        """The names `tools/list` offers, read once per client and kept."""
        if self._tools is None:
            names, cursor = set(), None
            while True:
                listed = self.rpc("tools/list", {"cursor": cursor} if cursor else {}, retry=True)
                names |= {tool.get("name") for tool in listed.get("tools") or [] if isinstance(tool, dict)}
                cursor = listed.get("nextCursor")
                if not cursor:
                    break
            self._tools = frozenset(names)
        return self._tools

    def reply_to_artifact_comment(self, slug, comment_id, body, *, resolve):
        """Never retried: on an ambiguous failure the caller re-lists the thread before resending."""
        return self.call("reply_to_artifact_comment",
                         {"slug": slug, "commentId": comment_id, "body": body, "resolve": resolve}, retry=False)

    # ----- the protocol ----------------------------------------------------------------------

    def call(self, tool, arguments, *, retry):
        """tools/call: the parsed JSON of the tool's text result. A tool error raises ToolError."""
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments}, retry=retry, what=tool)
        text = "".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text")
        if result.get("isError"):
            raise ToolError(tool, text)
        value = _json_object(text)
        return value if value is not None else {"text": text}

    def rpc(self, method, params, *, retry=False, what=None):
        what = what or method
        self._ensure_initialized()
        try:
            return self._exchange(method, params, retry, what)
        except _SessionRejected:
            self._session, self._initialized = None, False
            self._ensure_initialized()
            try:
                return self._exchange(method, params, retry, what)
            except _SessionRejected:
                raise ServiceError(f"{what}: the service rejected a new MCP session too") from None

    def _ensure_initialized(self):
        if self._initialized:
            return
        self._session = None
        self._exchange("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                      "clientInfo": {"name": "trk", "version": "1"}}, True, "initialize")
        self._post(None, "notifications/initialized", {}, retry=True, what="initialize")
        self._initialized = True

    def _exchange(self, method, params, retry, what):
        self._next_id += 1
        request_id = self._next_id
        status, headers, raw = self._post(request_id, method, params, retry=retry, what=what)
        text = raw.decode("utf-8", errors="replace")
        if _header(headers, "Content-Type").startswith("text/event-stream"):
            message = parse_sse(text, request_id)
        else:
            message = _json_object(text)
            if message is None:
                raise ServiceError(f"{what}: the service answered HTTP {status} with no JSON-RPC message")
        if "error" in message:
            error = message["error"] or {}
            if error.get("code") == SESSION_NOT_FOUND:
                raise _SessionRejected()
            raise ServiceError(f"{what}: JSON-RPC error {error.get('code')}: {str(error.get('message'))[:200]}")
        return message.get("result") or {}

    def _post(self, request_id, method, params, *, retry, what):
        body = request_body(request_id, method, params).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self._token}"}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        delays = iter(BACKOFF if retry else ())
        while True:
            try:
                status, out_headers, raw = self._transport(self.endpoint, headers, body)
                failure = None
                if status in RETRY_STATUSES:
                    failure = HttpFailure(f"{what}: HTTP {status} from the artifact service", status, ambiguous=True)
            except ConnectionFailure as exc:
                failure = ConnectionFailure(f"{what}: {exc}")
            if failure is None:
                break
            delay = next(delays, None)
            if delay is None:
                raise failure
            self._sleep(delay)
        if status == 401:
            raise Unauthorized(f"{what}: {Unauthorized.SUFFIX}")
        if status == 404 and self._session and _session_not_found(raw):
            raise _SessionRejected()
        if status >= 400:  # a 5xx may come after the origin applied the call
            raise HttpFailure(f"{what}: HTTP {status} from the artifact service", status, ambiguous=status >= 500)
        session = _header(out_headers, "Mcp-Session-Id")
        if session:
            self._session = session
        return status, out_headers, raw


def _header(headers, name):
    lowered = name.lower()
    return next((value for key, value in headers.items() if key.lower() == lowered), "")


def _session_not_found(raw):
    """Only the MCP SDK's own answer counts; any other 404 (a wrong path) is an ordinary failure."""
    message = _json_object(raw.decode("utf-8", errors="replace")) or {}
    error = message.get("error")
    return isinstance(error, dict) and error.get("code") == SESSION_NOT_FOUND
