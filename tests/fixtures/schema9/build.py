"""Rebuild the schema-9 store fixture with the client-names CLI at a given commit (the sha in BUILT_FROM).

    python tests/fixtures/schema9/build.py <sha>

See ../build_store.py for how a fixture is built. The store holds what the schema 10 (worktree and archive
location) migration upgrades: actors michael (default owner) and worker, program P with client acme and a
destination criterion, ticket T under it, and a live claim on T (session s-live, lease 24h).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_store import build  # noqa: E402

HERE = Path(__file__).resolve().parent

FILES = {"now": "Nothing records a tree.\n", "want": "Each item records its tree.\n"}

STEPS = [
    (0, ["init", "--me", "michael", "--display", "Michael Fork"]),
    (0, ["actor", "add", "worker", "--kind", "agent"]),
    (1, ["new", "program", "P", "--client", "acme", "--criterion", "Trees are recorded"]),               # P
    (2, ["new", "work-item", "T", "--form", "self-contained", "--parent", "{P}", "--criterion", "It works",
         "--section", "current-behaviour=@{file_now}", "--section", "desired-behaviour=@{file_want}"]),  # T
    (3, ["claim", "{T}", "--as", "worker", "--session", "s-live", "--lease", "24h"]),
]
NAMES = {3: "P", 4: "T"}  # step index -> placeholder


if __name__ == "__main__":
    build(HERE, sys.argv[1] if len(sys.argv) > 1 else "HEAD", STEPS, NAMES, FILES)
