"""Every `az` call the tracker makes: an Azure DevOps pull request's target branch and its source head.

The executable is `az` (with the azure-devops extension), or the one `$TRK_AZ` names (tests point it at a stub; the
suite's default names none).
"""

import json
import os
import re
import subprocess

# https://dev.azure.com/<org>/<project>/_git/<repo>/pullrequest/<id>, or the older https://<org>.visualstudio.com form
PR_URL = re.compile(r"^https://(?:[^@/]+@)?(?:dev\.azure\.com/(?P<org>[^/]+)|(?P<host>[^./]+)\.visualstudio\.com)"
                    r"(?:/DefaultCollection)?/[^/]+/_git/[^/]+/pullrequest/(?P<id>\d+)(?:[/?#].*)?$", re.I)
NUMBER = re.compile(r"^#?(?P<id>\d+)$")
# An origin on Azure DevOps: https://[<user>@]dev.azure.com/<org>/..., git@ssh.dev.azure.com:v3/<org>/...,
# https://<org>.visualstudio.com/..., or <org>@vs-ssh.visualstudio.com:v3/<org>/...
REMOTES = (re.compile(r"^https://(?:[^@/]+@)?dev\.azure\.com/(?P<org>[^/]+)/", re.I),
           re.compile(r"^(?:ssh://)?[^@]+@ssh\.dev\.azure\.com[:/]v3/(?P<org>[^/]+)/", re.I),
           re.compile(r"^https://(?:[^@/]+@)?(?P<host>[^./]+)\.visualstudio\.com/", re.I),
           re.compile(r"^(?:ssh://)?[^@]+@vs-ssh\.visualstudio\.com[:/]v3/(?P<org>[^/]+)/", re.I))


class Unreadable(Exception):
    """`az` is installed but could not tell us about the pull request; the message is why."""


def _org(found):
    return f"https://dev.azure.com/{found['org']}" if found["org"] else f"https://{found['host']}.visualstudio.com"


def origin_org(repo):
    """The organization URL (`https://dev.azure.com/<org>` or `https://<org>.visualstudio.com`) of the checkout's
    `origin` when it is on Azure DevOps, else None."""
    try:
        done = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True, text=True,
                              check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    url = done.stdout.strip() if done.returncode == 0 else ""
    for pattern in REMOTES:
        found = pattern.match(url)
        if found:
            groups = found.groupdict()
            return _org({"org": groups.get("org"), "host": groups.get("host")})
    return None


def pr_target(repo, ref):
    """(organization URL, pull request id) when the `pr` row's ref names an Azure DevOps pull request: its web URL, or
    a bare number or `#number` in a checkout whose origin is on Azure DevOps. None otherwise (a GitHub PR, a branch,
    `none`)."""
    if ref is None:
        return None
    ref = ref.strip()
    found = PR_URL.match(ref)
    if found:
        return _org(found.groupdict()), found["id"]
    found = NUMBER.match(ref)
    if found and repo is not None:
        org = origin_org(repo)
        if org is not None:
            return org, found["id"]
    return None


def is_pr_ref(repo, ref):
    """Whether the ref names an Azure DevOps pull request (`pr_target`)."""
    return pr_target(repo, ref) is not None


def _az(repo, *argv):
    """`az <argv>` run in `repo`: its stdout. Raises FileNotFoundError when az is not installed, `Unreadable` when it
    fails."""
    try:
        done = subprocess.run([os.environ.get("TRK_AZ", "az"), *argv], cwd=repo, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False, timeout=120)
    except subprocess.TimeoutExpired:
        raise Unreadable(f"az {argv[0]} timed out after 120 s") from None
    if done.returncode != 0:
        raise Unreadable(done.stderr.strip() or f"az {argv[0]} exited {done.returncode}")
    return done.stdout


def view(repo, ref):
    """{"base": the branch the pull request targets, "head": its source commit, "files": None} for the Azure DevOps pull
    request `ref`, asked of `az repos pr show`. Azure's CLI gives no file list, so `files` is None and the PR's own files
    are not compared. None when `az` is not installed; raises `Unreadable` when it is but fails, or prints something
    unexpected."""
    org, number = pr_target(repo, ref)
    try:
        found = json.loads(_az(repo, "repos", "pr", "show", "--id", number, "--org", org, "--output", "json"))
        target = found["targetRefName"]
        head = found["lastMergeSourceCommit"]["commitId"]
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        raise Unreadable("az printed no targetRefName or lastMergeSourceCommit") from None
    if not isinstance(target, str) or not target.startswith("refs/heads/") or not head:
        raise Unreadable(f"az printed a target {target!r} that is not a branch")
    return {"base": target[len("refs/heads/"):], "head": head, "files": None}
