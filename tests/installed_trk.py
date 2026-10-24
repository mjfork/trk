"""Drives the `trk` on PATH (the project's console script) in subprocesses, with $TRK_NOW under the test's control."""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime

from store_note import without_note

TRK = shutil.which("trk")
MISSING = "trk is not on PATH; run `uv tool install --editable .`"
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


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
