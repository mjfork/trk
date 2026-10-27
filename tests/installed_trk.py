"""Drives the `trk` on PATH (the project's console script) in subprocesses, with $TRK_NOW under the test's control."""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from store_note import without_note

TRK = shutil.which("trk")
MISSING = "trk is not on PATH; run `uv tool install --editable .`"
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent


def _out(*argv, cwd=None):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=15, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _path_trk_commit():
    """(commit, dirty) the `trk` on PATH says it was built from; (None, None) when it cannot say — which is
    the answer from any copy built before trk started stamping itself (7002060)."""
    if not TRK:
        return None, None
    reply = _out(TRK, "--version", "--json")
    try:
        answer = json.loads(reply) if reply else {}
    except json.JSONDecodeError:
        return None, None
    return answer.get("commit"), answer.get("dirty")


def _stale():
    """Why the `trk` on PATH cannot stand in for this checkout, or None when it can.

    These files drive the binary, not the source, so their expectations are about a *commit*. An older
    installed trk silently answers for a different one: that is how `trk init` registering `worker` passed
    here and failed on a box whose PATH trk was built from the commit under test. A copy that cannot say
    which commit it is counts as stale, because nothing proves it matches.
    """
    head = _out("git", "-C", str(ROOT), "rev-parse", "HEAD")
    if head is None:
        return None                                  # not a checkout (an installed copy's own tests): nothing to compare
    how_to = (f"reinstall it from this checkout (`./install.sh --no-editable --force`, or "
              f"`uv tool install --force .`) or put a trk built from {head[:8]} first on PATH")
    if _out("git", "-C", str(ROOT), "status", "--porcelain"):
        return f"this checkout has uncommitted changes, so no built trk answers for it; commit, then {how_to}"
    commit, dirty = _path_trk_commit()
    if commit is None:
        return f"the trk on PATH does not say which commit it was built from, so it cannot answer for {head[:8]}: {how_to}"
    if commit != head:
        return f"the trk on PATH is {commit[:8]}, not this checkout's {head[:8]}: {how_to}"
    if dirty:
        return f"the trk on PATH was built from a dirty {head[:8]}, so its source is not this commit: {how_to}"
    return None


STALE = _stale()
# A release run sets this, so a stale PATH trk fails `test_path_trk` loudly instead of quietly skipping
# every file that drives the binary.
MUST_MATCH = os.environ.get("TRK_PATH_MUST_MATCH") == "1"
GATE = MISSING if not TRK else STALE   # None when the binary on PATH can answer for this checkout


class Trk:
    """Runs the installed binary; `now` sets $TRK_NOW for every following command."""

    def __init__(self):
        self.now = T0

    def __call__(self, *argv, stdin=None):
        env = {**os.environ, "TRK_NOW": self.now.isoformat()}
        return without_note(subprocess.run([TRK, *argv], capture_output=True, text=True, env=env, input=stdin,
                                           check=False))

    def ok(self, *argv, stdin=None):
        done = self(*argv, stdin=stdin)
        assert done.returncode == 0, (argv, done.returncode, done.stderr)
        return done.stdout.strip()

    def json(self, *argv, stdin=None):
        return json.loads(self.ok(*argv, "--json", stdin=stdin))

    def error(self, *argv, stdin=None):
        done = self(*argv, "--json", stdin=stdin)
        return done.returncode, json.loads(done.stderr)["error"]

    def decision(self, title, parent, *flags):
        return self.ok("new", "work-item", title, "--form", "decision", "--subtype", "task", "--parent", parent, *flags)

    def feed(self, since=0, *flags):
        return self.json("changes", "--since", str(since), *flags)
