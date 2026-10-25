"""`trk-pages-folder`, the reference page adapter: each page is an HTML file in a directory on this machine.

    trk pages setup --adapter "trk-pages-folder --dir PATH [--base-url URL]" --home ID

`--base-url URL` names where the directory is served (a web server, a synced share), so the pages' links
to each other work there. Without it a page's URL is its `file://` path. There are no credentials and no
comments, so the pull never runs against it.

A page lives at `<dir>/<slug>.html`, its slug made from its tag (`trk:tr-abc12` → `tr-abc12`).
`<dir>/.trk-pages.json` keeps each page's tag, version and title. Every write replaces the file whole
(write, then rename), so a reader never sees half a page, and every write holds `<dir>/.trk-pages.lock`,
so two writers never lose each other's index changes. The page is written before the index: a crash
between the two leaves the index one version behind the file, and the next write on that version
replaces the file whole or patches it, so nothing is lost.
"""

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from .publisher import Held, Refused, ServiceError

INDEX = ".trk-pages.json"
LOCK = ".trk-pages.lock"


class Backend:
    NAME = "folder"
    COMMENTS = False
    expires = None

    def __init__(self, folder, base_url=None):
        self.dir = Path(folder)
        self.base_url = (base_url or "").rstrip("/")

    def check(self):
        """The directory exists, or can be made, and takes a file."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.dir, prefix=".trk-check-"):
                pass
        except OSError as exc:
            raise ServiceError(f"check: cannot write to {self.dir}: {exc.strerror or exc}") from None
        return {"dir": str(self.dir)}

    def publish(self, *, title, body, team, tags):
        with self._locked():
            return self._publish(title, body, tags)

    def _publish(self, title, body, tags):
        index = self._index()
        tag = next(iter(tags), "")
        slug = _slug(tag) or f"page-{len(index) + 1}"
        base, n = slug, 1
        while slug in index and index[slug].get("tag") != tag:
            n += 1
            slug = f"{base}-{n}"
        version = index.get(slug, {}).get("version", 0) + 1
        self._write(slug, body)
        index[slug] = {"tag": tag, "version": version, "title": title}
        self._save(index)
        return {"slug": slug, "url": self.url(slug), "version": version}

    def update(self, slug, *, base_version, edits=None, body=None, title=None):
        with self._locked():
            return self._update(slug, base_version, edits, body, title)

    def _update(self, slug, base_version, edits, body, title):
        index = self._index()
        entry = index.get(slug)
        if entry is None or not (self.dir / f"{slug}.html").exists():
            raise Refused("update", "not_found")
        if base_version != entry["version"]:
            raise Refused("update", json.dumps({"error": "conflict", "baseVersion": base_version,
                                                "currentVersion": entry["version"], "changedSections": []}))
        if edits is not None or body is not None:
            if edits is not None:
                body = _apply(self._read(slug), edits)
            self._write(slug, body)
        if title is not None:
            entry["title"] = title
        entry["version"] += 1
        self._save(index)
        return {"slug": slug, "url": self.url(slug), "version": entry["version"]}

    def sections(self, slug, *, handles):
        from .pages import parse  # pages imports the backends' errors, not the backends

        parsed = parse(self._read(slug))
        found = [{"id": handle, "source": parsed.outer(handle)} for handle in handles if handle in parsed.spans]
        missing = [handle for handle in handles if handle not in parsed.spans]
        return {"sections": found, **({"missing": missing} if missing else {})}

    def find_tagged(self, tag):
        for slug, entry in self._index().items():
            if entry.get("tag") == tag and (self.dir / f"{slug}.html").exists():
                return {"slug": slug, "url": self.url(slug), "version": entry["version"]}
        return None

    def url(self, slug):
        if self.base_url:
            return f"{self.base_url}/{urllib.parse.quote(slug)}.html"
        return (self.dir.resolve() / f"{slug}.html").as_uri()

    # ----- files ---------------------------------------------------------------------------

    @contextmanager
    def _locked(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            handle = open(self.dir / LOCK, "a")
        except OSError as exc:
            raise ServiceError(f"folder: cannot lock {self.dir / LOCK}: {exc.strerror or exc}") from None
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _index(self):
        try:
            with open(self.dir / INDEX, encoding="utf-8") as f:
                index = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise ServiceError(f"folder: cannot read {self.dir / INDEX}: {exc}") from None
        return index if isinstance(index, dict) else {}

    def _save(self, index):
        self._replace(self.dir / INDEX, json.dumps(index, indent=2, sort_keys=True) + "\n")

    def _read(self, slug):
        try:
            return (self.dir / f"{slug}.html").read_text(encoding="utf-8")
        except FileNotFoundError:
            raise Refused("sections", "not_found") from None

    def _write(self, slug, body):
        self._replace(self.dir / f"{slug}.html", body)

    def _replace(self, path, text):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".trk-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError as exc:
            raise ServiceError(f"folder: cannot write {path}: {exc.strerror or exc}") from None


def _slug(tag):
    """`trk:tr-abc12` → `tr-abc12`, `trk:needs-you:kim` → `needs-you-kim`: letters, digits, `-`, `_`."""
    text = tag.removeprefix("trk:")
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")


def _apply(html, edits):
    """A patch: each edit replaces its handle's element whole. The edits are outermost and don't
    overlap, so they go in from the end of the page back."""
    from .pages import parse

    parsed = parse(html)
    for edit in edits:
        if edit["section"] not in parsed.spans:
            raise Refused("update", f"edit_not_applied (unknown section {edit['section']!r})")
    for edit in sorted(edits, key=lambda e: parsed.spans[e["section"]][0], reverse=True):
        start, end = parsed.spans[edit["section"]]
        html = html[:start] + edit["replace"] + html[end:]
    return html


def main(argv=None):
    """`trk-pages-folder --dir PATH [--base-url URL] <verb>`, the request on stdin (trk.adapter)."""
    from . import adapter

    argv = sys.argv[1:] if argv is None else argv
    return adapter.serve(lambda: from_argv(argv[:-1]), argv[-1:])


def from_argv(argv):
    """The backend for these options; Held (exit 2) when they are wrong."""
    parser = argparse.ArgumentParser(prog="trk-pages-folder", add_help=False, exit_on_error=False)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--base-url")
    try:
        args, rest = parser.parse_known_args(argv)
    except (argparse.ArgumentError, SystemExit) as exc:
        raise Held(f"usage: trk-pages-folder --dir PATH [--base-url URL] VERB ({exc})") from None
    if rest:
        raise Held(f"usage: trk-pages-folder --dir PATH [--base-url URL] VERB (unknown: {' '.join(rest)})")
    if args.base_url:
        parsed = urllib.parse.urlsplit(args.base_url)
        if parsed.scheme not in ("http", "https", "file") or (parsed.scheme != "file" and not parsed.hostname):
            raise Held(f"folder: --base-url must be an http, https or file URL, not {args.base_url!r}")
    return Backend(os.path.abspath(os.path.expanduser(args.dir)), args.base_url)


if __name__ == "__main__":
    sys.exit(main())
