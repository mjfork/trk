"""A program's close checks its run evidence against the run itself (tr-43mme, ledger-5).

A `test-run` or `webtester-run` that decides a program criterion at close must name a GitHub Actions run, and that
run must have concluded `success`, have run the release-verification job to `success` (a skipped job is not a
success), and have been run at the recorded release sha. The check is made when the program closes, as
`blast-radius` checks a pull request when it rates: reading a run costs a `gh` call, which a gate that `show`
evaluates should not make. A run `gh` cannot read is rule `sha-unverifiable`.

The run must also belong to the program and be the repository's own: its `owner/repo` must be the GitHub `origin` of
one of the program's repos (ledger-8), and it must have run on the repository's default branch, a release branch
(`RELEASE_BRANCH`) or a tag, triggered by a push, a manual dispatch or a schedule, so a fork's pull request or a feature
branch that defines its own job proves nothing (ledger-15). The job it must have passed is the program's own, else the store's (ledger-9), and only a person may name
it: an agent cannot make a close pass by naming a job it controls (ledger-14). A notes line that mentions the setting
but does not read cleanly is a visible need, never a fallback to the store's job (ledger-13). `show` makes no `gh` call, so while a
program has deciding runs its close gate lists the `release-run-check` need: the run check is still to come at close.
"""

import fnmatch
import json
import re

from . import actors, criteria, evidence, github, gitrepo, sections, store
from .errors import gate_unmet, refused

SETTING = "release_verify_job"  # the `release-verify-job` setting's meta key
ATTESTED = "release_job_by_person:"  # meta key prefix: a program's notes job line, as a person last wrote it
PENDING = "release-run-check"  # the close gate's need while a deciding run is yet to be read at close
EVENTS = ("push", "workflow_dispatch", "schedule")  # what may trigger a run that proves a release
RELEASE_BRANCH = "release/*"  # a fixed shape, not a setting: `release/` and one segment (no `/` after the prefix)

# A program names its own release-verification job on a line of its `notes` section: `release-verify-job: <job name>`,
# with an optional list bullet, backticks around the line or the value, and any case. Any other line that mentions the
# setting is not read: it is a visible `unparsed` need, never a guess.
_MENTION = re.compile(r"release[-_]verify[-_]job", re.IGNORECASE)
_PLAIN = re.compile(r"^(?:[-*+][ \t]+)?`?release-verify-job`?[ \t]*:[ \t]*(?P<value>.*)$", re.IGNORECASE)
_WHOLE = re.compile(r"^(?:[-*+][ \t]+)?`release-verify-job[ \t]*:[ \t]*(?P<value>[^`]*)`$", re.IGNORECASE)
_JOB_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 _./()-]*$")  # a conservative GitHub job name

_RUN_URL = re.compile(r"^https://github\.com/(?P<slug>[^/\s]+/[^/\s]+)/actions/runs/(?P<id>\d+)(?:[/?#].*)?$")


def person_wrote(conn, actor, session):
    """Whether a recorded write was a person's: a human actor with no session. Reads only the record, never the
    environment of whoever asks, so a person's job counts whoever closes the program."""
    return session is None and actors.kind(conn, actor) == "human"


def is_person(conn, actor, session):
    """Whether the write being made now is a person's: a recorded person, and this process outside any agent session
    (neither `$CLAUDECODE` nor `$TRK_SESSION` set, as `actor-required` judges one). Judged when the write is made."""
    return person_wrote(conn, actor, session) and not store.in_agent_session()


def notes_job(body):
    """What a notes body says about the job: None when no line mentions `release-verify-job`; else a pair
    (`ok`, the name) when every such line reads as `release-verify-job: <name>` for one plain name, (`empty`, None) when
    the value is empty, and (`unparsed`, the first line that does not read cleanly) otherwise: a bold, numbered,
    quoted or table line, a value with a `#` or other punctuation, or two lines that name different jobs."""
    lines = [line.strip() for line in (body or "").splitlines() if _MENTION.search(line)]
    if not lines:
        return None
    names = []
    for line in lines:
        found = _WHOLE.match(line) or _PLAIN.match(line)
        if found is None:
            return "unparsed", line
        value = found["value"].strip()
        if len(value) > 1 and value.startswith("`") and value.endswith("`"):
            value = value[1:-1].strip()
        if value and not _JOB_NAME.match(value):
            return "unparsed", line
        names.append(value)
    if len(set(names)) > 1:
        return "unparsed", lines[0]
    return ("ok", names[0]) if names[0] else ("empty", None)


def attest(tx, item_id, handle, old, new):
    """Called after a section write (`old` and `new` are the bodies; None for absent): a program's `notes` write that sets
    the job line to a new value is the write that attests it. A person's records the name; an agent's forgets any
    record, so what it planted or changed never matches. A write that leaves the job as it was, a person's included,
    attests nothing and forgets nothing: it cannot vouch for a line an agent planted earlier."""
    if handle != "notes":
        return
    key = ATTESTED + item_id
    was, now = notes_job(old), notes_job(new)
    if now is not None and now[0] == "ok":
        if was == now:
            return
        if is_person(tx.conn, tx.actor, tx.session):
            tx.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET"
                            " value = excluded.value", (key, now[1]))
            return
    tx.conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def _meta(conn, key):
    found = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return found["value"] if found else None


def _setting_by_person(conn):
    """Whether the latest change to the store-wide setting was a person's."""
    for row in conn.execute("SELECT actor, session, data FROM journal WHERE change = 'config.changed' ORDER BY seq DESC"):
        if json.loads(row["data"]).get("key") == "release-verify-job":
            return person_wrote(conn, row["actor"], row["session"])
    return False


def job_name(conn, item_id):
    """(job, None) for the release-verification job a program's runs must have passed, or (None, (reason, detail)):
    `unset` (no one named a job), `empty` (the notes line has no value), `unparsed` (a notes line mentions the setting
    but does not read cleanly) or `not-by-person` (an agent wrote the name).
    The program's notes line wins over the store-wide setting; neither falls back silently when it is wrong."""
    named = notes_job(sections.body(conn, item_id, "notes"))
    if named is not None:
        state, name = named
        if state == "unparsed":
            return None, ("unparsed", f"the program's notes mention release-verify-job on a line that does not read as"
                          f" `release-verify-job: <job name>` (a plain name): {name!r}")
        if state == "empty":
            return None, ("empty", "the `release-verify-job:` line of the program's notes names no job")
        if _meta(conn, ATTESTED + item_id) != name:
            return None, ("not-by-person", f"the notes name job {name!r}, but a person did not set that name: the"
                          " write that sets or changes the value must be a person's")
        return name, None
    setting = _meta(conn, SETTING)
    if setting is None:
        return None, ("unset", "no job is named in the program's notes or the store's settings")
    if not _setting_by_person(conn):
        return None, ("not-by-person", f"the store-wide job {setting!r} was not set by a person: a person must run"
                      " `trk config set release-verify-job`")
    return setting, None


def repo_slugs(conn, item_id):
    """The lowercase `owner/repo` of the GitHub `origin` of each of the program's local checkouts (its own repos, then
    its ancestors'); empty when it has no checkout or none has a GitHub origin."""
    found = []
    for path in gitrepo.repos_of(conn, item_id):
        remote = gitrepo.remote_of(path)
        if remote and (slug := remote.removeprefix("https://github.com/").lower()) not in found:
            found.append(slug)
    return found


def deciding_runs(conn, row, weighing=None):
    """[(criterion handle, the counting run row that decides it)] for the live criteria of a program whose latest
    counting `close` row is a passing `test-run` or `webtester-run`; `human-accept` rows need no run."""
    weighing = weighing or evidence.weigh(conn, row)
    found = []
    for criterion in criteria.live(conn, row["id"]):
        latest = weighing.latest(evidence.RUNS, "close", criterion["n"])
        if latest is not None and latest["verdict"] == "pass" and latest["kind"] in evidence.RELEASE_RUNS:
            found.append((criteria.handle(criterion["n"]), latest))
    return found


def pending(conn, row, weighing):
    """The close gate's `release-run-check` need while a program has deciding runs (else none): `show` does not read
    them against GitHub, so it cannot say the close is met; `trk close` does, and replaces this need with its verdict."""
    if row["kind"] != "program" or not deciding_runs(conn, row, weighing):
        return []
    return [{"need": PENDING, "hint": "each deciding run is read against GitHub when the program closes:"
             " try `trk close`"}]


def check(conn, row):
    """Verify each deciding run of a program being closed: returns the list of definite failures (each a `need`
    `release-run` with its criterion, evidence id and reason), and raises `sha-unverifiable` when a run could not be
    read at all and nothing is definitely wrong."""
    runs = deciding_runs(conn, row)
    if not runs:
        return []
    job, why = job_name(conn, row["id"])
    if job is None:
        reason, detail = why
        return [{"need": "release-verify-job", "reason": reason, "detail": detail,
                 "hint": "a person names the job a release run must have passed: a `release-verify-job: <job name>`"
                 " line in the program's notes, or for every program `trk config set release-verify-job <job name>`"}]
    slugs = repo_slugs(conn, row["id"])
    defaults = {}  # slug -> its default branch, asked of gh once
    failures, unreadable = [], []
    for handle, run in runs:
        reason, detail = _judge(run, job, slugs, defaults)
        if reason == "sha-unverifiable":
            unreadable.append(f"{handle}: {detail}")
        elif reason is not None:
            failures.append({"need": "release-run", "criterion": handle, "evidence": run["id"], "reason": reason,
                             "detail": detail})
    if failures or not unreadable:
        return failures
    raise refused("sha-unverifiable", "cannot check the release runs: " + "; ".join(unreadable), item=row["id"])


def _default_branch(slug, defaults):
    """The default branch of `slug` as gh says (the local `origin/HEAD` is not trusted: a writer can repoint it);
    raises `github.Unreadable` when it cannot be told."""
    if slug not in defaults:
        name = github.default_branch(slug)
        if name is None:
            raise github.Unreadable("gh is not installed")
        defaults[slug] = name
    return defaults[slug]


def _is_release_branch(name):
    return fnmatch.fnmatchcase(name, RELEASE_BRANCH) and name.count("/") == 1 and name != "release/"


def _ref_kind(slug, name, head_sha, defaults):
    """`release` for a branch of the `RELEASE_BRANCH` shape, `default` for the default branch, `tag` for a tag of the
    repository that points at the run's own commit and has no branch of its name, else None (with the reason as a second
    value). The default branch, tags and branches are asked of gh; raises `github.Unreadable` when it cannot say."""
    if _is_release_branch(name):
        return "release", None
    if name == _default_branch(slug, defaults):
        return "default", None
    target = github.tag_commit(slug, name)
    if target is None:
        return None, "which is not the default branch, a release branch or a tag"
    if target.lower() != head_sha.lower():
        return None, f"a tag that points at {target[:12]}, not at the run's commit"
    if github.ref_object(slug, "heads", name) is not None:
        return None, "a name that is both a tag and a branch"
    return "tag", None


# What a ref's rules must restrict for a run on it to prove a release, counting only protection the token trk runs under
# cannot bypass. The default branch exists: a strong `pull_request` rule or an `update` rule suffices, or classic
# protection with strong reviews nobody can bypass (`_classic_counts`). A release branch or a tag may not exist yet, and an
# `update` or `pull_request` rule covers changes to an existing ref, not creating one, so it also needs a `creation`
# rule; classic protection never counts for a release branch or a tag. A `pull_request` rule is strong with one approval
# and stale approvals dismissed or the last push approved. A rule that only blocks `deletion` or `non_fast_forward`
# restricts no writer.


def _glob(pattern):
    """A ruleset `ref_name` pattern as a regex: `*` stays within a path segment, `**` crosses them."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        else:
            out = out + {"*": "[^/]*", "?": "[^/]"}.get(pattern[i], re.escape(pattern[i]))
            i += 1
    return re.compile(out + r"\Z")


def _covers(patterns, ref):
    return any(p == "~ALL" or (p != "~DEFAULT_BRANCH" and _glob(p).match(ref)) for p in patterns)


def _strong_reviews(params, dismiss_key):
    return (params.get("required_approving_review_count") or 0) >= 1 and bool(
        params.get(dismiss_key) or params.get("require_last_push_approval"))


def _branch_rule_types(slug, name, cache):
    """The types of the rules in effect for a branch that count: each from a ruleset the token cannot bypass
    (`current_user_can_bypass` is `never`), a `pull_request` rule only when strong."""
    types = set()
    for rule in github.branch_rules(slug, name):
        key = ("bypass", rule["source_type"], rule["source"], rule["ruleset_id"])
        if key not in cache:
            cache[key] = github.ruleset_bypass(*key[1:])
        if cache[key] == "never" and (rule["type"] != "pull_request" or
                                      _strong_reviews(rule["parameters"], "dismiss_stale_reviews_on_push")):
            types.add(rule["type"])
    return types


def _classic_counts(found):
    """Whether classic protection stops the token trk runs under, and so an agent pushing with it, from writing the
    default branch: `enforce_admins` on (an admin token is exempt otherwise) and strong required reviews (at least one
    approval, stale ones dismissed or the last push approved) that nobody may bypass (`bypass_pull_request_allowances`
    names no user, team or app). Push restrictions and `block_creations` never count: their allow-lists can name the
    token's own user."""
    if not found or not (found.get("enforce_admins") or {}).get("enabled"):
        return False
    reviews = found.get("required_pull_request_reviews") or {}
    allowed = reviews.get("bypass_pull_request_allowances") or {}
    return _strong_reviews(reviews, "dismiss_stale_reviews") and not any(allowed.get(k) for k in ("users", "teams", "apps"))


def _ref_protected(slug, where, name, cache):
    """Whether GitHub stops an untrusted pusher, and the token trk runs under, from writing the ref a run proves a release
    from: active rules (rulesets the token cannot bypass) or classic protection with `enforce_admins`. Read from GitHub,
    which an agent cannot change. Raises `github.Unreadable` when it cannot be read."""
    key = (slug, where, name)
    if key not in cache:
        if where == "tag":
            ref = f"refs/tags/{name}"
            types = set()
            for ruleset in github.tag_rulesets(slug):
                if (ruleset["bypass"] == "never" and _covers(ruleset["include"], ref)
                        and not _covers(ruleset["exclude"], ref)):
                    types |= ruleset["types"]
            cache[key] = "creation" in types and "update" in types
        else:
            types = _branch_rule_types(slug, name, cache)
            writers = "pull_request" in types or "update" in types
            if where == "default" and writers:
                cache[key] = True
            elif where == "default":
                cache[key] = _classic_counts(github.branch_protection(slug, name))
            else:  # a release branch counts only through rulesets: classic protection cannot say who may create one
                cache[key] = writers and "creation" in types
    return cache[key]


def _judge(run, job, slugs, defaults):
    """(reason, detail) for one run row: reason None when the run proves the release, else `run-wrong-repo`,
    `run-wrong-branch`, `ref-unprotected`, `run-not-success`, `job-not-success`, `run-wrong-sha` or `sha-unverifiable`. `slugs` are the
    program's repos."""
    found = _RUN_URL.match((run["ref"] or "").strip())
    if found is None:
        return "sha-unverifiable", f"{run['id']}'s ref {run['ref']!r} is not a GitHub Actions run URL"
    if not slugs:
        return "sha-unverifiable", ("the program has no local checkout with a GitHub origin, so the repository of run"
                                    f" {found['id']} cannot be checked: `trk edit --add-repo <path>`")
    if found["slug"].lower() not in slugs:
        return "run-wrong-repo", f"run {found['id']} is of {found['slug']}, not of {', '.join(slugs)}"
    try:
        seen = github.run_view(found["slug"], found["id"])
    except github.Unreadable as e:
        return "sha-unverifiable", f"gh cannot read run {found['id']} of {found['slug']}: {e}"
    if seen is None:
        return "sha-unverifiable", f"gh is not installed, so run {found['id']} of {found['slug']} was not read"
    if seen["event"] not in EVENTS:
        return "run-wrong-branch", (f"run {found['id']} was triggered by {seen['event']!r}; a release run is a push,"
                                    " dispatch or schedule")
    try:
        where, why = _ref_kind(found["slug"], seen["branch"], seen["head_sha"], defaults)
    except github.Unreadable as e:
        return "sha-unverifiable", f"gh cannot read the refs of {found['slug']}: {e}"
    if where is None:
        return "run-wrong-branch", f"run {found['id']} ran on {seen['branch']!r}, {why}"
    try:
        protected = _ref_protected(found["slug"], where, seen["branch"], defaults)
    except github.Unreadable as e:
        return "sha-unverifiable", f"gh cannot read the rules that protect {seen['branch']!r} of {found['slug']}: {e}"
    if not protected:
        return "ref-unprotected", (f"run {found['id']} ran on {seen['branch']!r}, which no active ruleset or branch"
                                   f" protection of {found['slug']} limits to the people it names; ask a person to protect it")
    if seen["conclusion"] != "success":
        return "run-not-success", f"run {found['id']} concluded {seen['conclusion'] or 'nothing yet'!r}"
    named = [j for j in seen["jobs"] if j["name"] == job]
    if not named or any(j["conclusion"] != "success" for j in named):
        have = ", ".join(f"{j['name']}={j['conclusion']}" for j in named) or "no such job"
        return "job-not-success", f"job {job!r} did not conclude success in run {found['id']} ({have})"
    sha = (run["sha"] or "").lower()
    if not seen["head_sha"].lower().startswith(sha):
        return "run-wrong-sha", f"run {found['id']} ran {seen['head_sha'][:12]}, the evidence says {sha[:12]}"
    return None, None


def verify(conn, row):
    """Refuse a program's close (gate `close`) when a deciding run does not prove its release; no-op for other items."""
    if row["kind"] != "program":
        return
    missing = check(conn, row)
    if missing:
        raise gate_unmet(row["id"], "close", missing)
