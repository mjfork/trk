"""Bakes the checkout's commit into the wheel, so `trk --version` can name it after a `--no-editable`
install, whose copy carries no git. An editable build gets no stamp: that trk imports from the checkout
and reads git live (`trk.version.provenance`).

The stamp is written to a temp file and force-included, so a build never writes into the working tree.
hx does the same thing the same way (hx 120b102), so the two tools answer `--version` alike.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

STAMP = "trk/_build.py"


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "git-stamp"

    def initialize(self, version, build_data):
        self._tmp = None
        if version == "editable":
            return
        commit, dirty = head(Path(self.root))
        if commit is None:  # a tarball, or no git: `trk --version` says the commit is unknown
            return
        self._tmp = Path(tempfile.mkdtemp(prefix="trk-build-stamp-"))
        stamp = self._tmp / "_build.py"
        stamp.write_text('"""Written at build time by hatch_build.py: the checkout this trk was built from."""\n'
                         f'COMMIT = "{commit}"\nDIRTY = {dirty}\n')
        build_data["force_include"][str(stamp)] = STAMP

    def finalize(self, version, build_data, artifact_path):
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
            self._tmp = None


def head(root):
    """HEAD of the checkout rooted at `root`, and whether it has uncommitted changes. (None, False) when git
    is missing or `root` is not a checkout root: a repo that merely surrounds it is some other project."""
    top = git(root, "rev-parse", "--show-toplevel")
    if top is None or Path(top).resolve() != Path(root).resolve():
        return None, False
    sha = git(root, "rev-parse", "HEAD")
    if sha is None or len(sha) != 40:
        return None, False
    return sha, bool(git(root, "status", "--porcelain"))


def git(root, *args):
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None
