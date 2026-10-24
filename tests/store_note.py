"""tr-fe457: subprocess tests run the real `trk` script with $TRK_HOME set, so every call starts its
stderr with the store line. They drop it here and assert on the rest; test_store_note.py covers the line."""

import subprocess


def without_note(done):
    lines = done.stderr.splitlines(keepends=True)
    if lines and lines[0].startswith("trk: store ") and lines[0].rstrip().endswith("(from $TRK_HOME)"):
        done = subprocess.CompletedProcess(done.args, done.returncode, done.stdout, "".join(lines[1:]))
    return done
