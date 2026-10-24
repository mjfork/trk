"""Page backends: where `trk pages` publishes, and the errors a backend raises.

Loaded only by the `pages` verbs. A backend is a class registered by name: `folder` is built in
(`trk.folder`), and an installed package adds others under the `trk.page_backends` entry-point
group. `trk pages setup --backend NAME` picks one, and `meta.publish_backend` keeps the choice.

A backend class has:

- `NAME`, its registered name, and `TOKEN`: True when it needs a token file and an expiry date
  (then setup takes `--base-url`, `--token-file` and `--expires`, and the renewal ask applies),
  False when it needs neither. `COMMENTS`: True when its pages take comments the pull can read and
  answer; a backend without them is never pulled. `GRANTS`: for a token backend, the permissions
  its token needs, named in the renewal ask.
- `__init__(settings, token)`: `settings` is {base_url, dir, team} as setup stored them (None when
  unset), `token` the secret (None for a backend without one).

and these calls. Each raises ServiceError (or a subclass) on failure, never with the token in it,
and the message starts with the call's name followed by a colon:

- `check()`: setup's live check, which changes nothing.
- `uses(token)`: True when this instance sends `token`.
- `publish(title=, body=, team=, tags=)`: a new page; {slug, url, version}. An ambiguous failure
  (the page may exist) carries `ambiguous = True`; the caller then looks with `find_tagged`.
- `update(slug, base_version=, edits=None, body=None, title=None)`: {version}. `edits` is a list of
  {section, replace}: each replaces the element whose `data-comment-id` is `section` with its
  outer HTML. A stale `base_version` raises Refused with error `conflict` and a payload carrying
  `currentVersion` and `changedSections` (the handles another writer changed).
- `sections(slug, handles=)`: {sections: [{id, source}], missing: [handle]}, the server's outer
  HTML of each handle.
- `find_tagged(tag)`: {slug, url, version} of the page carrying `tag`, or None.

A backend with `COMMENTS` also has:

- `list_comments(slug)`: {comments: [...]}, every root comment with `id`, `handle`, `body`,
  `createdAt`, `status` (open, outdated, resolved), `author`, `authorId`, `authorKind` (human,
  assistant) and `replies` ([{message, ...}]).
- `offers_activity()`: True when `activity` can be called.
- `activity(cursor, limit=)`: {events, cursor, hasMore}, the cross-page log of comment events from
  `cursor` (None: the oldest). An unknown cursor raises Refused with error `invalid_cursor`.
- `reply(slug, comment_id, body, resolve=)`: {resolved, note?}. Never retried by the backend: an
  ambiguous failure carries `ambiguous = True`, and the caller reads the thread before resending.
"""

import json
import re
from importlib.metadata import entry_points

ENTRY_POINTS = "trk.page_backends"
BUILTIN = {"folder": ("trk.folder", "Backend")}


class ServiceError(Exception):
    """A call to the page backend failed. The message never carries the token."""


class Unauthorized(ServiceError):
    """The backend rejected the token (revoked, expired or mistyped)."""

    SUFFIX = "the page backend rejected the token (HTTP 401)"  # ends every page's last_error it causes


class HttpFailure(ServiceError):
    """Any other failure with a status. `ambiguous` is set when the backend may have applied the
    call, so the caller must look before sending it again."""

    def __init__(self, message, status, ambiguous=False):
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class ConnectionFailure(ServiceError):
    """No answer at all: refused, reset, timed out or unresolvable. Always ambiguous."""

    ambiguous = True


class Refused(ServiceError):
    """The backend refused the call. `error` is its code (`conflict`, `not_found`, `invalid_cursor`…),
    `payload` the parsed JSON when the text was JSON (a conflict carries currentVersion and
    changedSections), and `missing_grant` the permission a token lacks (error `tool_not_allowed`)."""

    def __init__(self, call, text):
        self.call = call
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
        """One line for `last_error`: the code and its detail, without the backend's long advice."""
        if self.payload is not None:
            return f"{self.call}: {self.error}"
        first = self.text.strip().splitlines()[0] if self.text.strip() else self.error
        return f"{self.call}: {first[:200]}"


def _json_object(text):
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def names():
    """Every backend name this install can load, built-in ones first."""
    found = [point.name for point in entry_points(group=ENTRY_POINTS) if point.name not in BUILTIN]
    return [*BUILTIN, *sorted(found)]


def load(name):
    """The backend class registered as `name`; LookupError when none is. A built-in entry is a
    (module, attribute) pair, or the class itself (tests register their fakes so)."""
    if name in BUILTIN:
        if not isinstance(BUILTIN[name], tuple):
            return BUILTIN[name]
        module, attr = BUILTIN[name]
        return getattr(__import__(module, fromlist=[attr]), attr)
    for point in entry_points(group=ENTRY_POINTS):
        if point.name == name:
            return point.load()
    raise LookupError(f"no page backend named {name!r} is installed (installed: {', '.join(names())})")
