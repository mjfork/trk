#!/usr/bin/env python3
"""Add or remove trk's entries in a Claude Code settings.json, touching nothing else.

    merge_settings.py add|remove --settings PATH --repo PATH [--dry-run]
    merge_settings.py allow-add|allow-remove|allow-status --settings PATH --repo PATH [--dry-run]

`add` and `remove` handle the hook entries. They come from hooks/settings-snippet.json in the repo,
with <TRK_REPO> replaced by the repo's absolute path. An entry whose command is already present is
left alone, so `add` twice changes nothing. `remove` deletes only entries with those exact commands.

`allow-add` and `allow-remove` handle one permission rule, `Bash(trk *)`, in `permissions.allow`:
without it a session asks before every trk command. install.sh adds it only when the person says so,
and removes it only when install.sh was the one that added it. `allow-status` writes `present` or
`absent` and changes nothing, so the installer can skip the question when the rule is already there.

When the file changes, the old file is copied to <settings>.trk-bak-<UTC time> first and the diff is
printed. Stdlib only.
"""

import argparse
import difflib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

RULE = "Bash(trk *)"


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


def allow_list(settings, path):
    """`permissions.allow` as a list, or None when the file has no such list. A file that shapes
    either one some other way stops the run: guessing at it could throw away someone's rules."""
    permissions = settings.get("permissions")
    if permissions is None:
        return None
    if not isinstance(permissions, dict):
        sys.exit(f"{path}: `permissions` is not a JSON object, so trk left it alone. Add {RULE!r} to "
                 f"the allow list by hand, or fix the file and run again. Nothing was changed.")
    allow = permissions.get("allow")
    if allow is None:
        return None
    if not isinstance(allow, list):
        sys.exit(f"{path}: `permissions.allow` is not a JSON array, so trk left it alone. Add {RULE!r} "
                 f"to it by hand, or fix the file and run again. Nothing was changed.")
    return allow


def allow_add(settings, path):
    allow = allow_list(settings, path)
    if allow is None:
        settings.setdefault("permissions", {})["allow"] = [RULE]
    elif RULE not in allow:
        allow.append(RULE)


def allow_remove(settings, path):
    allow = allow_list(settings, path)
    if allow is None or RULE not in allow:
        return
    permissions = settings["permissions"]
    permissions["allow"] = [rule for rule in allow if rule != RULE]
    if not permissions["allow"]:
        del permissions["allow"]
    if not permissions:
        del settings["permissions"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("add", "remove", "allow-add", "allow-remove", "allow-status"))
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
    if args.action == "allow-status":
        print("present" if RULE in (allow_list(settings, path) or []) else "absent")
        return 0
    if args.action == "add":
        add(settings, repo)
    elif args.action == "remove":
        remove(settings, repo)
    elif args.action == "allow-add":
        allow_add(settings, path)
    else:
        allow_remove(settings, path)
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
