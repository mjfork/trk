"""Blast radius: rating a ticket's changed files RED, YELLOW or GREEN and recording its lane.

A file's colour is the highest of every `.trk/blast-radius` rule matching it, raised to at least YELLOW
by any ADR whose `Scope:` line matches it. Both are read at the commit being rated, so the rating of a
sha never changes. A `SENSITIVE <glob>` rule rates its files the same way but never stores their names:
the row keeps a count per colour for the glob instead. `items` is imported inside the functions that need it.
"""

import functools
import json
import os
import re

from . import actors, evidence, github, gitrepo, stages
from .errors import refused, usage

RULES_FILE = ".trk/blast-radius"
ADR_DIR = "docs/adr"
COLOURS = ("green", "yellow", "red")  # lowest first
_RULE = re.compile(r"(RED(?::([a-z0-9][a-z0-9_-]*))?|YELLOW|SENSITIVE)\s+(\S+)")
RED_ROLE = "engineering"  # the role a bare `RED <glob>` rule names
RED_ASK_TITLE = "Read RED changes in "

# The ticket section a RED ticket's independent auditor writes before auditing, and the nine headings it holds
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
    RED file, the roles that review it (every role named by a matching RED rule; empty otherwise)."""
    colour, rule, roles = "green", None, set()
    for rule_colour, glob, line, role in rules:
        if rule_colour == "sensitive" or not matches(glob, path):
            continue
        if COLOURS.index(rule_colour) > COLOURS.index(colour):
            colour, rule, roles = rule_colour, line, set()
        if rule_colour == colour and role is not None:
            roles.add(role)
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
    between words, lower case, a curly apostrophe made straight."""
    text = re.sub(r"^\d{1,2}[.)]\s+", "", text.strip().replace("\u2019", "'"))
    return " ".join(text.replace("*", "").replace("_", " ").rstrip(":.").split()).lower()


def brief_missing(text):
    """The `BRIEF_HEADINGS` (in order) that `text` lacks or leaves empty. A heading is a Markdown `#` heading
    or a line that is only bold text (with or without a list number); the body of one runs to the next of
    the nine, so a sub-heading or list inside it is content. Lines in a code fence are never headings."""
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
    return [h for h in BRIEF_HEADINGS if not "".join(bodies.get(h, ())).strip()]


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


def classify(repo, base, sha, role_exists=None):
    """The `blast-radius` detail for the changes `base...sha`: every changed file rated, and the lane
    (the highest colour; GREEN for no change)."""
    names = gitrepo.changed_files(repo, base, sha)
    if names is None:
        return None
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
    when the PR's own diff (`gh pr view`) lacks files the rating would include. A RED lane
    opens the RED-read asks (`open_red_ask`), and the notes saying why one was not opened are appended to
    `notes`. Returns the row's id."""
    from . import items

    conn = tx.conn
    row = items.get(conn, raw_id)
    item_id = row["id"]
    stages.require_ticket(row, "blast-radius rates")
    if row["status"] == "closed":
        raise refused("closed", f"{item_id} is closed", item=item_id)
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
    if gitrepo.resolve(repo, sha) is None:
        raise refused("sha-unverifiable", f"{sha} names no commit in {repo}", item=item_id)
    if gitrepo.read_file(repo, sha, RULES_FILE) is None:
        raise refused("no-blast-rules", f"{repo} has no {RULES_FILE} at {sha}: without it every change would rate "
                      f"GREEN and merge unreviewed; commit one (rules, or only a `#` comment line for a repo that "
                      "is deliberately all GREEN) and rate again", item=item_id)
    explicit = base is not None
    pull = pull_request(evidence.weigh(conn, row), repo, item_id, sha, notes)
    label = base
    if not explicit:
        base = pre_merge_base(conn, row, repo, sha)
        if base is None and pull is not None:
            base, label = fresh_pr_base(repo, pull, item_id)
        base = base or gitrepo.default_base(repo)
        label = label or base
        if base is None:
            raise usage(f"{repo} has none of origin/HEAD, main or master; pass --base")
    elif gitrepo.resolve(repo, base) is None:
        raise refused("sha-unverifiable", f"--base {base} names no commit in {repo}", item=item_id)
    detail = classify(repo, base, sha, lambda role: role_exists(conn, role, item_id))
    if detail is None:
        raise refused("sha-unverifiable", f"git cannot diff {base}...{sha} in {repo}", item=item_id)
    if pull is not None and pull["files"] is not None:
        check_own_files(repo, base, sha, pull, item_id)
    if not detail["files"] and not detail.get("sensitive") and not explicit:
        raise refused("empty-diff", f"{sha} changes nothing against the default base {base} (already merged?);"
                      " pass --base <the commit the ticket branched from>", item=item_id)
    counts = colour_counts(detail)
    note = f"{detail['lane']}: " + ", ".join(f"{n} {c}" for c, n in counts.items()) + f" (against {label})"
    evidence_id = evidence.record(tx, row, "blast-radius", "merged", sha=sha, note=note, detail=detail)
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
    `gh` is not installed (a note says the PR was not checked); rule `sha-unverifiable` when `gh` cannot read it."""
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
    return {"ref": pr["ref"], "base": found["base"], "files": files}


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


def check_own_files(repo, base, sha, pull, item_id):
    """Rule `stale-base` unless every file `base...sha` changes is in the pull request's own diff."""
    names = gitrepo.changed_files(repo, base, sha, renames=True)
    own = set(pull["files"])
    extra = [n for n in names or () if n not in own]
    if extra:
        shown = ", ".join(extra[:5]) + (f" and {len(extra) - 5} more" if len(extra) > 5 else "")
        raise refused("stale-base", f"{base}...{sha} changes {len(names)} files, {len(extra)} of them not in"
                      f" {pull['ref']}'s own diff ({len(own)} files): {shown}. The base is not the PR's own."
                      " Omit --base so trk uses the PR's freshly fetched base branch", item=item_id)


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
    pr = weighing.latest(("pr",), "built")
    pr_ref = pr["ref"] if pr is not None else sha
    record_it = (f"`trk review record {row['id']} --ref <review URL or local:<person>@{sha}> --sha {sha} approved"
                 " --note \"…\" --as <person>`")  # no ask is open to answer, so a direct review is refused
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
                         f" {role} at {sha} ({record_it})")
            continue
        question = _red_question(conn, row, weighing, sha, red)
        ask_id = asks.open_ask(tx, row["id"], role, f"{RED_ASK_TITLE}{pr_ref}", question)
        conn.execute("UPDATE items SET red_ask = ? WHERE id = ?", (ask_id, row["id"]))
    return notes


def open_red_asks(conn, item_id):
    """The ticket's open RED-read asks (`asks.open_under` rows)."""
    from . import asks

    return [a for a in asks.open_under(conn, item_id) if a["title"].startswith(RED_ASK_TITLE)]


def refresh_red_asks(tx, row):
    """Rewrite each open RED-read ask's question from the ticket's latest counting RED rating, so it shows the
    `review-brief` as it is now. Called when that section is written or removed; nothing else changes."""
    from . import items

    conn = tx.conn
    weighing = evidence.weigh(conn, row)
    rated = weighing.latest(("blast-radius",), "merged")
    if rated is None:
        return
    detail = json.loads(rated["detail"])
    if detail["lane"] != "red":
        return
    open_asks = {a["addressee_role"]: a["id"] for a in open_red_asks(conn, row["id"])}
    for role, red in red_roles(detail).items():
        if role in open_asks:
            items.set_section(tx, open_asks[role], "question", _red_question(conn, row, weighing, rated["sha"], red))


def brief_block(conn, row):
    """The RED-read ask's brief part: the ticket's complete `review-brief` as written, else a line saying the
    brief is pending (and what an incomplete one lacks), so an approver is never shown a half-written one."""
    from . import sections

    text = sections.body(conn, row["id"], BRIEF_HANDLE)
    missing = brief_missing(text) if text is not None else list(BRIEF_HEADINGS)
    if text is not None and not missing:
        return f"Review brief, written by the independent auditor:\n\n{text.strip()}\n"
    lacking = "" if text is None else " (the draft still lacks " + ", ".join(missing) + ")"
    return (f"Review brief: brief pending{lacking}. The independent auditor writes the `{BRIEF_HANDLE}` section"
            " when it audits; this question shows it once it is complete.\n")


def _red_question(conn, row, weighing, sha, red):
    """A RED-read ask's question for the head `sha`: the lead line, the ticket's review brief (or that it is
    pending), then the PR, the role's RED files `red` (each already formatted) and the command that records
    the approval."""
    record_it = f"`trk review record {row['id']} approved --sha {sha} --note \"…\" --as <person>`"
    pr = weighing.latest(("pr",), "built")
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
    approval cannot precede the RED context it approves."""
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
    return evidence.add(tx, row["id"], "human-review", "merged", ref=f"{DIRECT}{tx.actor}@{head}", sha=head,
                        verdict=REVIEW_VERDICTS[outcome], note=note, direct=True)
