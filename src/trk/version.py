"""`trk --version`: the package version, the commit the running trk was built from, whether its tree is the
installed trk, and the store schema this code speaks. Opens no store.

A source tree that sits in a checkout answers live, so an editable install always reports the working tree.
A copy installed with `--no-editable` has no checkout around it, and reads the commit from `_build`, the
record the build hook (`hatch_build.py`) puts in the wheel. So any build stamps itself, however it was
started, and nothing is ever written into the working tree. `dirty` says whether that source had uncommitted
changes **when it was built**, not now (added 2026-10-27, for an installer that skips a reinstall already at
its pinned commit)."""

import re
import subprocess
from importlib.metadata import PackageNotFoundError, version as dist_version
from pathlib import Path

from . import store

SHA = re.compile(r"[0-9a-f]{40}")


def report():
    running = store.running_source()
    commit, dirty = provenance(running)
    return {
        "schema": 1,
        "version": _package_version(),
        "commit": commit,
        "dirty": dirty,
        "installed": running in store.installed_sources(),
        "store_schema": store.SCHEMA_VERSION,
    }


def provenance(tree):
    """(commit, dirty) for the source `tree`: its own checkout when it is one, else the record the build
    hook put in the wheel this copy came from, else (None, None). Live wins over the record, so a record
    that somehow sits in a checkout never speaks for it."""
    root = _checkout_root(tree)
    if root is not None:
        sha = _commit(root)
        if sha is not None:
            return sha, _dirty(root)
    return _build_record()


def _checkout_root(tree):
    """The checkout `tree` is the `src` of, or None when `tree` is not one. An editable install imports
    from `<root>/src`, so the repository's top level must be `tree`'s parent.

    A repository that merely *contains* this copy is some other project and its HEAD says nothing about
    this trk: an installed copy under a `$HOME` that is a dotfiles repo would otherwise report the
    dotfiles commit, and its dirty state, instead of the commit baked into its wheel. This is the mirror
    of the same guard `hatch_build.head` applies at build time (found by an audit of 7002060, 2026-10-27).
    """
    top = _git(tree, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    try:
        return Path(top) if Path(top).resolve() == Path(tree).parent.resolve() else None
    except OSError:
        return None


def text(reply):
    running = store.running_source()
    return "\n".join([
        f"trk {reply['version'] or 'unknown'}",
        f"commit: {reply['commit'] or 'unknown'}" + (" (dirty)" if reply.get("dirty") else ""),
        f"installed: {'yes' if reply['installed'] else 'no'} ({'' if reply['installed'] else 'runs from '}{running})",
        f"store schema: {reply['store_schema']}",
    ])


def _package_version():
    try:
        return dist_version("trk")
    except PackageNotFoundError:
        return None


def _build_record():
    """(commit, dirty) from `_build`, the module `hatch_build.py` writes into a wheel, or (None, None) when
    this copy was not built from a checkout (the module is absent, or says nothing a 40-hex sha could be
    read from). Only a build writes it, so it is absent in every checkout and from every editable build."""
    try:
        from . import _build
    except ImportError:
        return None, None
    sha = getattr(_build, "COMMIT", None)
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        return None, None
    return sha, bool(getattr(_build, "DIRTY", False))


def _dirty(root):
    """Whether the checkout at `root` has uncommitted changes; None when git cannot say. Untracked files
    count: a tree carrying source nobody has committed is not clean."""
    answer = _git(root, "status", "--porcelain")
    return None if answer is None else bool(answer)


def _commit(root):
    """HEAD of the checkout at `root`; None when git is missing or cannot say."""
    sha = _git(root, "rev-parse", "HEAD")
    return sha if sha is not None and SHA.fullmatch(sha) else None


def _git(root, *args):
    """A git command's output at `root`, stripped, or None when git is missing or the command failed."""
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None
