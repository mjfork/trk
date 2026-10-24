"""Rebuild the schema-4 store fixture with the Stage gates CLI at a given commit (the sha in BUILT_FROM).

    python tests/fixtures/schema4/build.py <sha>

See ../build_store.py for how a fixture is built. The store holds what the schema 5 (Page publisher)
migration upgrades: actors michael (default owner), herdr and worker, program P with a destination
statement, project J under it, and spec-bound ticket T under J with an open ask to product.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_store import build  # noqa: E402

HERE = Path(__file__).resolve().parent

FILES = {"what": "Build the thing.\n", "question": "Which way?\n"}

STEPS = [
    (0, ["init", "--me", "michael", "--display", "Michael Fork"]),
    (0, ["actor", "add", "worker", "--kind", "agent"]),
    (1, ["new", "program", "P", "--criterion", "Pages replace the boards"]),                             # P
    (2, ["new", "project", "J", "--parent", "{P}"]),                                                     # J
    (3, ["new", "work-item", "T", "--form", "spec-bound", "--parent", "{J}",
         "--section", "what-to-build=@{file_what}", "--criterion", "It renders"]),                      # T
    (4, ["ask", "{T}", "--to", "product", "Which way?", "--question", "@{file_question}", "--as", "worker"]),
]
NAMES = {3: "P", 4: "J", 5: "T"}  # step index -> placeholder


if __name__ == "__main__":
    build(HERE, sys.argv[1] if len(sys.argv) > 1 else "HEAD", STEPS, NAMES, FILES)
