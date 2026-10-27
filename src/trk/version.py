"""`trk --version`: the package version, the commit the running trk was built from, whether its tree is the
installed trk, and the store schema this code speaks. Opens no store.

A source tree that sits in a checkout answers live, so an editable install always reports the working tree.
A copy installed with `--no-editable` has no checkout around it, and reads the commit from `_build`, the
record the build hook (`hatch_build.py`) puts in the wheel. So any build stamps itself, however it was
started, and nothing is ever written into the working tree. `dirty` says whether that source had uncommitted
changes **when it was built**, not now (added 2026-10-27, for an installer that skips a reinstall already at
its pinned commit)."""

import subprocess
from importlib.metadata import PackageNotFoundError, version as dist_version

from . import store


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
    """(commit, dirty) for the source `tree`: its live checkout when it sits in one, else the record the
    build hook put in the wheel this copy came from, else (None, None). Live wins, so a record that somehow
    sits in a checkout never speaks for it."""
    sha = _commit(tree)
    if sha is not None:
        return sha, _dirty(tree)
    return _build_record()


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
    if not isinstance(sha, str) or len(sha) != 40:
        return None, None
    return sha, bool(getattr(_build, "DIRTY", False))


def _dirty(tree):
    """Whether the checkout `tree` sits in has uncommitted changes; None when git cannot say."""
    try:
        done = subprocess.run(["git", "-C", str(tree), "status", "--porcelain"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(done.stdout.strip()) if done.returncode == 0 else None


def _commit(tree):
    """HEAD of the checkout `tree` sits in; None when git is missing or `tree` is not in a checkout."""
    try:
        done = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and len(sha) == 40 else None
