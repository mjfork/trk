"""The `folder` page backend: each page is an HTML file in a directory on this machine.

`trk pages setup --backend folder --dir PATH --home ID` sets it up; `--base-url URL` names where the
directory is served (a web server, a synced share), so the pages' links to each other work there.
Without it a page's URL is its `file://` path. There is no token and there are no comments, so the
pull never runs against it.

A page lives at `<dir>/<slug>.html`, its slug made from its tag (`trk:tr-abc12` → `tr-abc12`).
`<dir>/.trk-pages.json` keeps each page's tag, version and title. Every write replaces the file
whole (write, then rename), so a reader never sees half a page.
"""

import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path

from .publisher import Refused, ServiceError

INDEX = ".trk-pages.json"


class Backend:
    NAME = "folder"
    TOKEN = False
    COMMENTS = False
    GRANTS = ()

    def __init__(self, settings, token=None):
        if not settings.get("dir"):
            raise ServiceError("folder: no directory set: run `trk pages setup --backend folder --dir PATH`")
        self.dir = Path(settings["dir"])
        self.base_url = (settings.get("base_url") or "").rstrip("/")

    def uses(self, token):
        return token is None

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
        index = self._index()
        entry = index.get(slug)
        if entry is None or not (self.dir / f"{slug}.html").exists():
            raise Refused("update", "not_found")
        if edits is not None or body is not None:
            if base_version != entry["version"]:
                raise Refused("update", json.dumps({"error": "conflict", "baseVersion": base_version,
                                                    "currentVersion": entry["version"], "changedSections": []}))
            if edits is not None:
                body = _apply(self._read(slug), edits)
            self._write(slug, body)
            entry["version"] += 1
        if title is not None:
            entry["title"] = title
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
