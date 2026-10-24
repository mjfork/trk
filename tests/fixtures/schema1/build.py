"""Rebuild a store fixture with the CLI at a given commit (the sha in BUILT_FROM).

    python tests/fixtures/schema1/build.py <sha> [<out-dir>]

The out dir defaults to this one (schema 1, from Core); tests/fixtures/schema2/ is the same steps
run by the Asks and owners CLI. See ../build_store.py for how a fixture is built.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_store import build  # noqa: E402

HERE = Path(__file__).resolve().parent

# Times are T0 = 2026-10-10T12:00Z plus minutes. At T0+120 the first claim has lapsed and the
# second is live until T0+600; the migration test opens the store at T0+150.
STEPS = [
    (0, ["init", "--me", "michael", "--display", "Michael Fork"]),
    (0, ["actor", "add", "worker", "--kind", "agent"]),
    (1, ["new", "program", "Ownerless program", "--as", "worker"]),               # P: top-level, no owner
    (2, ["new", "program", "Owned program", "--owner", "worker"]),                # O: top-level, own owner
    (3, ["new", "project", "Project J", "--parent", "{P}"]),                       # J
    (4, ["new", "work-item", "Ticket A", "--form", "self-contained", "--parent", "{J}"]),  # A
    (5, ["new", "work-item", "Ticket B", "--form", "self-contained", "--parent", "{J}"]),  # B
    (6, ["new", "work-item", "Ticket C", "--form", "self-contained", "--parent", "{J}"]),  # C
    (7, ["link", "{A}", "blocks", "{B}"]),
    (8, ["close", "{C}", "--resolution", "done", "--outcome", "Done before the migration."]),
    (9, ["claim", "{A}", "--session", "s-lapsed", "--lease", "1h", "--as", "worker"]),
    (120, ["sweep"]),
    (121, ["claim", "{A}", "--session", "s-live", "--lease", "8h", "--as", "worker"]),
]
NAMES = {3: "P", 4: "O", 5: "J", 6: "A", 7: "B", 8: "C"}  # step index -> placeholder its printed id fills


if __name__ == "__main__":
    build(Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else HERE, sys.argv[1] if len(sys.argv) > 1 else "HEAD",
          STEPS, NAMES)
