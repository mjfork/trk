"""Rebuild the schema-5 store fixture with the Page publisher CLI at a given commit (the sha in BUILT_FROM).

    python tests/fixtures/schema5/build.py <sha>

See ../build_store.py for how a fixture is built. The store holds what the schema 6 (Needs-you pages)
migration upgrades: actors michael (default owner), kim (human, holding product) and worker, program P
with a destination criterion, and an open ask under P to product. Its page tables are empty: a test
fills them before the first open, since publishing needs an artifact service.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_store import build  # noqa: E402

HERE = Path(__file__).resolve().parent

FILES = {"question": "Which way?\n"}

STEPS = [
    (0, ["init", "--me", "michael", "--display", "Michael Fork"]),
    (0, ["actor", "add", "kim", "--kind", "human"]),
    (0, ["actor", "add", "worker", "--kind", "agent"]),
    (1, ["roles", "set", "product", "kim"]),
    (2, ["new", "program", "P", "--criterion", "Pages replace the boards"]),                             # P
    (3, ["ask", "{P}", "--to", "product", "Which way?", "--question", "@{file_question}", "--as", "worker"]),
]
NAMES = {5: "P"}  # step index -> placeholder


if __name__ == "__main__":
    build(HERE, sys.argv[1] if len(sys.argv) > 1 else "HEAD", STEPS, NAMES, FILES)
