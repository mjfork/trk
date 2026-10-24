"""Every git call the tracker makes: which repo is a ticket's, sha equality and ancestry ("contains").

Shas are compared through `git rev-parse` when the repo is a local checkout, else by prefix. Answers
are cached for the life of one CLI invocation; `main` calls `forget()` before each.
"""

import functools
import os
import re
import subprocess

SHORTEST = 7


def forget():
    """Drop every cached answer: commits may have been made since the last invocation in this process."""
    is_checkout.cache_clear()
    remote_of.cache_clear()
    resolve.cache_clear()
    _is_ancestor.cache_clear()
    landing.cache_clear()


def _git(repo, *argv):
    # Decoded leniently: a file or path that is not UTF-8 shows as U+FFFD and never raises.
    return subprocess.run(["git", "-C", repo, *argv], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False)


@functools.cache
def is_checkout(path):
    """Whether `path` is a directory inside a git work tree."""
    if not os.path.isdir(path):
        return False
    done = _git(path, "rev-parse", "--is-inside-work-tree")
    return done.returncode == 0 and done.stdout.strip() == "true"


GITHUB = re.compile(r"^(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)(?P<path>[^/]+/[^/]+?)(?:\.git)?/?$")


@functools.cache
def remote_of(repo):
    """The https web address of the checkout's `origin` on GitHub (`https://github.com/<owner>/<repo>`), or
    None when it has no origin, or one that is not on GitHub (a PR or commit then has no URL to link)."""
    done = _git(repo, "remote", "get-url", "origin")
    found = GITHUB.match(done.stdout.strip()) if done.returncode == 0 else None
    return f"https://github.com/{found['path']}" if found else None


def repos_of(conn, item_id):
    """Every local checkout among the item's repos (in the order added), then its ancestors', nearest first."""
    from . import items

    found = []
    for link in items.lineage(conn, item_id):
        for repo in items.repos(conn, link["id"]):
            path = os.path.expanduser(repo)
            if path not in found and is_checkout(path):
                found.append(path)
    return found


def place(repos, *shas):
    """The checkout of `repos` that holds every one of `shas`, the first such; else the first checkout (so an
    unknown sha is told as it is with one repo); None when there are none. A ticket whose code lives in its
    second repo places its commits there."""
    for repo in repos:
        if all(resolve(repo, sha) is not None for sha in shas if sha):
            return repo
    return repos[0] if repos else None


def tried(repo, sha):
    """What a `sha-unverifiable` need says about the lookup: the `checkout` tried (absent when the item
    has none) and a `hint` to fetch when the sha is simply unknown there."""
    if repo is None:
        return {"hint": "no local git checkout: add one with `trk edit --add-repo <path>`"}
    found = {"checkout": repo}
    if sha and resolve(repo, sha) is None:
        found["hint"] = f"{sha} is unknown to {repo}: run `git fetch` there, then retry"
    return found


def tried_in(repos, sha):
    """`tried` over every checkout the lookup searched (`place`): the `checkout` that holds the sha, else the
    checkout tried (a string) or, with several, all of them in order (a list), and a `hint` to fetch."""
    if not repos:
        return tried(None, sha)
    placed = place(repos, sha)
    if len(repos) == 1 or not sha or resolve(placed, sha) is not None:
        return tried(placed, sha)
    return {"checkout": list(repos), "hint": f"{sha} is unknown to {', '.join(repos)}: run `git fetch` in the "
                                             "checkout that should hold it, then retry"}


@functools.cache
def resolve(repo, sha):
    """The full commit sha `sha` names in `repo`, or None when it names no commit there."""
    done = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    return done.stdout.strip() if done.returncode == 0 else None


def _prefix_equal(a, b):
    short, long = sorted((a, b), key=len)
    return len(short) >= SHORTEST and long.startswith(short)


def same(repo, a, b):
    """Whether two shas name one commit: True, False, or None when the checkout cannot resolve one of them.
    Without a checkout, one must be a prefix of the other, the shorter at least 7 chars."""
    if a == b:
        return True
    if repo is None:
        return _prefix_equal(a, b)
    full_a, full_b = resolve(repo, a), resolve(repo, b)
    if full_a is None or full_b is None:
        return None
    return full_a == full_b


@functools.cache
def _is_ancestor(repo, ancestor, sha):
    done = _git(repo, "merge-base", "--is-ancestor", ancestor, sha)
    return {0: True, 1: False}.get(done.returncode)


def contains(repo, ancestor, sha):
    """Whether commit `sha` contains `ancestor` (git ancestry): True, False, or None when it cannot be
    told (no checkout, or an unknown commit). Equal shas contain each other without git."""
    if (_prefix_equal(ancestor, sha) if repo is None else ancestor == sha):
        return True
    if repo is None:
        return None
    return _is_ancestor(repo, ancestor, sha)


def _patch_ids(repo, *log_argv):
    """`git patch-id --stable` over the patches `git <log_argv>` prints: {patch id: first commit sha}, {} when
    there are no patches, None when git failed. The patches are piped as bytes, never decoded (a binary file or
    non-UTF-8 text in the range is common); only patch-id's ASCII output is."""
    try:
        with subprocess.Popen(["git", "-C", repo, *log_argv], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as shown:
            with subprocess.Popen(["git", "-C", repo, "patch-id", "--stable"], stdin=shown.stdout,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as ids:
                shown.stdout.close()  # patch-id holds the only read end now; shown sees EPIPE if it quits early
                out, _ = ids.communicate()
            shown.wait()
    except OSError:
        return None
    if shown.returncode != 0 or ids.returncode != 0:
        return None
    found = {}
    for entry in out.decode("ascii", "replace").splitlines():
        patch, _, commit = entry.partition(" ")
        found.setdefault(patch, commit)
    return found


def same_patch_in(repo, fix, sha):
    """Whether `sha` holds a commit with the same patch as `fix` (same `git patch-id`), as a rebase leaves
    it: True or False, or None when git failed on the patches (the caller reports `sha-unverifiable`). Looks at
    the commits between the merge-base of the two and `sha`; a fix with no patch (an empty commit) or no common
    history has none."""
    base = _git(repo, "merge-base", fix, sha)
    if base.returncode != 0:
        return False
    wanted = _patch_ids(repo, "show", "--format=", fix)
    if wanted is None:
        return None
    if not wanted:
        return False
    found = _patch_ids(repo, "log", "-p", "--format=%H", "--no-merges", f"{base.stdout.strip()}..{sha}")
    return None if found is None else bool(wanted.keys() & found.keys())


@functools.cache
def landing(repo, head, merge):
    """How commit `merge` landed the pull request whose head is `head`: (kind, pre), where `pre...merge` holds
    exactly the PR's changes (`pre` None for a fast-forward, whose base only the caller knows), False when `merge`
    did not land it, None when git cannot tell (no checkout, an unknown commit). Containing the head is not
    landing it: every later commit does. The kinds:

    - `fast-forward`: `merge` is the head.
    - `merge`: a merge commit with the head as a parent and another parent that lacks it (`pre` is the first such
      parent, so a main merged into the branch and then fast-forwarded reads against main's side). A merge whose
      parent only holds the head further back, such as a later merge of a side branch after a revert, is not one.
    - `squash`: one parent, without the head, and the patch of `parent..merge` is the PR's net patch.
    - `rebase`: one parent, without the head, and the last n commits (n the PR's commit count) carry each of the
      PR's patches, as a rebase leaves them (`pre` is `merge~n`).

    A squash or rebase whose patches changed (conflict resolutions) matches none, and is False."""
    if repo is None:
        return None
    same_commit = same(repo, head, merge)
    if same_commit is None:
        return None
    if same_commit:
        return ("fast-forward", None)
    parent = parents(repo, merge)
    if parent is None:
        return None
    if len(parent) >= 2:
        heads = [same(repo, head, p) for p in parent]
        if None in heads:
            return None
        if not any(heads):  # a merge that merely has the head somewhere behind a parent (a later merge of a side branch)
            return False
        lacking = [p for p, is_head in zip(parent, heads) if not is_head and contains(repo, head, p) is False]
        return ("merge", lacking[0]) if lacking else False
    if not parent:
        return False
    held = contains(repo, head, merge)
    if held is not False:
        return None if held is None else False  # a later commit on top of the head, not what landed it
    fork = _git(repo, "merge-base", head, merge)
    if fork.returncode != 0:
        return False
    fork = fork.stdout.strip()
    net = _patch_ids(repo, "diff", fork, head)
    squashed = _patch_ids(repo, "diff", parent[0], merge)
    if net is None or squashed is None:
        return None
    if net and net.keys() & squashed.keys():
        return ("squash", parent[0])
    commits = _git(repo, "rev-list", "--no-merges", f"{fork}..{head}")
    count = len(commits.stdout.split()) if commits.returncode == 0 else 0
    wanted = _patch_ids(repo, "log", "-p", "--format=%H", "--no-merges", f"{fork}..{head}")
    found = _patch_ids(repo, "log", "-p", "--format=%H", "--no-merges", "-n", str(count), merge) if count else {}
    if wanted is None or found is None:
        return None
    if not wanted or not wanted.keys() <= found.keys():
        return False
    pre = _git(repo, "rev-parse", "--verify", "--quiet", f"{merge}~{count}")
    return ("rebase", pre.stdout.strip()) if pre.returncode == 0 else False


def late_rating(repo, head, merge, sha, base):
    """Whether a rating at `sha` against `base` holds the whole pull request (head `head`, landed by `merge`) when
    `sha` is neither: `sha` contains `merge`, `merge` landed the PR (`landing`), and `base` holds neither the
    merge nor the head, whose diff would then leave the PR out. True, False, or None when git cannot tell."""
    landed = landing(repo, head, merge)
    answers = [contains(repo, merge, sha), None if landed is None else landed is not False]
    answers += [None if held is None else not held for held in (contains(repo, merge, base), contains(repo, head, base))]
    return False if False in answers else None if None in answers else True


def landing_paths(repo, base, head, merge, landed):
    """(the paths `merge` landed, the paths of `base...head`) for the pull request with head `head` that `merge` landed
    (`landed` is `landing`'s answer): `pre...merge`, `pre` the commit before the PR on the base side (`base` for a
    fast-forward). The second list is for a PR whose first commits reached the base branch on their own (main
    fast-forwarded to part of the branch), where `pre` is already past them, and for a merge that has the head as a
    parent but brought no tree (`git merge -s ours`). Both together are the PR's own paths, and the first alone is what a
    review of the merge saw. None when git cannot tell."""
    kind, pre = landed
    via_merge = changed_files(repo, pre or base, merge)
    via_head = changed_files(repo, base, head)
    return None if via_merge is None or via_head is None else (via_merge, via_head)


def _names(repo, *argv):
    """The NUL-terminated names `git <argv> -z` prints, as the raw bytes git stored, or None when git failed.
    `-z` keeps git from quoting and octal-escaping a non-ASCII name, as `core.quotepath` does by default."""
    done = subprocess.run(["git", "-C", repo, argv[0], "-z", *argv[1:]], capture_output=True, check=False)
    return [name for name in done.stdout.split(b"\0") if name] if done.returncode == 0 else None


def display(name):
    """A path for ratings, display and storage: UTF-8 with U+FFFD for bytes that are not (a name read by
    `list_dir` keeps its raw bytes as surrogate escapes, for handing back to git)."""
    return os.fsencode(name).decode("utf-8", "replace")


def changed_files(repo, base, sha, renames=False):
    """The repo-relative paths `git diff --name-only <base>...<sha>` lists, sorted, in `display` form (a
    non-UTF-8 name shows U+FFFD; a non-ASCII name is never quoted); None when git cannot tell (an unknown
    commit). A rename lists both its names, unless `renames`, which lists only the new one (as GitHub does)."""
    names = _names(repo, "diff", "--name-only", "-M" if renames else "--no-renames", f"{base}...{sha}")
    return sorted(name.decode("utf-8", "replace") for name in names) if names is not None else None


def merge_base(repo, a, b):
    """The full sha of the best common ancestor of commits `a` and `b`; None when they have none or git cannot tell."""
    done = _git(repo, "merge-base", a, b)
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def hides_pr(repo, base, sha, head, branch_from=None):
    """Why a rating of `sha` against `base` leaves out changes the pull request made, else False: the diff `base...sha`
    starts at `merge_base(base, sha)`, which is hiding when it is `sha` ("at-sha"), or contains the PR's `head`
    ("holds-head"), or (given `branch_from`, a commit of the branch the PR merges into as it stood before the PR) is
    not an ancestor of the merge-base of `branch_from` and `sha`, the point the PR branched ("after-branch-point").
    Returns the reason and, for the last, the branch point: `(reason, point)`. A branch point that is `sha` itself (the
    PR is already in `branch_from`) says nothing of where the PR began, so only the first two hold. None when git cannot
    tell."""
    start = merge_base(repo, base, sha)
    if start is None:
        return None
    if same(repo, start, sha) is not False:
        return "at-sha", sha
    if head is not None:
        held = contains(repo, head, start)
        if held is None:
            return None
        if held:
            return "holds-head", head
    if branch_from is None:
        return False
    point = merge_base(repo, branch_from, sha)
    if point is None:
        return None
    if same(repo, point, sha) is not False:
        return False
    early = contains(repo, start, point)
    if early is None:
        return None
    return False if early else ("after-branch-point", point)


def parents(repo, sha):
    """The full shas of commit `sha`'s parents, first parent first; None when git cannot tell."""
    done = _git(repo, "rev-list", "--parents", "-n", "1", sha)
    return done.stdout.split()[1:] if done.returncode == 0 else None


def default_base(repo):
    """The first of `origin/HEAD`, `main` and `master` that names a commit in `repo`, else None."""
    for ref in ("origin/HEAD", "main", "master"):
        if resolve(repo, ref) is not None:
            return ref
    return None


def merge_parent(repo, head, merge):
    """Where the branch the pull request with head `head` merged into stood before commit `merge` landed it (`landing`):
    `pre`, the parent of a merge commit that lacks the head, the parent of a squash, or the commit before a rebase's
    commits. None for a fast-forward (`merge` is the head, whose parent is the PR's own), for a merge row that did not
    land this PR (an earlier head of the ticket, a later commit that merely holds the head), or when git cannot tell."""
    landed = landing(repo, head, merge)
    return landed[1] if landed and landed[0] != "fast-forward" else None


def landing_parent(repo, head, ref):
    """Where `ref` stood before the pull request with head `head` landed in it by a merge commit: the first parent of the
    first commit on `ref`'s own first-parent line that holds `head`, when that commit is a merge and its first parent
    does not hold `head`. None for a fast-forward (the first such commit's parent is `head`), a squash or rebase merge
    (no commit holds `head`), or when git cannot tell."""
    done = _git(repo, "rev-list", "--first-parent", "--ancestry-path", f"{head}..{ref}")
    landed = done.stdout.split()[-1:] if done.returncode == 0 else []
    parent = parents(repo, landed[0]) if landed else None
    if not parent or len(parent) < 2 or contains(repo, head, parent[0]) is not False:
        return None
    return parent[0]


def remote_default_base(repo):
    """What a rating is checked against when nothing fresher is known: `origin/HEAD`, `origin/main` or `origin/master`,
    the first that names a commit, for a checkout with an origin (a branch made here is no stand-in for the remote's
    own); for one without, `default_base`. None when there is none."""
    if _git(repo, "remote", "get-url", "origin").returncode != 0:
        return default_base(repo)
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        if resolve(repo, ref) is not None:
            return ref
    return None


def fetch_tip(repo, branch, remote="origin"):
    """The full sha `branch` is at on `remote` right now: fetched into `refs/remotes/<remote>/<branch>` and read back,
    never the checkout's earlier copy. None when the fetch fails (no such remote or branch, no network). Not cached."""
    tracking = f"refs/remotes/{remote}/{branch}"
    if _git(repo, "fetch", "--quiet", remote, f"+refs/heads/{branch}:{tracking}").returncode != 0:
        return None
    done = _git(repo, "rev-parse", "--verify", "--quiet", f"{tracking}^{{commit}}")
    return done.stdout.strip() if done.returncode == 0 else None


def read_file(repo, sha, path):
    """The text of `path` at commit `sha`, or None when that commit has no such file."""
    done = _git(repo, "show", f"{sha}:{path}")
    return done.stdout if done.returncode == 0 else None


def list_dir(repo, sha, directory):
    """The paths of the files directly in `directory` at commit `sha`, sorted. Each keeps its raw bytes as
    surrogate escapes (`os.fsdecode`), so it can go back to git in `read_file`; use `display` to show or store it."""
    names = _names(repo, "ls-tree", "--name-only", sha, f"{directory}/")
    return sorted(os.fsdecode(name) for name in names) if names is not None else []


def branch_of(path):
    """The branch the work tree at `path` has checked out, or None when HEAD is detached or it is no checkout."""
    done = _git(path, "symbolic-ref", "--short", "--quiet", "HEAD")
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def repo_of(path):
    """The main checkout the work tree at `path` belongs to (itself for a main checkout, the checkout that
    added it for a linked worktree), with symlinks resolved; None when it is no checkout."""
    done = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if done.returncode != 0:
        return None
    common = os.path.realpath(done.stdout.strip())
    return os.path.dirname(common) if os.path.basename(common) == ".git" else common


def toplevel(path):
    """The root of the git work tree that holds `path` (which need not exist: its nearest existing directory
    is asked), with symlinks resolved; None when it is in none."""
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    done = _git(path, "rev-parse", "--show-toplevel")
    return os.path.realpath(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None
