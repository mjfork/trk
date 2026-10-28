# Page adapters

trk publishes its pages through a **page adapter**: any program that speaks the small protocol below. trk ships one, `trk-pages-folder`, which writes each page as an HTML file in a directory. An adapter for a hosted service, where people comment on pages, lives in its own repository and keeps its own credentials.

## Contents

- [Set up an adapter](#set-up-an-adapter)
- [The protocol](#the-protocol)
- [The verbs](#the-verbs)
- [Retries and ambiguous failures](#retries-and-ambiguous-failures)
- [Writing an adapter](#writing-an-adapter)
- [Checking an adapter](#checking-an-adapter)

## Set up an adapter

```bash
trk pages setup --adapter "trk-pages-folder --dir ~/trk-pages" --home <open item id>
```

`--adapter` is the adapter's command line, quoted as one argument. trk runs it once per call, adding the verb as the last argument. Setup runs `describe` and `check`, then stores the command line, the team, the expiry the adapter reported (if any) and the home item.

The stored command line names the program by the absolute path setup resolved it to, so name it however your shell finds it. A timer runs with a bare PATH — a systemd user unit and a launchd job have neither `~/.local/bin` nor anything a login shell adds — so a bare command that works in `trk pages setup` and in a hand-run `trk pages tick` cannot be started on a timed one, and that reads as the adapter refusing. Symlinks are kept as they are, since resolving a virtualenv's `python` would leave the virtualenv behind. A program PATH does not have is stored as written and the check reports it as `cannot run`. Move the adapter, and run setup again.

trk stores no secret. Keep credentials where the adapter reads them: its own config file, a file of mode 0600 named on its command line, or a keychain. The command line is stored and shown in `trk changes`, so a path to a secret may go on it and a secret itself never does.

## The protocol

trk runs `<command> <verb>`, writes one JSON object to the adapter's stdin, and reads one JSON object from its stdout. Every request carries `"protocol": 1`. The exit code says how the call went:

| Exit | Meaning | What trk does |
|---|---|---|
| `0` | ok: stdout holds the answer | goes on |
| `2` | held: credentials rejected, a permission missing, or the adapter's configuration wrong | holds every page write until `trk pages setup` runs again, and opens the ask "Fix the page adapter". trk being unable to start the program at all counts as exit 2, and the ask says so rather than blaming credentials |
| `3` | unavailable: try again later; the call **may have taken effect** | leaves the page dirty; after a `publish`, looks the page up by its tag before sending again |
| `4` | refused: this call alone. stdout holds `{"error": "<code>", ...}` | acts on the code (below) |

Any other exit, or an answer that isn't one JSON object, fails the call. After a write (`publish`, `update`, `reply`) trk treats such a failure like exit `3`, since the adapter may have crashed after its host applied the write.

The adapter's stderr becomes the error's text, cut to 300 characters. Never write a secret or the request to stderr or stdout.

A request whose `protocol` the adapter does not speak gets exit `2`. A verb the adapter does not support gets exit `4` with `{"error": "unknown_verb"}`.

Refusal codes trk acts on:

- `conflict`: an `update` sent on a stale `base_version`. Include `currentVersion` (an integer) and `changedSections` (the handles another writer changed, as strings or `{id}` objects). trk reads those sections and sends its change once more on the current version.
- `not_found`: no page with that slug.
- `edit_not_applied`: a patch named a handle the page lacks. Add `detail` to say which.
- `invalid_cursor`: an `activity` cursor the adapter no longer knows.
- `tool_not_allowed`: the credentials lack a permission. Put the permission's name in `detail`.

## The verbs

`→` is the request trk writes and `←` the answer. `protocol` is left out of each request below.

### describe

What the adapter says of itself. It should not need its host unless a capability depends on it. trk calls it at setup and once per comment pull.

```
→ {}
← {"name": "folder", "protocol": 1, "capabilities": [], "expires": "2027-01-31"}
```

- `capabilities` lists the optional verbs: `comments` (the verbs `comments` and `reply`) and `activity` (needs `comments`). An empty list means publish only.
- `expires` is the last day the adapter's credentials work, as YYYY-MM-DD. Leave it out when nothing expires. From 14 days before it, the publisher opens the ask "Fix the page adapter".

### check

Setup's live check: reach the host with the credentials and change nothing.

```
→ {}
← {}
```

### publish

A new page.

```
→ {"title": "Add the widget button", "html": "<!doctype html>…", "team": "tracker", "tag": "trk:tr-j3eqh"}
← {"slug": "page-81", "url": "https://pages.example/page-81", "version": 1}
```

- `tag` names the trk page: `trk:<item id>`, or `trk:needs-you:<actor>` for a person's page. Keep it with the page; `find` looks it up.
- `team` is who the page is shared with from the moment it exists.
- `version` is an integer that every later content write raises.

### update

A new version of a page.

```
→ {"slug": "page-81", "base_version": 3, "edits": [{"section": "tiles", "replace": "<div data-comment-id=\"tiles\">…</div>"}]}
→ {"slug": "page-81", "base_version": 3, "html": "<!doctype html>…", "title": "New title"}
← {"version": 4}
```

- Send exactly one of `edits` and `html`, or neither when only `title` changes.
- `edits` replaces, for each entry, the element whose `data-comment-id` is `section` with `replace` (outer HTML). Apply all the edits or none.
- **Enforce `base_version`.** When it is not the page's current version, refuse with `conflict`. Without this a person's edit on the page is silently overwritten.
- A title alone may keep the version.

### sections

The current outer HTML of some handles, read after a conflict.

```
→ {"slug": "page-81", "handles": ["tiles", "ask-tr-n9t9t"]}
← {"sections": [{"id": "tiles", "source": "<div data-comment-id=\"tiles\">…</div>"}], "missing": ["ask-tr-n9t9t"]}
```

### find

The page carrying a tag, or `null`.

```
→ {"tag": "trk:tr-j3eqh"}
← {"page": {"slug": "page-81", "url": "https://pages.example/page-81", "version": 4}}
← {"page": null}
```

### comments (capability `comments`)

Every root comment on one page.

```
→ {"slug": "page-81"}
← {"comments": [{"id": "c-88", "handle": "ask-tr-n9t9t", "body": "Metres.", "createdAt": "2026-10-11T12:00:00.000Z",
                 "status": "open", "author": "Kim Reader", "authorId": "u-kim", "authorKind": "human",
                 "replies": [{"message": "…"}]}]}
```

- `handle` is the `data-comment-id` the comment is anchored to.
- `status` is `open`, `outdated` (its handle left the page) or `resolved`.
- `authorKind` is `human` or `assistant`. trk acts only on people's comments.
- `authorId` is the host's stable id for the person; `trk actor link` maps it to an actor.

### activity (capability `activity`)

The cross-page log of comment events, so a pull makes one call instead of one per page.

```
→ {"cursor": null, "limit": 500}
← {"events": [...], "cursor": "opaque", "hasMore": false}
```

`cursor` is `null` for the oldest event, else the cursor an earlier answer gave. Refuse a cursor you no longer know with `invalid_cursor`; trk then reads the log again from the start to seed itself, and acts on none of it.

### reply (capability `comments`)

One reply in a comment's thread, and optionally resolving it.

```
→ {"slug": "page-81", "comment_id": "c-88", "body": "Answered: metres.", "resolve": true, "idempotency_key": "trk-reply-412"}
← {"resolved": true}
← {"resolved": false, "note": "only the page's owner can resolve"}
```

## Retries and ambiguous failures

trk never assumes a failed write did not happen:

- **publish.** Before a retry, trk calls `find` with the page's tag and adopts the page it finds. Keep the tag with the page from the moment it is created, so a page made by a call whose answer was lost can be found.
- **update.** A retry carries the same `base_version`. If the first try was applied, the retry meets `conflict`, and trk reads the changed sections and goes on. That works only when the adapter enforces `base_version` in the same step as the write.
- **reply.** After an ambiguous failure trk reads the thread before sending again. `idempotency_key` stays the same across those sends: an adapter whose host can deduplicate by key should pass it on.

## Writing an adapter

In any language: read the verb from the last argument, read stdin, write one JSON object to stdout, and exit with the code above.

In Python, `trk.adapter.serve` does the protocol for you. Give it an object with the calls `trk.publisher` lists (`check`, `publish`, `update`, `sections`, `find_tagged`, and with `COMMENTS` also `list_comments`, `offers_activity`, `activity`, `reply`). Raise `trk.publisher.Held` for exit 2, `Refused` for exit 4, and a `ServiceError` with `ambiguous = True` for exit 3. `src/trk/folder.py` is a complete example:

```python
from trk import adapter

def main():
    return adapter.serve(lambda: MyBackend.from_argv(sys.argv[1:-1]))
```

Keep every secret inside the adapter: read it from a file or a keychain on each run, never from trk, and never print it.

## Checking an adapter

`python -m trk.conformance` runs the protocol against an adapter and prints one line per check:

```bash
python -m trk.conformance -- trk-pages-folder --dir /tmp/pages-check
```

It publishes one throwaway page tagged `trk:conformance-<random>`, so point it at a scratch directory, team or space. It checks `describe`, `check`, a request of another protocol, `publish` and `find`, conflicts on a stale `base_version`, patches and `sections`, the refusal codes, and `comments` and `activity` when the adapter has them. It exits 0 when every check passes.
