"""Every `gh` call the tracker makes: a pull request's base branch and its own changed files, and a workflow run's
conclusion, head sha and jobs.

The executable is `gh`, or the one `$TRK_GH` names (tests point it at a stub; the suite's default names none).
"""

import json
import os
import re
import subprocess
import urllib.parse

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


def compare_status(repo, ref, base, sha):
    """How GitHub places commit `sha` against the branch `base` of the pull request `ref`'s repository
    (`gh api repos/<owner>/<repo>/compare/<base>...<sha>`, its `status`): `behind` or `identical` when `sha` is on the
    branch, `ahead` or `diverged` when it is not. None when `gh` is not installed; raises `Unreadable` when it is but
    fails (a sha GitHub has never been pushed is a 404), or prints nothing."""
    url = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/", ref.strip())
    slug = url[1] if url else "{owner}/{repo}"
    try:
        status = _gh(repo, "api", f"repos/{slug}/compare/{base}...{sha}", "--jq", ".status").strip()
    except FileNotFoundError:
        return None
    if not status:
        raise Unreadable("gh printed no compare status")
    return status


def run_view(slug, run_id):
    """{"conclusion": the run's, "head_sha": the sha it ran at, "branch": its headBranch, "event": what triggered it,
    "jobs": [{"name", "conclusion"}]} for workflow run
    `run_id` of `slug` (`owner/repo`). None when `gh` is not installed; raises `Unreadable` when it is but fails, or prints
    something unexpected."""
    try:
        found = json.loads(_gh(None, "run", "view", str(run_id), "-R", slug, "--json",
                                 "conclusion,event,headBranch,headSha,jobs"))
        return {"conclusion": found["conclusion"], "head_sha": found["headSha"], "branch": found["headBranch"],
                "event": found["event"],
                "jobs": [{"name": j["name"], "conclusion": j["conclusion"]} for j in found["jobs"]]}
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        raise Unreadable("gh printed no conclusion, event, headBranch, headSha or jobs") from None


def default_branch(slug):
    """The default branch of the GitHub repository `slug` (`owner/repo`), as `gh repo view` reports it. None when `gh` is
    not installed; raises `Unreadable` when it is but fails, or prints something unexpected."""
    try:
        return json.loads(_gh(None, "repo", "view", slug, "--json", "defaultBranchRef"))["defaultBranchRef"]["name"]
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        raise Unreadable("gh printed no defaultBranchRef") from None


def _api(endpoint):
    """The JSON `gh api <endpoint>` prints. Raises `Unreadable` when gh is not installed, fails, or prints no JSON."""
    try:
        return json.loads(_gh(None, "api", endpoint))
    except FileNotFoundError:
        raise Unreadable("gh is not installed") from None
    except ValueError:
        raise Unreadable(f"gh api {endpoint} printed no JSON") from None


def ref_object(slug, kind, name):
    """{"type", "sha"} of the git ref `refs/<kind>/<name>` (`kind` is `tags` or `heads`) of the repository `slug`, or None
    when there is no such ref (a name that merely starts with it does not count). Raises `Unreadable`."""
    try:
        listing = _api(f"repos/{slug}/git/matching-refs/{kind}/{urllib.parse.quote(name, safe='/')}")
        found = [entry["object"] for entry in listing if entry["ref"] == f"refs/{kind}/{name}"]
        return {"type": found[0]["type"], "sha": found[0]["sha"]} if found else None
    except (KeyError, TypeError, IndexError):
        raise Unreadable("gh printed no list of refs") from None


def tag_commit(slug, name):
    """The commit tag `name` of `slug` points at (an annotated tag peeled), or None when there is no such tag. Raises
    `Unreadable`."""
    target = ref_object(slug, "tags", name)
    try:
        for _ in range(5):  # a tag object may point at another
            if target is None or target["type"] != "tag":
                break
            target = _api(f"repos/{slug}/git/tags/{target['sha']}")["object"]
        return target["sha"] if target is not None and target["type"] == "commit" else None
    except (KeyError, TypeError):
        raise Unreadable("gh printed no tag object") from None


def branch_rules(slug, branch):
    """The active rules the repository `slug` applies to `branch`, from its rulesets (repository- and organisation-level):
    [{"type", "parameters", "ruleset_id", "source_type", "source"}]. Raises `Unreadable`."""
    try:
        return [{"type": rule["type"], "parameters": rule.get("parameters") or {}, "ruleset_id": rule["ruleset_id"],
                 "source_type": rule["ruleset_source_type"], "source": rule["ruleset_source"]}
                for rule in _api(f"repos/{slug}/rules/branches/{urllib.parse.quote(branch, safe='')}")]
    except (KeyError, TypeError):
        raise Unreadable("gh printed no list of rules") from None


def ruleset_bypass(source_type, source, ruleset_id):
    """Whether the token gh runs under can bypass the ruleset: its `current_user_can_bypass` (`always`,
    `pull_requests_only` or `never`). `source` is `owner/repo`, or the organisation for an `Organization` ruleset.
    Raises `Unreadable`, including when the answer has no such field."""
    where = f"orgs/{source}" if source_type == "Organization" else f"repos/{source}"
    try:
        return _api(f"{where}/rulesets/{ruleset_id}")["current_user_can_bypass"]
    except (KeyError, TypeError):
        raise Unreadable(f"gh printed no current_user_can_bypass for ruleset {ruleset_id}") from None


def tag_rulesets(slug):
    """[{"include": [...], "exclude": [...], "types": {...}, "bypass": ...}] for each active ruleset of `slug` that targets tags: the
    `ref_name` patterns it covers, the rule types it applies and whether the token can bypass it. Raises `Unreadable`."""
    try:
        found = []
        for summary in _api(f"repos/{slug}/rulesets?includes_parents=true"):
            if summary["target"] != "tag" or summary["enforcement"] != "active":
                continue
            detail = _api(f"repos/{slug}/rulesets/{summary['id']}")
            names = detail["conditions"]["ref_name"]
            found.append({"include": names.get("include", []), "exclude": names.get("exclude", []),
                          "types": {rule["type"] for rule in detail["rules"]},
                          "bypass": detail["current_user_can_bypass"]})
        return found
    except (KeyError, TypeError):
        raise Unreadable("gh printed no ruleset") from None


def branch_protection(slug, branch):
    """The classic protection of `branch` in `slug` as gh prints it (`required_pull_request_reviews`, `restrictions`,
    `enforce_admins`, `block_creations`, …), or None when the branch has none. Raises `Unreadable`."""
    try:
        return _api(f"repos/{slug}/branches/{urllib.parse.quote(branch, safe='')}/protection")
    except Unreadable as e:
        if "not protected" in str(e).lower():
            return None
        raise
