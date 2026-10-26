"""Blast radius: rating a ticket's changed files RED, YELLOW or GREEN and recording its lane.

A file's colour is the highest of every `.trk/blast-radius` rule matching it, raised to at least YELLOW
by any ADR whose `Scope:` line matches it. Both are read at the commit being rated, so the rating of a
sha never changes. The rules file itself is a hard floor of RED, whatever the file says at the rated commit:
a change cannot edit away the rule that rates it. A `SENSITIVE <glob>` rule rates its files the same way but never stores their names:
the row keeps a count per colour for the glob instead. `items` is imported inside the functions that need it.
"""

import functools
import json
import os
import re

from . import actors, claims, evidence, github, gitrepo, ledger, stages
from .errors import TrkError, refused, usage

RULES_FILE = ".trk/blast-radius"
ADR_DIR = "docs/adr"
COLOURS = ("green", "yellow", "red")  # lowest first
_RULE = re.compile(r"(RED(?::([a-z0-9][a-z0-9_-]*))?|YELLOW|SENSITIVE)\s+(\S+)")
RED_ROLE = "engineering"  # the role a bare `RED <glob>` rule names
RULES_FLOOR = f"RED {RULES_FILE}"  # the rule line shown for the rules file when no rule of the file itself says so
RED_ASK_TITLE = "Read RED changes in "

# The ticket section a RED ticket's auditor session writes before auditing, and the nine headings it holds
# (Michael, 2026-10-23, on the tr-ht564 RED-read ask).
BRIEF_HANDLE = "review-brief"
BRIEF_HEADINGS = ("Decision", "What changes and why", "Why it's RED", "Impact if wrong", "Evidence", "Rollback",
                  "Decision criteria", "Where to look", "Recommendation")
_ATX = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$")
_BOLD_LINE = re.compile(r"^\s{0,3}(?:\d{1,2}[.)]\s+)?(?:\*\*|__)(.+?)(?:\*\*|__)\s*:?\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


@functools.cache
def _pattern(glob):
    """A repo-relative path glob as a regex: `**` crosses directories (`**/` is zero or more of them),
    `*` and `?` stay within one."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out))


def matches(glob, path):
    return _pattern(glob).fullmatch(path) is not None


def parse_rules(text, where=RULES_FILE, role_exists=None):
    """[(colour, glob, rule line, role)] from a rules file (a `SENSITIVE` rule's colour is `sensitive`); `role` is the role that reviews a RED rule's files
    (`RED:<role> <glob>`, a bare `RED` meaning `engineering`) and None for YELLOW. A line that is neither a
    rule nor a `#` comment is a usage error naming it, so a typo never silently rates a risky file GREEN; so
    is a RED rule naming a role that `role_exists` (when given) says does not exist."""
    rules = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        found = _RULE.fullmatch(line)
        if found is None:
            raise usage(f"{where} line {number}: expected `RED <glob>`, `RED:<role> <glob>`, `YELLOW <glob>`"
                        f" or `SENSITIVE <glob>`, got {line!r}")
        colour = "red" if found[1].startswith("RED") else found[1].lower()
        role = (found[2] or RED_ROLE) if colour == "red" else None
        if role is not None and role_exists is not None and not role_exists(role):
            raise usage(f"{where} line {number}: {role!r} is not a role (see `trk roles`), got {line!r}")
        rules.append((colour, found[3], line, role))
    return rules


def adr_scopes(repo, sha):
    """[(adr path, [glob])] for every `docs/adr/*.md` at `sha` with a `Scope:` line."""
    scopes = []
    for path in gitrepo.list_dir(repo, sha, ADR_DIR):
        if not path.endswith(".md"):
            continue
        for line in (gitrepo.read_file(repo, sha, path) or "").splitlines():  # `path` keeps git's raw bytes
            if line.strip().startswith("Scope:"):
                globs = [g.strip() for g in line.split(":", 1)[1].split(",") if g.strip()]
                scopes.append((gitrepo.display(path), globs))
                break
    return scopes


def rate(path, rules, scopes):
    """One file's rating: its colour, the winning rule line, the first ADR whose scope matched and, for a
    RED file, the roles that review it (every role named by a matching RED rule; empty otherwise). The rules
    file is never below RED: a change that deletes its own RED rule, or empties the file, still rates RED, with
    `RULES_FLOOR` as the rule and `RED_ROLE` as the reviewer unless a matching RED rule names its own."""
    colour, rule, roles = "green", None, set()
    for rule_colour, glob, line, role in rules:
        if rule_colour == "sensitive" or not matches(glob, path):
            continue
        if COLOURS.index(rule_colour) > COLOURS.index(colour):
            colour, rule, roles = rule_colour, line, set()
        if rule_colour == colour and role is not None:
            roles.add(role)
    if path == RULES_FILE and colour != "red":
        colour, rule, roles = "red", RULES_FLOOR, {RED_ROLE}
    adr = next((adr for adr, globs in scopes if any(matches(g, path) for g in globs)), None)
    if adr is not None and colour == "green":
        colour = "yellow"
    return {"path": path, "colour": colour, "rule": rule, "adr": adr, "roles": sorted(roles)}


def red_roles(detail):
    """{role: [RED paths it reviews, each in backticks]} for a `blast-radius` detail, roles in name order. A RED
    file stored before rules named roles has no `roles` key and is `engineering`'s. A sensitive directory's RED
    files are listed as one count line under each role."""
    by_role = {}
    for f in detail["files"]:
        if f["colour"] == "red":
            for role in f.get("roles") or [RED_ROLE]:
                by_role.setdefault(role, []).append(f"`{f['path']}`")
    for d in detail.get("sensitive", ()):
        for role, n in d["roles"].items():
            by_role.setdefault(role, []).append(f"{n} red file(s) under `{d['dir']}` (sensitive: names not stored)")
    return dict(sorted(by_role.items()))


def _heading_key(text):
    """A heading's text as it is compared: no list number, emphasis marks or end punctuation, one space
    between words, lower case, a curly apostrophe made straight, "it is" and "it's" alike."""
    text = re.sub(r"^\d{1,2}[.)]\s+", "", text.strip().replace("\u2019", "'"))
    text = " ".join(text.replace("*", "").replace("_", " ").rstrip(":.").split()).lower()
    return re.sub(r"\bit is\b", "it's", text)


def brief_bodies(text):
    """{heading: body text} for each of the `BRIEF_HEADINGS` that `text` has a non-empty body under, in heading
    order. A heading is a Markdown `#` heading or a line that is only bold text (with or without a list
    number); the body of one runs to the next of the nine, so a sub-heading or list inside it is content.
    Lines in a code fence are never headings."""
    wanted = {_heading_key(h): h for h in BRIEF_HEADINGS}
    bodies, current, fenced = {}, None, False
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        found = None if fenced else _ATX.match(line) or _BOLD_LINE.match(line)
        key = _heading_key(found.group(1)) if found else None
        if key in wanted:
            current = wanted[key]
            bodies.setdefault(current, [])
        elif current is not None:
            bodies[current].append(line)
    joined = {h: "\n".join(bodies[h]).strip() for h in BRIEF_HEADINGS if h in bodies}
    return {h: body for h, body in joined.items() if body}


def brief_missing(text):
    """The `BRIEF_HEADINGS` (in order) that `text` lacks or leaves empty (see `brief_bodies`)."""
    have = brief_bodies(text)
    return [h for h in BRIEF_HEADINGS if h not in have]


def brief_gap(conn, row, weighing=None):
    """What a RED ticket's `review-brief` lacks, as `(why, missing headings)`; None when the ticket is not RED
    (its latest counting `blast-radius` row is another lane, or there is none) or the brief is complete."""
    from . import sections

    if lane(conn, row, weighing) != "red":
        return None
    text = sections.body(conn, row["id"], BRIEF_HANDLE)
    if text is None:
        return f"{row['id']} has no {BRIEF_HANDLE} section", list(BRIEF_HEADINGS)
    missing = brief_missing(text)
    if not missing:
        return None
    return f"{BRIEF_HANDLE} of {row['id']} lacks " + ", ".join(f"'{h}'" for h in missing), missing


def check_brief(conn, row):
    """Refuse (rule `brief-required`) an audit of a ticket whose latest counting `blast-radius` row is RED
    unless its `review-brief` section is written and holds all nine headings, each with text; the message
    names what is missing. Any other lane, or no rating, passes. The Merged gate asks the same of a RED
    ticket (`brief_needs`), so an audit recorded before the rating, or a brief removed since, does not get
    around it."""
    gap = brief_gap(conn, row)
    if gap is None:
        return
    why, _ = gap
    raise refused("brief-required", f"{row['id']} is rated RED, so its audit needs a complete {BRIEF_HANDLE}: {why}."
                  f" Write it with `trk section set {row['id']} {BRIEF_HANDLE}` under these headings, in this"
                  f" order: {', '.join(BRIEF_HEADINGS)}", item=row["id"])


def brief_needs(conn, row, weighing=None):
    """The Merged gate's `review-brief` need for a ticket whose latest counting rating is RED and whose brief
    is missing or lacks a heading; empty otherwise."""
    gap = brief_gap(conn, row, weighing)
    if gap is None:
        return []
    return [{"need": BRIEF_HANDLE, "missing": gap[1]}]


def classify(repo, base, sha, role_exists=None, own=None):
    """The `blast-radius` detail for the changes `base...sha`: every such file rated, and the lane (the highest colour;
    GREEN for no change). The rules are those of `sha`'s tree. `own` is the PR's own paths for a rating at a later
    commit (`pr_paths`): they are the whole list, since `base...sha` also holds every PR that landed since, and they stay
    whatever the later commits did to them."""
    if own is None:
        names = gitrepo.changed_files(repo, base, sha)
        if names is None:
            return None
    else:
        names = sorted(set(own))
    rules_text = gitrepo.read_file(repo, sha, RULES_FILE)
    rules = parse_rules(rules_text, role_exists=role_exists) if rules_text is not None else []
    scopes = adr_scopes(repo, sha)
    hidden = [glob for colour, glob, _, _ in rules if colour == "sensitive"]
    files, sensitive = [], {}
    for name in names:
        rated = rate(name, rules, scopes)
        glob = next((g for g in hidden if matches(g, name)), None)
        if glob is None:
            files.append(rated)
            continue
        entry = sensitive.setdefault(glob, {"dir": glob, **dict.fromkeys(reversed(COLOURS), 0), "roles": {}})
        entry[rated["colour"]] += 1
        for role in rated["roles"]:
            entry["roles"][role] = entry["roles"].get(role, 0) + 1
    colours = [f["colour"] for f in files] + [c for d in sensitive.values() for c in COLOURS if d[c]]
    detail = {"lane": max(colours, key=COLOURS.index, default="green"), "files": files}
    if sensitive:
        detail["sensitive"] = list(sensitive.values())
    return detail


def colour_counts(detail):
    """{colour: changed files}, highest colour first, over the named files and the sensitive counts."""
    return {c: sum(f["colour"] == c for f in detail["files"]) + sum(d[c] for d in detail.get("sensitive", ()))
            for c in reversed(COLOURS)}


def record(tx, raw_id, sha=None, base=None, repo=None, notes=None):
    """`trk blast-radius`: rate the ticket's changes at `sha` (default the head sha) against `base`
    (default the pre-merge point when the merge row is a merge commit, else, for a ticket whose `pr` row
    names a GitHub pull request, the tip of that PR's base branch fetched from origin now, else the
    merge-base with origin/HEAD, else main, else master) and record one `blast-radius` row at that sha. Rule
    `sha-unverifiable` when git cannot place a commit; rule `empty-diff` when the default base shows no
    change (the head already merged), so a merged ticket is never rated GREEN by accident; rule `stale-base`
    when the PR's own diff (`gh pr view`) lacks files the rating would include; rule `base-hides-pr` when an explicit
    `--base` starts after commits of the PR itself (`check_base`) or leaves out files of the PR's own diff
    (`check_own_files`), the base is recorded in the detail for the Merged gate to check again. A RED lane
    opens the RED-read asks (`open_red_ask`); any open RED-read ask the rating no longer needs is withdrawn first
    (`withdraw_stale_red_asks`). The notes saying why an ask was not opened, or was withdrawn, are appended to
    `notes`. Returns the row's id."""
    from . import items

    conn = tx.conn
    row = items.get(conn, raw_id)
    item_id = row["id"]
    stages.require_code(row, "blast-radius rates")
    if row["status"] == "closed":
        raise refused("closed", f"{item_id} is closed", item=item_id)
    claims.require_held(tx, row, "blast-radius")
    if sha is None:
        sha = evidence.weigh(conn, row).head
        if sha is None:
            raise usage(f"{item_id} has no head sha yet: record its `pr` row first, or pass --sha")
    else:
        sha = evidence.check_sha(sha)
    if repo is not None:
        repo = os.path.expanduser(repo)
        if not gitrepo.is_checkout(repo):
            raise refused("sha-unverifiable", f"--repo {repo} is not a git checkout", item=item_id)
    else:
        repo = gitrepo.place(gitrepo.repos_of(conn, item_id), sha)
        if repo is None:
            raise refused("sha-unverifiable", f"{item_id} has no repo that is a local git checkout (nor has any"
                          " ancestor); add one with `trk edit --add-repo`, or pass --repo", item=item_id)
    full = gitrepo.resolve(repo, sha)
    if full is None:
        raise refused("sha-unverifiable", f"{sha} names no commit in {repo}", item=item_id)
    sha = full  # stored in full, so a RED-read ask's `red_read.sha` meets its contract
    if gitrepo.read_file(repo, sha, RULES_FILE) is None:
        raise refused("no-blast-rules", f"{repo} has no {RULES_FILE} at {sha}: without it every change would rate "
                      f"GREEN and merge unreviewed; commit one (rules, or only a `#` comment line for a repo that "
                      "is deliberately all GREEN) and rate again", item=item_id)
    explicit = base is not None
    pull = pull_request(evidence.weigh(conn, row), repo, item_id, sha, notes)
    label = base
    if not explicit:
        base, label = branch_from(conn, row, repo, sha, pull, item_id)
        if base is None:
            raise usage(f"{repo} has none of origin/HEAD, main or master, so trk cannot show where the PR began: fetch"
                        " origin, or rate with `gh` installed so the PR's own files can be compared")
    elif gitrepo.resolve(repo, base) is None:
        raise refused("sha-unverifiable", f"--base {base} names no commit in {repo}", item=item_id)
    weighing = evidence.weigh(conn, row)
    late = late_merge(repo, base, sha, weighing, item_id, notes)
    if late:
        check_on_base(repo, sha, pull, item_id)
    judged, own_files = judged_files(repo, pull, sha, weighing.head if late else None)
    if explicit:  # a default base is the branch point's own source: it cannot start after the PR began
        ref, ref_label = proof_ref(conn, row, repo, sha, pull, item_id, notes)
        files_checked = own_files is not None and gitrepo.same(repo, pull["head"], judged) is True
        check_base(repo, base, sha, weighing.head, ref, ref_label, files_checked, item_id)
    own = pr_paths(repo, base, weighing.head, late) if late else None
    if late and own is None:
        raise refused("sha-unverifiable", f"git cannot diff the merge {late[0]} in {repo}", item=item_id)
    if late:
        weakened = weakened_rules(repo, late[0], sha)
        if weakened is None:
            raise refused("sha-unverifiable", f"git cannot diff {late[0]}...{sha} in {repo}", item=item_id)
        own = sorted({*own, *weakened})
    detail = classify(repo, base, sha, lambda role: role_exists(conn, role, item_id), own)
    if detail is None:
        raise refused("sha-unverifiable", f"git cannot diff {base}...{sha} in {repo}", item=item_id)
    detail["base"] = gitrepo.resolve(repo, base)  # the Merged gate checks it again (`evidence._base_hides`)
    if late:  # what makes the row count at Merged (`evidence._late_rating`)
        detail["merge"] = gitrepo.resolve(repo, late[0])
    if own_files is not None:
        check_own_files(repo, base, judged, {**pull, "files": own_files}, item_id)
    if not detail["files"] and not detail.get("sensitive") and not explicit:
        raise refused("empty-diff", f"{sha} changes nothing against the default base {base} (already merged?);"
                      " pass --base <the commit the ticket branched from>", item=item_id)
    counts = colour_counts(detail)
    note = f"{detail['lane']}: " + ", ".join(f"{n} {c}" for c, n in counts.items()) + f" (against {label})"
    evidence_id = evidence.record(tx, row, "blast-radius", "merged", sha=sha, note=note, detail=detail)
    stale = withdraw_stale_red_asks(tx, row, evidence_id, detail)
    if notes is not None:
        notes.extend(f"withdrew {ask_id}: {why}" for ask_id, why in stale)
    if detail["lane"] == "red":
        skipped = open_red_ask(tx, row, evidence_id, detail)
        if notes is not None:
            notes.extend(skipped)
    return evidence_id


def pull_request(weighing, repo, item_id, sha, notes):
    """What `gh` says of the ticket's pull request ({"ref", "base", "files"}, see `github.view`), when its
    latest counting `pr` row names one. Its `files` are None, with a note, when the PR's head is neither the rated
    `sha` nor a descendant of it (an older head of a ticket with several PRs, or a force-push): the PR's list is
    then not the list of what is rated. None when the ref is not a GitHub pull request (a branch, `none`), or
    `gh` is not installed (a note says the PR was not checked); rule `sha-unverifiable` when `gh` cannot read it. `listed`
    is the PR's file list whatever the head, for a late rating, whose rated sha is past it (`judged_files`)."""
    pr = weighing.latest(("pr",), "built")
    if pr is None or not github.is_pr_ref(pr["ref"]):
        return None
    try:
        found = github.view(repo, pr["ref"])
    except github.Unreadable as e:
        raise refused("sha-unverifiable", f"gh cannot read {pr['ref']} in {repo}: {e}; a rating cannot be checked"
                      " against the PR's own files", item=item_id) from None
    if found is None:
        if notes is not None:
            notes.append(f"gh is not installed: {pr['ref']}'s base and own files were not checked, so a stale base"
                         " would go unnoticed")
        return None
    files = found["files"]
    if gitrepo.contains(repo, sha, found["head"]) is not True:
        files = None
        if notes is not None:
            notes.append(f"{pr['ref']}'s head {found['head'][:9]} is not {sha[:9]} or a descendant of it: the PR's"
                         " own files were not used to check the rating")
    return {"ref": pr["ref"], "base": found["base"], "files": files, "head": found["head"], "listed": found["files"]}


def judged_files(repo, pull, sha, head):
    """(the commit, the pull request's file list) a rating's diff is compared with (`check_own_files`): `sha` and the
    PR's files (`pull_request` leaves them out when they say nothing of what is rated). For a late rating (`head`, the
    ticket's head, given) the commit is that head: the rated sha is past the PR, so `base...head` is what `gh` lists,
    which proves a base the ticket's own rows cannot (a fast-forward merge shows no branch point), when `gh`'s head is
    the ticket's."""
    if pull is None:
        return sha, None
    if head is not None:
        return head, pull["listed"] if gitrepo.same(repo, pull["head"], head) is True else None
    return sha, pull["files"]


def fresh_pr_base(repo, pull, item_id):
    """(the sha, its label) of the pull request's base branch on origin as of now: fetched, never the checkout's
    cached copy, which is how a rating once diffed against develop as it was days before. Rule
    `sha-unverifiable` when it cannot be fetched."""
    tip = gitrepo.fetch_tip(repo, pull["base"])
    if tip is None:
        raise refused("sha-unverifiable", f"cannot fetch {pull['base']} from origin in {repo}, the base of"
                      f" {pull['ref']}; a stale copy would rate files the PR never touched. Fetch and retry, or pass"
                      " --base", item=item_id)
    return tip, f"origin/{pull['base']} {tip[:9]}"


def branch_from(conn, row, repo, sha, pull, item_id):
    """(a commit, its label) of the branch the ticket's PR merges into as it stood before the PR, the default base: the
    pre-merge point when the merge row is a merge commit of this head, else the PR's base branch fetched from origin
    now, else `gitrepo.default_base`. (None, None) when the repo has none of them."""
    base = pre_merge_base(conn, row, repo, sha)
    if base is not None:
        return base, base
    if pull is not None:
        return fresh_pr_base(repo, pull, item_id)
    base = gitrepo.default_base(repo)
    return base, base


def proof_ref(conn, row, repo, sha, pull, item_id, notes):
    """(a commit, its label) from which an explicit `--base` is checked for hiding the PR (`check_base`): a ref the
    builder did not just make. The PR's base branch fetched from origin now (when the fetch fails, with a note, the
    checkout's `origin/<base>`, which can only be older and so refuses more); else, without `gh`, `origin/HEAD`,
    `origin/main` or `origin/master` (`gitrepo.remote_default_base`; a repo with no origin has only its local
    default branch). When that ref holds the merge row's commit, the first parent of a merge commit or of a squash
    or rebase merge replaces it: where the PR stood before it landed. (None, None) when there is no ref."""
    ref = label = None
    if pull is not None:
        try:
            ref, label = fresh_pr_base(repo, pull, item_id)
        except TrkError as failed:
            if failed.rule != "sha-unverifiable":
                raise
            cached = f"origin/{pull['base']}"
            if gitrepo.resolve(repo, cached) is not None:
                if notes is not None:
                    notes.append(f"cannot fetch {pull['base']} from origin: {cached} as of the last fetch checks the base")
                ref = label = cached
    if ref is None:
        ref = label = gitrepo.remote_default_base(repo)
    tip = evidence.weigh(conn, row).head or sha
    merged = evidence.weigh(conn, row).latest(("merge",), "merged")
    if ref is not None and merged is not None and merged["sha"] and gitrepo.contains(repo, merged["sha"], ref) is True:
        before = gitrepo.merge_parent(repo, tip, merged["sha"])
        if before is not None:
            return before, f"the first parent of the merge {merged['sha'][:9]}"
    if ref is not None and gitrepo.contains(repo, tip, ref) is True:
        landed = gitrepo.landing_parent(repo, tip, ref)  # a no-ff merge needs no merge row to show where the PR began
        if landed is not None:
            return landed, f"the first parent of the merge that brought the PR into {label}"
    return ref, label


def check_base(repo, base, sha, head, ref, ref_label, files_checked, item_id):
    """Rule `base-hides-pr` when the diff `base...sha` would leave out commits of the PR itself (`gitrepo.hides_pr`):
    its start (the merge-base of `base` and `sha`) is `sha`, contains the PR's head or is later than the point the PR
    branched from `ref` (`branch_from`). A rating that left them out could rate a RED file GREEN. The branch point must
    be proven: `ref` has to exist and lack the PR's head. When it does not (no `gh`, and no origin/HEAD, main or master
    that is not the PR itself, or the PR already in it), the base is refused too, unless `files_checked`: the PR's own
    file list is compared with the rating (`check_own_files`), which a base that hides a file of the PR cannot pass.
    Rule `sha-unverifiable` when git cannot tell."""
    tip = head or sha
    held = None if ref is None else gitrepo.contains(repo, tip, ref)
    if held is not False:
        if not files_checked:
            known = f"{ref_label} already holds the PR" if held else "this checkout has no branch the PR merges into"
            raise refused("base-hides-pr", f"trk cannot show where the PR began ({known}), so it cannot tell whether"
                          f" --base {base} leaves out commits of the PR itself. Rate before the PR merges, or install"
                          " `gh` so the PR's own files can be compared, or omit --base", item=item_id)
        ref = None
    hides = gitrepo.hides_pr(repo, base, sha, head, ref)
    if hides is None:
        raise refused("sha-unverifiable", f"git cannot tell whether --base {base} leaves out the PR's own commits in"
                      f" {repo}", item=item_id)
    if hides:
        why, point = hides
        if why == "after-branch-point":
            what = (f"--base {base} is later than {point[:9]}, where the PR branched from {ref_label} as this checkout"
                    f" knows it. If {ref_label} is out of date, `git fetch` and retry; otherwise the base holds commits"
                    " the PR itself made")
        else:
            what = f"--base {base} {'is the rated commit' if why == 'at-sha' else 'contains the head of the PR'}"
        raise refused("base-hides-pr", f"{what}, so {base}...{sha} leaves part of the PR out of the rating. Omit --base"
                      " so trk uses the PR's own base, or pass the commit the branch was cut from", item=item_id)


def check_own_files(repo, base, sha, pull, item_id):
    """Rule `stale-base` unless every file `base...sha` changes is in the pull request's own diff; rule `base-hides-pr`
    when `sha` is the PR's head and a file in the PR's diff is not among those `base...sha` changes."""
    names = gitrepo.changed_files(repo, base, sha, renames=True)
    own = set(pull["files"])
    extra = [n for n in names or () if n not in own]
    missing = sorted(own.difference(names or ())) if gitrepo.same(repo, pull["head"], sha) is True else ()
    if missing:
        raise refused("base-hides-pr", f"{base}...{sha} lacks {len(missing)} of the {len(own)} files in {pull['ref']}'s"
                      f" own diff, the first {missing[0]}: the base leaves part of the PR out of the rating. Omit"
                      " --base so trk uses the PR's freshly fetched base branch", item=item_id)
    if extra:
        shown = ", ".join(extra[:5]) + (f" and {len(extra) - 5} more" if len(extra) > 5 else "")
        raise refused("stale-base", f"{base}...{sha} changes {len(names)} files, {len(extra)} of them not in"
                      f" {pull['ref']}'s own diff ({len(own)} files): {shown}. The base is not the PR's own."
                      " Omit --base so trk uses the PR's freshly fetched base branch", item=item_id)


def late_merge(repo, base, sha, weighing, item_id, notes):
    """(the merge sha, its `gitrepo.landing`) when `sha` is a rating the Merged gate
    takes in place of one at the head: a descendant of the commit the ticket's `merge` row names (a repo that got
    its rules after the PR merged cannot be rated at the head or the merge), else None. Rule `stale-base` for such
    a rating whose `base` already holds the merge or the PR head: its diff would leave the PR out. A merge row
    whose commit did not land the PR (`gitrepo.landing`: a later commit that merely contains the head, a commit
    that does not hold it) is no anchor: the row is recorded, with a note, and never counts. A rating before the
    merge row is recorded the same way, and must be made again after it."""
    merge, head = weighing.merge, weighing.head
    if merge is None or head is None or gitrepo.same(repo, head, sha) is not False:
        return None
    if gitrepo.contains(repo, merge, sha) is not True:
        return None
    for what, anchor in (("the merge", merge), ("the PR head", head)):
        if gitrepo.contains(repo, anchor, base) is True:
            raise refused("stale-base", f"{base} already contains {what} {anchor}, so {base}...{sha} leaves the PR out"
                          " of the rating. Pass --base <the commit the ticket branched from>", item=item_id)
    landed = gitrepo.landing(repo, head, merge)
    if not landed:
        if notes is not None:
            notes.append(f"the merge row {merge[:9]} is not the commit that landed the head {head[:9]} (a merge commit"
                         " of it, a squash or rebase of its patches, or the head itself): the rating at"
                         f" {sha[:9]} will not count")
        return None
    return merge, landed


def pr_paths(repo, base, head, late):
    """The paths the pull request itself changed, from how its merge landed it (`gitrepo.landing_paths`): `pre...merge`
    and `base...head`, and nothing of what later PRs changed. None when git cannot tell."""
    merge, landed = late
    found = gitrepo.landing_paths(repo, base, head, merge, landed)
    return None if found is None else sorted({*found[0], *found[1]})


def weakened_rules(repo, merge, sha):
    """(RULES_FILE,) when the merge's own tree already had the rules file and `merge...sha` changes it, else (): the
    rules a late rating uses are the rated tree's, so a rules file that was there at the merge and was edited since
    can be the PR's rating weakened, and it is listed at its RED floor. Rules that first arrived after the merge are the
    honest late-rules case and are not listed. None when git cannot tell."""
    if gitrepo.read_file(repo, merge, RULES_FILE) is None:
        return ()
    changed = gitrepo.changed_files(repo, merge, sha)
    if changed is None:
        return None
    return (RULES_FILE,) if RULES_FILE in changed else ()


def check_on_base(repo, sha, pull, item_id):
    """Rule `sha-off-base` unless the commit `sha` of a late rating is on the branch the PR merged into: a late rating
    takes its rules from `sha`'s tree, so a commit the builder made on top of the merge, with rules of its own, would
    rate the PR by them. With `gh` (`pull`), GitHub says so (`github.compare_status`: `behind` or `identical`), and a
    failure is `sha-unverifiable`; without it the remote-tracking default ref (`gitrepo.remote_default_base`; for a
    checkout with no origin, its local default branch) has to hold `sha`. trk does not fetch."""
    if pull is not None:
        try:
            status = github.compare_status(repo, pull["ref"], pull["base"], sha)
        except github.Unreadable as e:
            raise refused("sha-unverifiable", f"gh cannot place {sha} on {pull['base']} in {repo}: {e}; a late rating"
                          " is only taken from a commit of the branch the PR merged into", item=item_id) from None
        if status is not None:
            if status not in ("behind", "identical"):
                raise refused("sha-off-base", f"GitHub puts {sha} {status} of {pull['base']}, the branch {pull['ref']}"
                              " merged into: a late rating takes its rules from the rated commit, so it must be one"
                              " the base branch holds", item=item_id)
            return
    ref = gitrepo.remote_default_base(repo)
    held = None if ref is None else gitrepo.contains(repo, sha, ref)
    if held is None:
        raise refused("sha-off-base", f"trk cannot show {sha} is on the branch the PR merged into: {repo} has no"
                      " origin/HEAD, main or master to hold it", item=item_id)
    if not held:
        raise refused("sha-off-base", f"{sha} is not on {ref}, so a late rating there would take its rules from a commit"
                      f" the base branch never held. Rate a commit {ref} holds (`git fetch` first if {ref} is out of"
                      " date; trk does not fetch)", item=item_id)


def role_exists(conn, role, item_id):
    """Whether `role` names a role a RED rule may use: one in the role table, or the relative `parent`."""
    from . import asks

    return asks.resolve_role(conn, role, item_id) is not None or role == "parent"


def reviewed_by(weighing, actor):
    """Whether `actor`'s latest counting `human-review` row at Merged is a pass."""
    latest = None
    for row in weighing.rows:
        if row["kind"] == "human-review" and row["gate"] == "merged" and row["counts"] and row["actor"] == actor:
            latest = row
    return latest is not None and latest["verdict"] == "pass"


def open_red_ask(tx, row, evidence_id, detail):
    """The RED-read asks for the RED `blast-radius` row `evidence_id`: for each role that reviews one of its
    RED files (`red_roles`), unless that role's RED-read ask is still open, open one under the ticket to the
    role (asker the writer, title `Read RED changes in <PR ref>`, the question listing the PR and that
    role's RED files). None is opened for a row that does not count (not at the head sha, where no review
    could count either), nor for a role whose holder already has a counting `human-review` pass at the
    head (a re-rating). When a role resolves to the writer, who is then the person who must approve, none is
    opened for it (Asks' `self-addressed` rule). Returns the notes saying why a role got none, and the
    `human-review` need applies either way."""
    from . import asks, items

    conn = tx.conn
    weighing = evidence.weigh(conn, row)
    rated = next(r for r in weighing.rows if r["id"] == evidence_id)
    if not rated["counts"]:
        return []
    sha = rated["sha"]
    approve = approving_sha(weighing, sha)
    pr = weighing.latest(("pr",), "built")
    pr_ref = pr["ref"] if pr is not None else sha
    # no ask is open to answer, so a direct review is refused
    record_it = (f"`trk review record {row['id']} --ref <review URL or local:<person>@{approve}>"
                 f" --sha {approve} approved --note \"…\" --as <person>`")
    open_asks = {a["addressee_role"]: a["id"] for a in open_red_asks(conn, row["id"])}
    notes = []
    for role, red in red_roles(detail).items():
        if role in open_asks:  # a re-rating at the new head: the open ask must name that head
            items.set_section(tx, open_asks[role], "question", _red_question(conn, row, weighing, sha, red))
            continue
        addressee = asks.resolve_role(conn, role, row["id"])
        if addressee is not None and reviewed_by(weighing, addressee):
            continue
        if addressee in (None, tx.actor):
            why = f"{role} resolves to {tx.actor}, the writer" if addressee is not None else f"there is no {role} role"
            notes.append(f"no RED-read ask opened for {role}: {why}; Merged still needs a human-review pass by"
                         f" {role} at {approve} ({record_it})")
            continue
        question = _red_question(conn, row, weighing, sha, red)
        ask_id = asks.open_ask(tx, row["id"], role, f"{RED_ASK_TITLE}{pr_ref}", question)
        conn.execute("UPDATE items SET red_ask = ? WHERE id = ?", (ask_id, row["id"]))
    return notes


def withdraw_stale_red_asks(tx, row, evidence_id, detail):
    """A re-rating that counts (at the head sha) withdraws each open RED-read ask it no longer needs: every one
    when the lane is not RED, else those to a role that reviews none of the RED files now. The withdrawal is
    the ticket's own consequence of the new rating, so it skips the asker-or-owner check `trk withdraw` makes.
    Returns [(ask id, reason)]."""
    from . import asks

    conn = tx.conn
    rated = next(r for r in evidence.weigh(conn, row).rows if r["id"] == evidence_id)
    if not rated["counts"]:
        return []
    roles = set(red_roles(detail)) if detail["lane"] == "red" else set()
    why = f"re-rated {detail['lane'].upper()} at {rated['sha'][:7]}: no RED file needs this read now"
    stale = []
    for ask in open_red_asks(conn, row["id"]):
        if ask["addressee_role"] not in roles:
            asks._withdraw_row(tx, asks.open_row(conn, ask["id"]), asks.WITHDRAWN + why, why)
            stale.append((ask["id"], why))
    return stale


def approving_sha(weighing, rated):
    """The sha a person approves: the head, which a review must name. It differs from `rated` only when the rating
    is at a descendant of the merge sha (a repo that got its rules after the PR merged)."""
    head = weighing.head
    if head is None or gitrepo.same(weighing.repo.get(head, rated), head, rated) is not False:
        return rated
    return head


def open_red_asks(conn, item_id):
    """The ticket's open RED-read asks (`asks.open_under` rows)."""
    from . import asks

    return [a for a in asks.open_under(conn, item_id) if a["title"].startswith(RED_ASK_TITLE)]


def held_reason(conn, ask):
    """Why an open ask is held back from its addressee, or None: it is a RED-read ask and either its ticket's
    latest counting rating is RED while the `review-brief` is missing or lacks a heading (`review brief missing:
    <headings>`), or no rating counts at the ticket's head any more. `rating-stale` is a rating that stopped
    counting because a newer `pr` row moved the head or the ticket was moved back; `rating-missing` is any other
    case (the `blast-radius` row retracted, its sha unverifiable, or no rating at all). The addressee never sees
    a plain question about a head it cannot approve; a counting re-rating releases the ask. Computed on every
    read, so a brief or rating set or removed holds or releases it at once."""
    from . import items

    if ask["form"] != "ask" or ask["status"] != "open" or not ask["title"].startswith(RED_ASK_TITLE):
        return None
    parent = items.get(conn, ask["parent_id"])
    if not stages.is_ticket(parent):
        return None
    weighing = evidence.weigh(conn, parent)
    if weighing.latest(("blast-radius",), "merged") is None:
        stale = stale_red_rating(weighing)
        if stale is not None:
            return (f"rating-stale: the {json.loads(stale['detail'])['lane'].upper()} rating at {stale['sha'][:7]}"
                    f" no longer counts at {parent['id']}'s head; rate it again with"
                    f" `trk blast-radius {parent['id']}`")
        return (f"rating-missing: no rating of {parent['id']} counts (retracted, its sha unverifiable, or none);"
                f" rate it again with `trk blast-radius {parent['id']}`")
    gap = brief_gap(conn, parent, weighing)
    return None if gap is None else "review brief missing: " + ", ".join(gap[1])


def latest_kept(weighing, kinds, gate):
    """The latest row of `kinds` for `gate` that counts, else the latest a move back cut off (`before-reopen`):
    an open RED-read ask outlives a `reopen-stage`, and keeps showing the rating and PR it was opened from."""
    return weighing.latest(kinds, gate) or next(
        (r for r in reversed(weighing.rows)
         if r["kind"] in kinds and r["gate"] == gate and r["reason"] == "before-reopen"), None)


def refresh_red_asks(tx, row):
    """Rewrite each open RED-read ask's question from the ticket's latest counting RED rating, so it shows the
    `review-brief` and its byline as the record has them now. Called when that section is written or removed
    and when the builder record changes (`pr`/`test-run` evidence, a `builder` ledger row, a claim before Built,
    a move back to Spec'd or earlier), so the byline's independence claim never outlives the evidence against it."""
    from . import items

    conn = tx.conn
    if not open_red_asks(conn, row["id"]):
        return
    weighing = evidence.weigh(conn, row)
    rated = latest_kept(weighing, ("blast-radius",), "merged")
    if rated is None:
        return
    detail = json.loads(rated["detail"])
    if detail["lane"] != "red":
        return
    open_asks = {a["addressee_role"]: a["id"] for a in open_red_asks(conn, row["id"])}
    for role, red in red_roles(detail).items():
        if role in open_asks:
            items.set_section(tx, open_asks[role], "question", _red_question(conn, row, weighing, rated["sha"], red))


def brief_complete(conn, row):
    """Whether the ticket's `review-brief` is written and holds all nine headings, each with text."""
    from . import sections

    text = sections.body(conn, row["id"], BRIEF_HANDLE)
    return text is not None and not brief_missing(text)


def brief_written_at(conn, ticket_id):
    """When the ticket's `review-brief` section was last written or removed, from the journal's `item.updated`
    rows (their `fields` name `section:review-brief`); None if it never was. While the brief is complete that
    write is the one that left it complete, and the journal's text is what the section holds now."""
    found = conn.execute(
        "SELECT MAX(at) FROM journal WHERE item = ? AND change = 'item.updated' AND EXISTS "
        "(SELECT 1 FROM json_each(journal.data, '$.fields') WHERE value = ?)",
        (ticket_id, f"section:{BRIEF_HANDLE}")).fetchone()
    return found[0]


def brief_writer(conn, item_id):
    """`(actor, session)` of the latest write of the ticket's `review-brief` section, from its `item.updated`
    journal row; None when no write is journaled."""
    for found in conn.execute("SELECT actor, session, data FROM journal WHERE item = ? AND change = 'item.updated'"
                              " ORDER BY seq DESC", (item_id,)):
        if f"section:{BRIEF_HANDLE}" in json.loads(found["data"]).get("fields", ()):
            return found["actor"], found["session"]
    return None


def brief_byline(conn, item_id):
    """Who wrote the brief, from the section's own write: "<actor> (session <short id>)", plus ", independent of
    the builder" only when the record supports it. Independence is judged by the rule `ledger.check_independent`
    applies: the writer's session is none of the ticket's builder sessions (a `builder` ledger row, `pr` or
    `test-run` evidence, a claim held before Built) and the writer's actor is no builder actor; and at least one
    builder session is known. Computed from the record as it is now, never stored."""
    writer = brief_writer(conn, item_id)
    if writer is None:
        return "an unrecorded writer"
    actor, session = writer
    if not session:
        return actor
    sessions, _ = ledger.builders(conn, item_id)
    independent = sessions and not ledger.built_by(conn, item_id)(session, actor)
    return f"{actor} (session {session[:8]})" + (", independent of the builder" if independent else "")


def brief_block(conn, row):
    """The RED-read ask's brief part: the ticket's complete `review-brief` as written, saying who wrote it, else a
    line saying the brief is pending (and what an incomplete one lacks), so an approver is never shown a
    half-written one."""
    from . import sections

    text = sections.body(conn, row["id"], BRIEF_HANDLE)
    missing = brief_missing(text) if text is not None else list(BRIEF_HEADINGS)
    if text is not None and not missing:
        return f"Review brief, written by {brief_byline(conn, row['id'])}:\n\n{text.strip()}\n"
    lacking = "" if text is None else " (the draft still lacks " + ", ".join(missing) + ")"
    return (f"Review brief: brief pending{lacking}. An auditor session that did not build the ticket writes the"
            f" `{BRIEF_HANDLE}` section when it audits; this question shows it once it is complete. Until then the"
            " ask is held back from its addressee.\n")


BRIEF_KEYS = dict(zip(BRIEF_HEADINGS, ("decision", "what_changes", "why_red", "impact", "evidence", "rollback",
                                       "decision_criteria", "where_to_look", "recommendation")))


def stale_red_rating(weighing):
    """The latest `blast-radius` row that no longer counts only because the head moved (a newer `pr` row,
    `wrong-sha`) or the ticket was moved back (`before-reopen`); None when there is none."""
    return next((r for r in reversed(weighing.rows)
                 if r["kind"] == "blast-radius" and r["gate"] == "merged"
                 and r["reason"] in ("wrong-sha", "before-reopen")), None)


def red_read_json(conn, ask):
    """The `red_read` object of an open RED-read ask row, for item JSON: the ticket, the sha of its latest RED
    rating, the roles reviewing and the count of RED files, and the ticket's `review-brief` read by
    `brief_bodies`, the parser the gates use (`brief` has each of the nine fields as text or None;
    `brief_lacking` names the headings the gates would call missing; `brief_complete` is true exactly when
    there are none), with the actor and session of the section's latest write. `current` is whether that
    rating counts at the ticket's head now: after a newer `pr` row and before a re-rating, or after a
    `reopen-stage`, the ask stays open and keeps the last RED rating with `current` false, so a client shows
    the card without Approve. `sha` is always in full: a rating stored short (before trk stored them in full) is
    resolved in the ticket's checkout. None for any other ask, a closed one, a ticket with no RED rating, or a short
    rating no checkout resolves."""
    if ask["status"] != "open" or not ask["title"].startswith(RED_ASK_TITLE):
        return None
    from . import items

    row = items.get(conn, ask["parent_id"])
    if not stages.is_ticket(row):
        return None
    weighing = evidence.weigh(conn, row)
    rated = weighing.latest(("blast-radius",), "merged") or stale_red_rating(weighing)
    detail = json.loads(rated["detail"]) if rated is not None else None
    if detail is None or detail["lane"] != "red":
        return None
    from . import sections

    sha = rated["sha"]
    if len(sha) < 40:  # a row rated before shas were stored in full
        repo = weighing.repo.get(sha)
        sha = gitrepo.resolve(repo, sha) if repo is not None else None
        if sha is None:
            return None  # no checkout names it in full: the ask shows without its RED card
    text = sections.body(conn, row["id"], BRIEF_HANDLE)
    bodies = brief_bodies(text)
    lacking = [h for h in BRIEF_HEADINGS if h not in bodies]
    writer = brief_writer(conn, row["id"]) if text is not None else None
    return {"ticket": row["id"], "sha": sha, "current": bool(rated["counts"]), "roles": list(red_roles(detail)),
            "files_count": colour_counts(detail)["red"],
            "brief": {key: bodies.get(h) for h, key in BRIEF_KEYS.items()},
            "brief_lacking": lacking, "brief_complete": not lacking,
            "brief_writer": {"actor": writer[0], "session": writer[1]} if writer is not None else None}


def _red_question(conn, row, weighing, sha, red):
    """A RED-read ask's question for the head `sha`: the lead line, the ticket's review brief (or that it is
    pending), then the PR, the role's RED files `red` (each already formatted) and the command that records
    the approval."""
    approve = approving_sha(weighing, sha)
    record_it = f"`trk review record {row['id']} approved --sha {approve} --note \"…\" --as <person>`"
    pr = latest_kept(weighing, ("pr",), "built")
    pr_ref = pr["ref"] if pr is not None else sha
    return (f"{row['id']} ({row['title']}) is rated RED at {sha}: a person must read the PR and approve it"
            f" before it merges.\n\n{brief_block(conn, row)}\nPR: {pr_ref}\n\nRED files:\n\n"
            + "".join(f"- {entry}\n" for entry in red)
            + f"\nRecord the approval with {record_it}; it answers this ask.\n")


def answer_red_ask(tx, row, evidence_id, ref):
    """A `human-review` pass at the head sha answers, as a proxy answer by evidence in the same transaction,
    each open RED-read ask addressed to a role the recording actor holds; a pass at another sha, or by an
    actor no open RED-read ask resolves to, changes nothing."""
    from . import asks, items

    conn = tx.conn
    weighed = next(r for r in evidence.weigh(conn, row).rows if r["id"] == evidence_id)
    if not weighed["counts"]:
        return
    said = f"Approved by {tx.actor} at {weighed['sha'][:7]}" if is_direct(ref) else f"Approved on GitHub: {ref}"
    outcome = said
    if len(outcome) > items.OUTCOME_MAX:
        outcome = outcome[:items.OUTCOME_MAX - 1] + "…"
    body = f"{said}\n\nRecorded as `human-review` evidence {evidence_id} at the head sha.\n"
    for ask in open_red_asks(conn, row["id"]):
        if ask["addressee"] == tx.actor:
            asks.answer_by_evidence(tx, items.get(conn, ask["id"]), outcome, body, evidence_id)


def pre_merge_base(conn, row, repo, sha):
    """The ticket's pre-merge point when its latest counting `merge` row is a merge commit that contains `sha`
    (the merge of this head, not of an earlier PR of the ticket): that commit's first parent, unless it already
    contains `sha`. None otherwise (no merge row, another head's merge row, or a fast-forward)."""
    merged = evidence.weigh(conn, row).latest(("merge",), "merged")
    if merged is None or merged["sha"] is None or gitrepo.contains(repo, sha, merged["sha"]) is not True:
        return None  # no merge row, or one for another head: an earlier PR's merge of the same ticket
    merge_parents = gitrepo.parents(repo, merged["sha"]) or []
    if len(merge_parents) < 2 or gitrepo.contains(repo, sha, merge_parents[0]) is not False:
        return None
    return merge_parents[0]


def needs_review(lane):
    """Whether a lane merges only with its review: YELLOW and RED do, GREEN and an unrated ticket don't."""
    return lane in ("yellow", "red")


def lane(conn, row, weighing=None):
    """A ticket's lane: the lane in the detail of its latest counting `blast-radius` row; None without one."""
    if not stages.is_ticket(row):
        return None
    latest = (weighing or evidence.weigh(conn, row)).latest(("blast-radius",), "merged")
    return json.loads(latest["detail"])["lane"] if latest is not None else None


REVIEW_VERDICTS = {"approved": "pass", "changes-requested": "fail"}

DIRECT = "direct:"


def is_direct(ref):
    """Whether a `human-review` ref is a person's direct approval (`direct:<actor>@<sha>`), not a GitHub review.
    Only `review_record` writes one (`evidence.add` refuses the prefix otherwise)."""
    return ref is not None and ref.lower().startswith(DIRECT)


def review_record(tx, raw_id, ref, sha, outcome, note):
    """`trk review record`: a review as `human-review` evidence for Merged, `approved` a pass and
    `changes-requested` a fail, through `evidence add`'s checks (so an approval at the head sha answers
    the open RED-read ask). With a `ref` (a GitHub review URL, or any ref an agent records as the named
    proxy; `direct:` is reserved) it is taken as given. With none it is a person's direct approval
    (`_direct_review`). Returns the row's id."""
    if ref is None:
        return _direct_review(tx, raw_id, sha, outcome, note)
    return evidence.add(tx, raw_id, "human-review", "merged", ref=ref, sha=sha, verdict=REVIEW_VERDICTS[outcome],
                        note=note)


def _direct_review(tx, raw_id, sha, outcome, note):
    """A person's review with no GitHub behind it, stored with ref `direct:<actor>@<head>`. Refused unless the
    writer is a human actor (`approval-needs-person`), `sha` names the ticket's head commit (`not-head`; it is
    never defaulted, so a delayed command cannot approve a head the person never saw), and the head has a
    counting RED `blast-radius` row and an open RED-read ask addressed to the writer (`no-red-read`): an
    approval cannot precede the RED context it approves. An approval is also refused (`no-brief`) while the
    ticket's `review-brief` is missing a heading: nobody approves a RED change without the context to judge it
    (Michael, 2026-10-24). A `changes-requested` review is not."""
    from . import items

    row = items.get(tx.conn, raw_id)
    if actors.kind(tx.conn, tx.actor) != "human":
        raise refused("approval-needs-person", f"{tx.actor} is not a person; a review without --ref is a person's"
                      " own (pass --ref to record a GitHub review as a proxy)", item=row["id"])
    sha = evidence.check_sha(sha)
    weighing = evidence.weigh(tx.conn, row)
    head = weighing.head
    if head is None:
        raise refused("not-head", f"{row['id']} has no head sha yet (no counting pr row)", item=row["id"])
    repo = weighing.repo.get(head, sha)
    if gitrepo.same(repo, head, sha) is not True:
        raise refused("not-head", f"--sha {sha} is not the head {head} (or cannot be told from it); a review is of"
                      " the head only", item=row["id"])
    head = (gitrepo.resolve(repo, head) if repo is not None else None) or head  # the pr row may hold a short sha
    if lane(tx.conn, row, weighing) != "red" or not any(a["addressee"] == tx.actor
                                                        for a in open_red_asks(tx.conn, row["id"])):
        raise refused("no-red-read", f"{row['id']} has no counting RED rating at {head} with an open RED-read ask"
                      f" addressed to {tx.actor}; run `trk blast-radius {row['id']}` first, or record a GitHub"
                      " review with --ref", item=row["id"])
    gap = brief_gap(tx.conn, row, weighing) if outcome == "approved" else None  # a person may always send it back
    if gap is not None:
        raise refused("no-brief", f"{row['id']} is rated RED, so a direct approval needs its complete {BRIEF_HANDLE}:"
                      f" {gap[0]}. Write it with `trk section set {row['id']}"
                      f" {BRIEF_HANDLE}` under these headings, in this order: {', '.join(BRIEF_HEADINGS)}",
                      item=row["id"])
    return evidence.add(tx, row["id"], "human-review", "merged", ref=f"{DIRECT}{tx.actor}@{head}", sha=head,
                        verdict=REVIEW_VERDICTS[outcome], note=note, direct=True)
