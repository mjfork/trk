"""Gated creation for fixtures: the flags `trk new` needs so an item's creation gate holds (Stage gates).

A program needs one destination statement (`*DEST`); a ticket needs its written sections and one
acceptance criterion (`*ticket("spec-bound")`). Section bodies are files beside the test's store.
A ticket closes `done` only by reaching Verified: `walk_to_verified` takes it there (`walk_to_merged` stops
at Merged).
"""

import json
import os
import subprocess
from pathlib import Path

DEST = ("--criterion", "The destination holds.")
CRITERION = ("--criterion", "It works.")

# The project sections `spec accept` needs written (`acceptance-criteria` is generated from criterion rows);
# the CLI's list is stages.SPEC_SECTIONS.
PROJECT_SECTIONS = ("problem", "solution", "user-stories", "implementation-decisions", "testing-decisions",
                    "out-of-scope")

WRITTEN = {"spec-bound": ("what-to-build",), "self-contained": ("current-behaviour", "desired-behaviour")}


def section_file(handle, body=None):
    """A file holding a section body, in a directory beside the test's $TRK_HOME."""
    directory = Path(os.environ["TRK_HOME"]).parent / "gated-sections"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{handle}.md"
    path.write_text(body if body is not None else f"The {handle} of a test ticket.\n")
    return path


def ticket(form, skip=()):
    """`trk new work-item … --form <form>` flags for a ticket born Framed: its written sections (but
    those in `skip`) and one criterion."""
    flags = []
    for handle in WRITTEN[form]:
        if handle not in skip:
            flags += ["--section", f"{handle}=@{section_file(handle)}"]
    return (*flags, *CRITERION)


def new_flags(argv):
    """The gate flags a `new` argv (kind, title, flags…) still lacks: none when it gives a --criterion
    already, or when its kind and form need none. Sections the argv writes are left to it."""
    if "--criterion" in argv:
        return ()
    if argv[0] == "program":
        return DEST
    if argv[0] == "work-item" and "--form" in argv:
        form = argv[argv.index("--form") + 1]
        if form in WRITTEN:
            given = {flag.partition("=")[0] for flag in argv if "=" in flag}
            return ticket(form, skip=given)
    return ()


def acceptable(project):
    """`trk` argvs that make a project's spec acceptable: every spec section written, one criterion and a
    `judgment` for gate `accept`."""
    return [*(("section", "set", project, handle, "--file", str(section_file(handle))) for handle in PROJECT_SECTIONS),
            ("criterion", "add", project, "The spec holds."),
            ("evidence", "add", project, "--kind", "judgment", "--gate", "accept", "--note", "The spec is complete.")]


def accepting(project):
    """`trk` argvs that accept a project's spec through its gate."""
    return [*acceptable(project), ("spec", "accept", project)]


def closable(item_id, criteria=("ac-1",)):
    """`trk` argvs that let a project close `done`: a passing `judgment` for gate `close` on each of its live
    criteria."""
    return [("evidence", "add", item_id, "--kind", "judgment", "--gate", "close", "--criterion", handle,
             "--note", "Delivered by its children.") for handle in criteria]


def closable_program(item_id, texts):
    """`trk` argvs that let a program close `done`, which takes no `judgment` and reads a run against GitHub:
    each live criterion (handle -> text) is edited to name `michael` as its acceptor, who then records a
    `human-accept` for it (as the store's default actor)."""
    return [argv for handle, text in texts.items() for argv in (
        ("criterion", "edit", item_id, handle, f"{text} accept-by: michael"),
        ("evidence", "add", item_id, "--kind", "human-accept", "--gate", "close", "--criterion", handle,
         "--ref", "page", "--verdict", "pass", "--note", "Accepted by the named acceptor."))]


def resolving(decision):
    """`trk` argvs that let a decision close `done`: its `resolution` written."""
    return [("section", "set", decision, "resolution", "--file", str(section_file("resolution")))]


def run_all(run, argvs):
    """Run each argv through a `trk` fixture, asserting it succeeds."""
    for argv in argvs:
        result = run(*argv)
        assert result.code == 0, (argv, result.err)


def accept(run, project):
    """Accept a project's spec through its gate, with a `trk` fixture."""
    run_all(run, accepting(project))


def resolve(run, decision):
    """Write a decision's `resolution`, so it may close `done`, with a `trk` fixture."""
    run_all(run, resolving(decision))


def prove_close(run, item_id):
    """Meet an open item's `close` gate but its children: a decision's `resolution`, or a passing `close`
    row on every live criterion (a project's a judgment, a program's a `human-accept` from `michael`), adding a criterion when none is live, a draft
    project's spec accepted first. Unknown, closed and other items are left alone, so the close under test
    still meets its own refusals."""
    shown = run("show", item_id, "--json")
    if shown.code != 0 or shown.json()["item"]["status"] != "open":
        return
    item = shown.json()["item"]
    if item["spec_state"] == "draft":
        accept(run, item_id)
        item = run("show", item_id, "--json").json()["item"]
    if item["form"] == "decision":
        resolve(run, item_id)
    elif item["kind"] in ("program", "project"):
        live = {c["handle"]: c["text"] for c in item["criteria"] if not c["retired"]}
        if not live:
            run_all(run, [("criterion", "add", item_id, "It is delivered.")])
            live = {"ac-1" if not item["criteria"] else f"ac-{len(item['criteria']) + 1}": "It is delivered."}
        if item["kind"] == "program":
            run_all(run, closable_program(item_id, live))
        else:
            run_all(run, closable(item_id, list(live)))


# --- walking a ticket to Verified ---

def git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *argv],
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, name, body=None):
    """Write `name` (directories made as needed), commit it, and return the new head sha."""
    path = Path(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if body is not None else f"{name}\n")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def _gated_repo():
    """A one-commit checkout beside the test's $TRK_HOME, made on first use: (path, its sha)."""
    path = Path(os.environ["TRK_HOME"]).parent / "gated-repo"
    if not path.exists():
        path.mkdir(parents=True)
        git(path, "init", "-q", "-b", "main")
        commit(path, ".trk/blast-radius", "# no rules: every change rates green\n")
        git(path, "checkout", "-q", "-b", "feature")  # main stays the branch the work merges into
        commit(path, "work")
    return str(path), git(path, "rev-parse", "HEAD")


def _branch_point(repo, sha):
    """The commit a fixture ticket's rating starts from: the parent of `sha` (a base at `sha` itself hides the PR and is
    refused `base-hides-pr`), else `sha` for a repo of one commit."""
    try:
        return git(repo, "rev-parse", f"{sha}^")
    except subprocess.CalledProcessError:
        return sha


def walk_to_verified(ok, ticket_id, outcome="Done.", sha=None, repo=None):
    """Take an open ticket from where it is to Verified, closing it `done`, through every gate.

    `ok(*argv)` runs a trk command that must succeed and returns its stdout. The ticket must be able
    to reach Spec'd (a spec-bound ticket's project accepted; a self-contained ticket is judged here),
    and must be at most at Merged.
    Without `repo`, every row is at the one commit of a throwaway checkout the ticket itself never
    names, so its shas compare by prefix and the rating is GREEN (the one file of its last commit)."""
    if repo is None:
        repo, sha = _gated_repo()
    walk_to_merged(ok, ticket_id, sha, repo)
    for criterion in json.loads(ok("criteria", ticket_id, "--json"))["criteria"]:
        if not criterion["retired"]:
            ok("evidence", "add", ticket_id, "--kind", "test-run", "--gate", "verified", "--criterion",
               criterion["handle"], "--ref", "run-2", "--sha", sha, "--verdict", "pass", "--note", "It holds.")
    return ok("advance", ticket_id, "--outcome", outcome)


def walk_to_merged(ok, ticket_id, sha=None, repo=None):
    """Take an open ticket from where it is to Merged through every gate, as walk_to_verified does."""
    if repo is None:
        repo, sha = _gated_repo()
    while True:
        shown = json.loads(ok("show", ticket_id, "--json"))["item"]
        stage = shown["stage"]
        if stage == "framed":
            judged = ("--judgment", "Complete.") if shown["form"] == "self-contained" else ()
            ok("advance", ticket_id, *judged)
        elif stage == "specd":
            ok("evidence", "add", ticket_id, "--kind", "pr", "--gate", "built", "--ref",
               "https://example.test/pull/1", "--sha", sha, "--note", "The PR.")
            ok("evidence", "add", ticket_id, "--kind", "test-run", "--gate", "built", "--ref", "run-1", "--sha", sha,
               "--verdict", "pass", "--note", "The suite passed.")
            ok("advance", ticket_id)
        elif stage == "built":
            ok("blast-radius", ticket_id, "--sha", sha, "--base", _branch_point(repo, sha), "--repo", repo)
            ok("evidence", "add", ticket_id, "--kind", "merge", "--gate", "merged", "--ref",
               "https://example.test/pull/1", "--sha", sha, "--note", "Merged.")
            ok("advance", ticket_id)
        else:
            break
