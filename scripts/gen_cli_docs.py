#!/usr/bin/env python3
"""Write docs/cli.md: every trk verb's `--help`, in the order `trk --help` lists them.

    python scripts/gen_cli_docs.py           rewrite docs/cli.md
    python scripts/gen_cli_docs.py --check   exit 1 if docs/cli.md is out of date

Run it from the repo with the checkout's source on the path (PYTHONPATH=src). Help is rendered at
80 columns so the file does not depend on the terminal. tests/test_docs.py keeps docs/cli.md current.
"""

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "docs" / "cli.md"
WIDTH = "80"

HEADER = """# trk command reference

This file is generated from `trk <verb> --help`. Do not edit it. Regenerate it with
`PYTHONPATH=src python scripts/gen_cli_docs.py` after a verb changes; a test fails until you do.

Every verb accepts `--as ACTOR` (who is writing), `--session ID` and `--json` (machine-readable output).
Exit codes: 0 ok, 1 refused by a rule, 2 usage error, 3 not found, 4 conflict.
For what the verbs mean, read [README.md](../README.md) and [CONTEXT.md](../CONTEXT.md).
"""


def verbs(parser, prefix=()):
    """(words, subparser) for every verb and subverb, depth first, in the order `--help` lists them."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                words = (*prefix, *name.split())
                yield words, sub
                yield from verbs(sub, words)


def render():
    sys.path.insert(0, str(REPO / "src"))
    old = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = WIDTH
    try:
        from trk.cli import build_parser

        parts = [HEADER]
        for words, sub in verbs(build_parser()):
            parts.append(f"## trk {' '.join(words)}\n\n```text\n{sub.format_help().rstrip()}\n```\n")
        return "\n".join(parts)
    finally:
        if old is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = old


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = render()
    if args.check:
        if TARGET.read_text() != text:
            print("docs/cli.md is out of date; run scripts/gen_cli_docs.py", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(text)
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
