#!/usr/bin/env python3
"""Add or remove trk's Claude Code hook entries in a settings.json, touching nothing else.

    merge_settings.py add|remove --settings PATH --repo PATH [--dry-run]

The entries come from hooks/settings-snippet.json in the repo, with <TRK_REPO> replaced by the
repo's absolute path. An entry whose command is already present is left alone, so `add` twice
changes nothing. `remove` deletes only entries with those exact commands. When the file changes,
the old file is copied to <settings>.trk-bak-<UTC time> first and the diff is printed. Stdlib only.
"""

import argparse
import difflib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path


def wanted(repo):
    text = (repo / "hooks" / "settings-snippet.json").read_text().replace("<TRK_REPO>", str(repo))
    return json.loads(text)["hooks"]


def commands(entry):
    return {h.get("command") for h in entry.get("hooks", [])}


def add(settings, repo):
    hooks = settings.setdefault("hooks", {})
    for event, entries in wanted(repo).items():
        have = hooks.setdefault(event, [])
        for entry in entries:
            if not any(commands(entry) & commands(e) for e in have):
                have.append(entry)


def remove(settings, repo):
    ours = {c for entries in wanted(repo).values() for e in entries for c in commands(e)}
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        kept = []
        for entry in hooks[event]:
            rest = [h for h in entry.get("hooks", []) if h.get("command") not in ours]
            if rest:
                kept.append({**entry, "hooks": rest})
            elif not entry.get("hooks"):
                kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("add", "remove"))
    ap.add_argument("--settings", required=True, type=Path)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    path, repo = args.settings, args.repo.resolve()
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        settings = json.loads(before) if before.strip() else {}
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON ({exc}); fix it and run again. Nothing was changed.")
    if not isinstance(settings, dict):
        sys.exit(f"{path} does not hold a JSON object. Nothing was changed.")
    (add if args.action == "add" else remove)(settings, repo)
    after = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    if before.strip() and json.loads(before) == settings or not before.strip() and not settings:
        print(f"settings: {path} already up to date")
        return 0
    diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"{path} (old)", f"{path} (new)"))
    print(diff, end="")
    if args.dry_run:
        print(f"settings: would change {path}")
        return 0
    if path.exists():
        backup = path.with_name(f"{path.name}.trk-bak-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
        shutil.copy2(path, backup)
        print(f"settings: backed up to {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after, encoding="utf-8")
    print(f"settings: updated {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
