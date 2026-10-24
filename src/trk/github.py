"""Every `gh` call the tracker makes: a pull request's base branch and its own changed files.

The executable is `gh`, or the one `$TRK_GH` names (tests point it at a stub; the suite's default names none).
"""

import json
import os
import re
import subprocess

PR_REF = re.compile(r"^(?:#?(?P<number>\d+)|https://github\.com/[^/]+/[^/]+/pull/(?P<url>\d+)(?:[/?#].*)?)$")


class Unreadable(Exception):
    """`gh` is installed but could not tell us about the pull request; the message is why."""


def is_pr_ref(ref):
    """Whether a `pr` row's ref names a GitHub pull request (a number, `#number` or a pull request URL), as
    against a branch name, `none` or a review link that `gh pr view` could not be trusted to resolve."""
    return ref is not None and PR_REF.match(ref.strip()) is not None


def _gh(repo, *argv):
    """`gh <argv>` run in `repo`: its stdout. Raises FileNotFoundError when gh is not installed, `Unreadable` when it
    fails."""
    try:
        done = subprocess.run([os.environ.get("TRK_GH", "gh"), *argv], cwd=repo, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False, timeout=120)
    except subprocess.TimeoutExpired:
        raise Unreadable(f"gh {argv[0]} timed out after 120 s") from None
    if done.returncode != 0:
        raise Unreadable(done.stderr.strip() or f"gh {argv[0]} exited {done.returncode}")
    return done.stdout


def view(repo, ref):
    """{"base": the PR's base branch, "head": its head sha, "files": every path in its own diff (renames by their new name)} for the pull
    request `ref`, asked of `gh` in `repo`. None when `gh` is not installed; raises `Unreadable` when it is but
    fails, or prints something unexpected. The files come from the REST list with `--paginate`: `gh pr view
    --json files` stops at 100 files, which would leave a large PR's check blind past them."""
    found = PR_REF.match(ref.strip())
    number = found["number"] or found["url"]
    url = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/", ref.strip())
    slug = url[1] if url else "{owner}/{repo}"
    try:
        found = json.loads(_gh(repo, "pr", "view", ref.strip().lstrip("#"), "--json", "baseRefName,headRefOid"))
        base, head = found["baseRefName"], found["headRefOid"]
        listing = _gh(repo, "api", "--paginate", f"repos/{slug}/pulls/{number}/files", "--jq", ".[].filename | @json")
        return {"base": base, "head": head, "files": [json.loads(line) for line in listing.splitlines() if line.strip()]}
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        raise Unreadable("gh printed no baseRefName, headRefOid or file list") from None
