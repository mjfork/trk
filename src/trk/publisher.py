"""The errors a page adapter's calls raise, and the backend object a Python adapter is built on.

trk publishes through an adapter, a program it runs (docs/adapters.md, `trk.adapter`). A Python adapter
can be a backend object handed to `trk.adapter.serve`, which turns each verb into one of its calls and
each error below into the protocol's exit code; `trk.adapter.Client`, trk's side, makes the same calls
and raises the same errors. A backend object has:

- `NAME`, the adapter's name, `COMMENTS`: True when its pages take comments the pull can read and
  answer (an adapter without them is never pulled), and `expires`: the YYYY-MM-DD date its
  credentials expire, or None (the publisher asks for a renewal 14 days before it).

and these calls. Each raises ServiceError (or a subclass) on failure, never with a secret in it, and the
message starts with the call's name followed by a colon:

- `check()`: setup's live check, which changes nothing (verb `describe`).
- `publish(title=, body=, team=, tags=)`: a new page; {slug, url, version}. An ambiguous failure
  (the page may exist) carries `ambiguous = True`; the caller then looks with `find_tagged`.
- `update(slug, base_version=, edits=None, body=None, title=None)`: {version}. `edits` is a list of
  {section, replace}: each replaces the element whose `data-comment-id` is `section` with its
  outer HTML. A stale `base_version` raises Refused with error `conflict` and a payload carrying
  `currentVersion` and `changedSections` (the handles another writer changed). A title alone may
  keep the version.
- `sections(slug, handles=)`: {sections: [{id, source}], missing: [handle]}, the host's outer
  HTML of each handle.
- `find_tagged(tag)`: {slug, url, version} of the page carrying `tag`, or None (verb `find`).

An adapter with `COMMENTS` also has:

- `list_comments(slug)`: {comments: [...]}, every root comment with `id`, `handle`, `body`,
  `createdAt`, `status` (open, outdated, resolved), `author`, `authorId`, `authorKind` (human,
  assistant) and `replies` ([{message, ...}]) (verb `comments`).
- `offers_activity()`: True when `activity` can be called (capability `activity`).
- `activity(cursor, limit=)`: {events, cursor, hasMore}, the cross-page log of comment events from
  `cursor` (None: the oldest). An unknown cursor raises Refused with error `invalid_cursor`.
- `reply(slug, comment_id, body, resolve=)`: {resolved, note?}. Never retried by the adapter: an
  ambiguous failure carries `ambiguous = True`, and the caller reads the thread before resending.
"""

import json
import re

class ServiceError(Exception):
    """A call to the page adapter failed. The message never carries a secret."""


class Held(ServiceError):
    """The adapter refuses every call until it is fixed: its credentials were rejected (revoked,
    expired, mistyped), or a permission or its configuration is wrong. Exit 2.

    `cannot_run` holds the message when trk could not start the adapter at all, which is the one exit 2
    whose words trk wrote itself (from the OSError and the command) rather than read from the adapter's
    stderr. That is why the renewal ask may quote it: the ask's question is published, and nothing an
    adapter printed goes on a page."""

    SUFFIX = "the page adapter is held (exit 2)"  # ends every page's last_error it causes

    def __init__(self, message, *, cannot_run=None):
        super().__init__(message)
        self.cannot_run = cannot_run


class HttpFailure(ServiceError):
    """An adapter's call to its host failed with a status. `ambiguous` is set when the host may have applied
    the call, so the caller must look before sending it again."""

    def __init__(self, message, status, ambiguous=False):
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class ConnectionFailure(ServiceError):
    """No answer at all: refused, reset, timed out or unresolvable. Always ambiguous."""

    ambiguous = True


class Refused(ServiceError):
    """The adapter refused the call. `error` is its code (`conflict`, `not_found`, `invalid_cursor`…),
    `payload` the parsed JSON when the text was JSON (a conflict carries currentVersion and
    changedSections), and `missing_grant` the permission a token lacks (error `tool_not_allowed`)."""

    def __init__(self, call, text):
        self.call = call
        self.text = text
        self.payload = _json_object(text)
        if self.payload is not None:
            self.error = str(self.payload.get("error", "error"))
            detail = self.payload.get("detail")
            self.detail = detail if isinstance(detail, str) else None
        else:
            match = re.match(r"\s*([a-z][a-z_]*)\s*(?:\((.*)\))?", text, re.S)
            self.error = match.group(1) if match else "error"
            self.detail = match.group(2) if match else None
        self.missing_grant = self.detail if self.error == "tool_not_allowed" else None
        super().__init__(self.summary())

    def summary(self):
        """One line for `last_error`: the code and its detail, without the adapter's long advice."""
        if self.payload is not None:
            return f"{self.call}: {self.error}" + (f" ({self.detail[:200]})" if self.detail else "")
        first = self.text.strip().splitlines()[0] if self.text.strip() else self.error
        return f"{self.call}: {first[:200]}"


def _json_object(text):
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
