"""Build a store fixture with the trk CLI at a given commit; each schema's build.py names its steps.

Extracts src/ at <sha> with `git archive`, then drives that trk's main() in a fresh $TRK_HOME
under $TRK_TEST=1 with $TRK_NOW stepping from T0 = 2026-10-10T12:00Z plus each step's minutes, and
writes store.db, feed.head and BUILT_FROM into the fixture directory. The store ends checkpointed
with no -wal or -shm file. Never touches the live store.

A step's argv may name `{X}`, an id printed by an earlier step listed in `names`, and `{file_K}`,
the path of a file holding `files[K]`.
"""

import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def build(here, sha, steps, names, files=None):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        archive = subprocess.run(["git", "-C", str(REPO), "archive", sha, "src"], check=True, capture_output=True)
        tarfile.open(fileobj=io.BytesIO(archive.stdout)).extractall(tmp, filter="data")
        values = {}
        for key, body in (files or {}).items():
            path = tmp / f"{key}.md"
            path.write_text(body)
            values[f"file_{key}"] = str(path)
        home = tmp / "home"
        env = {**os.environ, "TRK_HOME": str(home), "TRK_TEST": "1", "PYTHONPATH": str(tmp / "src")}
        for var in ("TRK_ACTOR", "TRK_SESSION"):
            env.pop(var, None)
        for index, (minutes, argv) in enumerate(steps, start=1):
            argv = [arg.format_map(values) for arg in argv]
            env["TRK_NOW"] = f"2026-10-10T{12 + minutes // 60:02d}:{minutes % 60:02d}:00Z"
            done = subprocess.run(
                [sys.executable, "-c", "import sys; from trk.cli import main; sys.exit(main(sys.argv[1:]))", *argv],
                env=env, capture_output=True, text=True, check=False,
            )
            if done.returncode != 0:
                sys.exit(f"step {index} {argv} failed ({done.returncode}): {done.stderr}")
            if index in names:
                values[names[index]] = done.stdout.strip()
        conn = sqlite3.connect(home / "store.db")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        leftovers = [p.name for p in home.iterdir() if p.name.endswith(("-wal", "-shm"))]
        if leftovers:
            sys.exit(f"store not checkpointed: {leftovers}")
        for name in ("store.db", "feed.head"):
            shutil.copyfile(home / name, here / name)
    full_sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", sha], check=True, capture_output=True, text=True)
    (here / "BUILT_FROM").write_text(full_sha.stdout)
    print(f"built from {full_sha.stdout.strip()}: { {k: v for k, v in values.items() if not k.startswith('file_')} }")
