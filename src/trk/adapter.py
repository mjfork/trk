"""Page adapters: the publisher's one door to wherever pages live (docs/adapters.md).

An adapter is any program. trk runs `<adapter command> <verb>`, writes one JSON object to its stdin and
reads one JSON object from its stdout. Every request carries `"protocol": PROTOCOL`. The exit code says
how the call went:

    0  ok: stdout holds the answer.
    2  held: credentials, permission or configuration. Page writes are held until `pages setup` runs
       again, and the publisher opens an ask to fix the adapter.
    3  unavailable: try again later. The call may or may not have taken effect.
    4  refused: this call alone. stdout holds {"error": code, ...}: `conflict` (with `currentVersion` and
       `changedSections`), `not_found`, `invalid_cursor`, `edit_not_applied`, `tool_not_allowed`…

Any other exit, or an answer that isn't one JSON object, fails that call; after a write (`publish`,
`update`, `reply`) it may have taken effect, since the adapter may have crashed after the host applied it.
The adapter's stderr becomes the error's text, cut short; the request is never echoed into an error.

This module has both sides. `Client` is trk's: it makes the calls the publisher makes (the same calls a
backend object has: see `trk.publisher`) by running the adapter. `serve` is an adapter's: a Python adapter
hands it a backend object, and it reads the verb and the request, makes the call and writes the answer and
the exit code. `trk.folder` is an adapter built so.

Every call goes through a runner, `(argv, request bytes) -> (exit code, stdout bytes, stderr bytes)`; the
default one runs the adapter as a child process, and tests plug in an in-process one.
"""

import json
import shlex
import subprocess
import sys

from .publisher import Held, Refused, ServiceError

PROTOCOL = 1
VERBS = ("describe", "check", "publish", "update", "sections", "find", "comments", "activity", "reply")
CAPABILITIES = ("comments", "activity")  # the optional verbs: `comments` and `reply`, and `activity`
WRITES = frozenset({"publish", "update", "reply"})
OK, HELD, UNAVAILABLE, REFUSED = 0, 2, 3, 4
TIMEOUT_SECONDS = 300
MAX_DETAIL = 300  # characters of the adapter's stderr kept in an error


class Unavailable(ServiceError):
    """Exit 3, a timeout, or a write whose outcome is unknown: the call may have taken effect."""

    ambiguous = True


def subprocess_runner(argv, request):
    """Run the adapter as a child process: (exit code, stdout, stderr). A timeout is exit 3."""
    try:
        done = subprocess.run(argv, input=request, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return UNAVAILABLE, b"", f"no answer within {TIMEOUT_SECONDS} s".encode()
    except OSError as exc:
        return HELD, b"", f"cannot run {argv[0]}: {exc.strerror or exc}".encode()
    return done.returncode, done.stdout, done.stderr


_runner = subprocess_runner  # tests replace it with an in-process one


def split(command):
    """The adapter command line as argv; ValueError when it is empty or badly quoted."""
    argv = shlex.split(command)
    if not argv:
        raise ValueError("the adapter command is empty")
    return argv


class Client:
    """The publisher's calls, made by running the adapter `command` (a shell-quoted command line).
    `described` is its `describe` answer as setup stored it: {name, capabilities, expires?}. `setup_id`
    names that setup, so a refusal to a client made before a later setup holds nothing (see
    `renewal.rejected`)."""

    def __init__(self, command, *, described=None, setup_id=None, runner=None):
        self.command = command
        self.argv = split(command)
        described = described or {}
        self.NAME = described.get("name") or self.argv[0]
        self.capabilities = frozenset(described.get("capabilities") or ())
        self.COMMENTS = "comments" in self.capabilities
        self.expires = described.get("expires")
        self.setup_id = setup_id
        self._runner = runner

    # ----- the calls -----------------------------------------------------------------------

    def check(self):
        """setup's check: `describe`, then the live `check`. {name, capabilities, expires?}."""
        answer = self.describe()
        self.call("check", {})
        return answer

    def describe(self, label="describe"):
        """{name, capabilities, expires?}: what the adapter says of itself, with no call to its host
        beyond what its capabilities need. Its errors start with `label`."""
        answer = self.call("describe", {}, label=label)
        if answer.get("protocol") != PROTOCOL:
            raise ServiceError(f"describe: the adapter speaks protocol {answer.get('protocol')!r}; "
                               f"this trk speaks {PROTOCOL}")
        caps = answer.get("capabilities", [])
        if not isinstance(caps, list) or not all(isinstance(cap, str) for cap in caps):
            raise ServiceError("describe: `capabilities` must be a list of names")
        expires = answer.get("expires")
        if expires is not None and not isinstance(expires, str):
            raise ServiceError("describe: `expires` must be a YYYY-MM-DD date or absent")
        name = answer.get("name")
        return {"name": name if isinstance(name, str) and name else self.argv[0],
                "capabilities": sorted(set(caps) & set(CAPABILITIES)),
                **({"expires": expires} if expires else {})}

    def publish(self, *, title, body, team, tags):
        answer = self.call("publish", {"title": title, "html": body, "team": team, "tag": tags[0]})
        return _page("publish", answer)

    def update(self, slug, *, base_version, edits=None, body=None, title=None):
        request = {"slug": slug, "base_version": base_version}
        if edits is not None:
            request["edits"] = edits
        if body is not None:
            request["html"] = body
        if title is not None:
            request["title"] = title
        answer = self.call("update", request)
        if not isinstance(answer.get("version"), int):
            raise Unavailable("update: the answer lacks `version`")
        return {"version": answer["version"]}

    def sections(self, slug, *, handles):
        answer = self.call("sections", {"slug": slug, "handles": list(handles)})
        found = [s for s in answer.get("sections") or [] if isinstance(s, dict)]
        return {"sections": found, **({"missing": list(answer["missing"])} if answer.get("missing") else {})}

    def find_tagged(self, tag):
        page = self.call("find", {"tag": tag}, label="find_tagged").get("page")
        return _page("find", page) if page else None

    def list_comments(self, slug):
        answer = self.call("comments", {"slug": slug}, label="list_comments")
        if not isinstance(answer.get("comments"), list):
            raise ServiceError("comments: the answer lacks a `comments` list")
        return {"comments": [c for c in answer["comments"] if isinstance(c, dict)]}

    def offers_activity(self):
        """Asked once per pull, so an adapter that gains or loses the log needs no new setup."""
        return "activity" in self.describe("offers_activity")["capabilities"]

    def activity(self, cursor, *, limit):
        answer = self.call("activity", {"cursor": cursor, "limit": limit})
        if not isinstance(answer.get("events"), list):
            raise ServiceError("activity: the answer lacks an `events` list")
        return {"events": [e for e in answer["events"] if isinstance(e, dict)], "cursor": answer.get("cursor"),
                "hasMore": bool(answer.get("hasMore"))}

    def reply(self, slug, comment_id, body, *, resolve, idempotency_key=None):
        """Never sent twice by trk without reading the thread first; `idempotency_key` lets an adapter
        whose host can deduplicate post it at most once all the same."""
        request = {"slug": slug, "comment_id": comment_id, "body": body, "resolve": resolve}
        if idempotency_key is not None:
            request["idempotency_key"] = idempotency_key
        return self.call("reply", request)

    # ----- one run -------------------------------------------------------------------------

    def call(self, verb, request, *, label=None):
        """One run of the adapter: its answer, or the error its exit code means, its message starting
        with `label` (the verb)."""
        runner = self._runner or _runner
        code, out, err = runner([*self.argv, verb], json.dumps({"protocol": PROTOCOL, **request}).encode())
        detail, label = _detail(err), label or verb
        message = detail if detail.startswith(f"{label}:") else f"{label}: " + (detail or f"exit {code}")
        if code == HELD:
            raise Held(f"{message}; {Held.SUFFIX}")
        if code == UNAVAILABLE:
            raise Unavailable(message)
        if code == REFUSED:
            raise Refused(label, (out or b"").decode("utf-8", "replace") or detail or "error")
        answer = _object(out) if code == OK else None
        if answer is None:
            why = message if code != OK else f"{label}: the answer is not one JSON object"
            if verb in WRITES:
                raise Unavailable(f"{why} (it may have taken effect)")
            raise ServiceError(why)
        return answer


def _page(verb, answer):
    if not isinstance(answer, dict) or not answer.get("slug") or not answer.get("url") \
            or not isinstance(answer.get("version"), int):
        error = Unavailable if verb == "publish" else ServiceError
        raise error(f"{verb}: the answer lacks `slug`, `url` or `version`")
    return {"slug": str(answer["slug"]), "url": answer["url"], "version": answer["version"]}


def _object(raw):
    try:
        value = json.loads(raw or b"")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _detail(err):
    """The adapter's stderr, as one short line."""
    text = " ".join((err or b"").decode("utf-8", "replace").split())
    return text if len(text) <= MAX_DETAIL else text[:MAX_DETAIL - 1] + "…"


# ----- the adapter's side ------------------------------------------------------------------------


def dispatch(backend, verb, request):
    """One call on a backend object (the calls `trk.publisher` lists): (exit code, answer, stderr text)."""
    try:
        return OK, _call(backend, verb, request), ""
    except Held as exc:
        return HELD, None, str(exc)
    except Refused as exc:
        payload = exc.payload if exc.payload is not None else {
            "error": exc.error, **({"detail": exc.detail} if exc.detail else {})}
        return REFUSED, payload, str(exc)
    except ServiceError as exc:
        return (UNAVAILABLE if getattr(exc, "ambiguous", False) else 1), None, str(exc)


def _call(backend, verb, a):
    if a.get("protocol") != PROTOCOL:
        raise Held(f"{verb}: this adapter speaks protocol {PROTOCOL}, not {a.get('protocol')!r}")
    if verb == "check":
        backend.check()
        return {}
    if verb == "describe":
        caps = [cap for cap, on in (("comments", backend.COMMENTS),
                                    ("activity", backend.COMMENTS and backend.offers_activity())) if on]
        expires = getattr(backend, "expires", None)
        return {"name": backend.NAME, "protocol": PROTOCOL, "capabilities": caps,
                **({"expires": expires} if expires else {})}
    if verb == "publish":
        return backend.publish(title=a["title"], body=a["html"], team=a.get("team"), tags=[a["tag"]])
    if verb == "update":
        return backend.update(a["slug"], base_version=a["base_version"], edits=a.get("edits"), body=a.get("html"),
                              title=a.get("title"))
    if verb == "sections":
        return backend.sections(a["slug"], handles=a["handles"])
    if verb == "find":
        return {"page": backend.find_tagged(a["tag"])}
    if verb in ("comments", "activity", "reply") and not backend.COMMENTS:
        raise Refused(verb, "unknown_verb")
    if verb == "comments":
        return backend.list_comments(a["slug"])
    if verb == "activity":
        return backend.activity(a.get("cursor"), limit=a["limit"])
    if verb == "reply":
        return backend.reply(a["slug"], a["comment_id"], a["body"], resolve=a.get("resolve", False))
    raise Refused(verb, "unknown_verb")


def serve(backend, argv=None, *, stdin=None, stdout=None, stderr=None):
    """An adapter's main: `<program> [options] <verb>` with the request on stdin. `backend` is the
    backend object, or a callable that makes it (so a bad configuration is exit 2, not a crash).
    Returns the exit code."""
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    verb = argv[-1] if argv else ""
    if verb not in VERBS:
        print(f"usage: <adapter> <verb>, a verb of {', '.join(VERBS)}", file=stderr)
        return 1
    try:
        request = json.loads(stdin.read() or "{}")
        if not isinstance(request, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        print(f"{verb}: the request is not one JSON object: {exc}", file=stderr)
        return 1
    try:
        obj = backend() if callable(backend) and not hasattr(backend, "publish") else backend
    except ServiceError as exc:
        print(str(exc), file=stderr)
        return HELD
    code, answer, message = dispatch(obj, verb, request)
    if answer is not None:
        stdout.write(json.dumps(answer) + "\n")
    if message:
        print(message, file=stderr)
    return code


def in_process(make_backend):
    """A runner that serves `make_backend(argv)` in this process: tests, and `trk.conformance --self-test`."""
    import io

    def runner(argv, request):
        out, err = io.StringIO(), io.StringIO()
        code = serve(lambda: make_backend(argv[:-1]), argv[-1:], stdin=io.StringIO(request.decode()),
                     stdout=out, stderr=err)
        return code, out.getvalue().encode(), err.getvalue().encode()

    return runner
