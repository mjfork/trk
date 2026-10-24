"""One writer of the concurrency test: 50 mixed writes on the store at $TRK_HOME, each journaling one row.

Even-numbered writers create, edit, section, link and claim decisions; odd-numbered ones create a decision,
open an ask on it as the agent `bot`, readdress it, answer it as the default actor, and claim the decision.

    python concurrency_worker.py <binary|main> <worker-number> <parent-id> <section-file>

`binary` runs every write as a `trk` subprocess; `main` calls `trk.cli.main` in this process, which
contends harder for the write lock. Every write carries `--session w<n>`, so the test can find this
worker's rows. Exits 1 on the first write that fails, printing its argv, exit code and stderr.
"""

import contextlib
import io
import shutil
import subprocess
import sys

CYCLES = 10  # five writes per cycle: 50 writes


def runner(mode):
    if mode == "binary":
        trk = shutil.which("trk")

        def run(*argv):
            done = subprocess.run([trk, *argv], capture_output=True, text=True, check=False)
            return done.returncode, done.stdout, done.stderr

        return run

    from trk.cli import main

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    return run


def write(run, *argv):
    code, out, err = run(*argv)
    if code != 0:
        print(f"{argv}: exit {code}: {err}", file=sys.stderr)
        sys.exit(1)
    return out.strip()


def decisions(run, number, parent, section_file, session):
    previous = parent
    for cycle in range(CYCLES):
        title = f"w{number}-{cycle}"
        item = write(run, "new", "work-item", title, "--form", "decision", "--subtype", "task",
                     "--parent", parent, "--session", session)
        write(run, "edit", item, "--title", f"{title} edited", "--session", session)
        write(run, "section", "set", item, "question", "--file", section_file, "--session", session)
        write(run, "link", previous, "relates", item, "--session", session)
        write(run, "claim", item, "--session", session)
        previous = item


def asks(run, number, parent, section_file, session):
    for cycle in range(CYCLES):
        item = write(run, "new", "work-item", f"w{number}-{cycle} asked", "--form", "decision", "--subtype", "task",
                     "--parent", parent, "--session", session)
        ask = write(run, "ask", item, "--to", "product", f"w{number}-{cycle}?", "--question", f"@{section_file}",
                    "--as", "bot", "--session", session)
        write(run, "readdress", ask, "--to", "engineering", "--as", "bot", "--session", session)
        write(run, "answer", ask, "--outcome", "Settled.", "--answer", f"@{section_file}", "--session", session)
        write(run, "claim", item, "--session", session)


def main():
    mode, number, parent, section_file = sys.argv[1:]
    writes = asks if int(number) % 2 else decisions
    writes(runner(mode), number, parent, section_file, f"w{number}")


if __name__ == "__main__":
    main()
