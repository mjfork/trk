"""Rebuild the schema-3 store fixture with the Handoffs CLI at a given commit (the sha in BUILT_FROM).

    python tests/fixtures/schema3/build.py <sha>

See ../build_store.py for how a fixture is built. The store holds what the schema 4 (Stage gates)
migration imports: program P with a destination list, accepted project J with prose criteria, under
it spec-bound ticket A with a checklist (open, so born Spec'd on upgrade), self-contained ticket B
with no sections (Framed, failing its gate), spec-bound ticket C with a checklist (closed done, so
its stage stays null), and decision E with an acceptance-criteria section (decisions take no
criteria, so it stays written); draft project K with spec-bound ticket D (Framed).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_store import build  # noqa: E402

HERE = Path(__file__).resolve().parent

FILES = {
    "destination": "Where Tracker v1 ends up:\n\n- Agents find work from data\n- Every write is journaled,\n  and replayable\n\nA closing note, not a statement.\n",
    "prose": "The whole flow works end to end,\nchecked by one run.\n",
    "checklist": "- [ ] `trk new` refuses an empty ticket\n- [x] the refusal lists what is missing\n  - nested detail\n1. numbered item\n",
    "what": "Build the thing.\n",
    "decision-ac": "- a decision's own list\n",
}

STEPS = [
    (0, ["init", "--me", "michael", "--display", "Michael Fork"]),
    (0, ["actor", "add", "worker", "--kind", "agent"]),
    (1, ["new", "program", "P", "--section", "destination=@{file_destination}"]),                        # P
    (2, ["new", "project", "J", "--parent", "{P}", "--section", "acceptance-criteria=@{file_prose}"]),   # J
    (3, ["spec", "accept", "{J}"]),
    (4, ["new", "work-item", "A", "--form", "spec-bound", "--parent", "{J}",
         "--section", "what-to-build=@{file_what}", "--section", "acceptance-criteria=@{file_checklist}"]),  # A
    (5, ["new", "work-item", "B", "--form", "self-contained", "--parent", "{J}"]),                         # B
    (6, ["new", "work-item", "C", "--form", "spec-bound", "--parent", "{J}",
         "--section", "acceptance-criteria=@{file_checklist}"]),                                          # C
    (7, ["close", "{C}", "--resolution", "done", "--outcome", "Done before the upgrade."]),
    (8, ["new", "work-item", "E", "--form", "decision", "--subtype", "task", "--parent", "{J}",
         "--section", "acceptance-criteria=@{file_decision-ac}"]),                                        # E
    (9, ["new", "project", "K", "--parent", "{P}"]),                                                      # K
    (10, ["new", "work-item", "D", "--form", "spec-bound", "--parent", "{K}",
          "--section", "what-to-build=@{file_what}"]),                                                    # D
]
NAMES = {3: "P", 4: "J", 6: "A", 7: "B", 8: "C", 10: "E", 11: "K", 12: "D"}  # step index -> placeholder


if __name__ == "__main__":
    build(HERE, sys.argv[1] if len(sys.argv) > 1 else "HEAD", STEPS, NAMES, FILES)
