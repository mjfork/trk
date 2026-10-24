"""`trk --version`: the package version, the git commit of the source tree this process runs from, whether that tree
is the installed trk, and the store schema this code speaks. Opens no store."""

import subprocess
from importlib.metadata import PackageNotFoundError, version as dist_version

from . import store


def report():
    running = store.running_source()
    return {
        "schema": 1,
        "version": _package_version(),
        "commit": _commit(running),
        "installed": running in store.installed_sources(),
        "store_schema": store.SCHEMA_VERSION,
    }


def text(reply):
    running = store.running_source()
    return "\n".join([
        f"trk {reply['version'] or 'unknown'}",
        f"commit: {reply['commit'] or 'unknown'}",
        f"installed: {'yes' if reply['installed'] else 'no'} ({'' if reply['installed'] else 'runs from '}{running})",
        f"store schema: {reply['store_schema']}",
    ])


def _package_version():
    try:
        return dist_version("trk")
    except PackageNotFoundError:
        return None


def _commit(tree):
    """HEAD of the checkout `tree` sits in; None when git is missing or `tree` is not in a checkout."""
    try:
        done = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and len(sha) == 40 else None
