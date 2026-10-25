"""Evidence: append-only proof rows toward one gate, their retraction, and weighing which rows count.

Every evidence row is written by `record`, whichever verb writes it, with its `evidence.added` feed row.
`weigh` is the one place that decides whether a row counts and why not; the gates in `stages` and the
criterion statuses in `criteria` read only its answer. `items` is imported inside the functions that need it.
"""

import json
import re
import secrets

from . import actors, claims, clock, criteria, gitrepo, ids, roles
from .errors import not_found, refused, usage

ID_LENGTH = 6
NOTE_MAX = 300
_SHA = re.compile(r"[0-9a-f]{7,40}")

# The gates each kind of item has (asks have none).
ITEM_GATES = {
    ("program", None): ("close",),
    ("project", None): ("accept", "close"),
    ("work_item", "spec_bound"): ("framed", "specd", "built", "merged", "verified"),
    ("work_item", "self_contained"): ("framed", "specd", "built", "merged", "verified"),
    ("work_item", "decision"): ("close",),
}

ANY = None  # a kind that may name any of its item's gates

# The spec's kinds table: kind -> {gate it may name: the fields it needs there}. ANY maps every gate.
KINDS = {
    "judgment": {"specd": (), "accept": (), "close": ("criterion",)},
    "pr": {"built": ("ref", "sha")},
    "test-run": {"built": ("ref", "sha"), "verified": ("ref", "sha", "criterion"), "close": ("ref", "criterion")},
    "webtester-run": {"verified": ("ref", "sha", "criterion"), "close": ("ref", "criterion")},
    "human-accept": {"verified": ("ref", "sha", "criterion"), "close": ("ref", "criterion")},
    "merge": {"merged": ("ref", "sha")},
    "blast-radius": {"merged": ("sha", "detail")},
    "audit": {"merged": ("sha",)},
    "human-review": {"merged": ("ref", "sha")},
    "review": {ANY: ("ref",)},
    "skip": {"merged": ()},
    "plan": {ANY: ("ref",)},
    "doc": {ANY: ("ref",)},
    "retract": {ANY: ("ref",)},
}

# Each kind's verdict: "pass" (must pass), "required" (pass or fail), "optional", or None (none allowed).
VERDICT = {
    "judgment": "pass", "pr": None, "test-run": "required", "webtester-run": "required", "human-accept": "required",
    "merge": None, "blast-radius": None, "audit": None, "human-review": "required", "review": "optional",
    "skip": None, "plan": None, "doc": None, "retract": None,
}

# Kinds the tracker writes only through their own verb, with that verb's checks.
MANAGED = {"skip": "trk skip", "blast-radius": "trk blast-radius", "audit": "trk ledger audit",
           "retract": "trk evidence retract"}

# Kinds in no weighing group: they never count.
NOT_WEIGHED = ("review", "plan", "doc", "retract")

# The kinds that prove a criterion: for `verified`, and with `judgment` for `close`.
RUNS = ("test-run", "webtester-run", "human-accept")

# Kinds that can be a red-first proof: a run meant to fail, at a base before the fix, showing the test catches
# the defect. Stored as a `fail` run with `detail.red_first`, so the schema and the feed carry the same verdicts.
RED_FIRST_KINDS = ("test-run", "webtester-run")
RED_FIRST_LABEL = "failed as intended"

# Kinds the builder records on a ticket: the writer must hold the ticket's claim (`claims.require_held`).
BUILDER_KINDS = ("pr", "test-run", "merge")

# Kinds a closed item still takes (besides a retraction).
ON_CLOSED = ("review", "doc")

# A program's close is harder to meet than a project's (tr-43mme, from tr-tfk26's hardened plan): no `judgment`
# counts, a run needs a release sha, and a `human-accept` counts only from the acceptor the criterion names.
RELEASE_RUNS = ("test-run", "webtester-run")
PROGRAM_CLOSE_REASONS = ("program-judgment", "no-release-sha", "no-acceptor-named", "not-acceptor")

# A criterion names its one acceptor in its text: `accept-by: michael` or `accept-by: security`, the name an actor id
# or a role (a role stands for the actor holding it now). Michael's ruling 2026-10-24 (A1): one acceptor, not several.
_ACCEPT_BY = re.compile(r"\baccept-by:\s*([a-z0-9][a-z0-9_-]*)", re.IGNORECASE)

_FLAG = {"ref": "--ref", "sha": "--sha", "criterion": "--criterion", "detail": "detail", "verdict": "--verdict"}


def check_note(note, flag="--note"):
    """One non-empty line of at most NOTE_MAX chars (usage error otherwise); returns it stripped."""
    note = (note or "").strip()
    if not note:
        raise usage(f"evidence needs {flag} \"<one line>\"")
    if "\n" in note or "\r" in note:
        raise usage(f"{flag} is one line, no newline")
    if len(note) > NOTE_MAX:
        raise usage(f"{flag} is at most {NOTE_MAX} chars (got {len(note)})")
    return note


def check_sha(sha):
    """A sha stored lowercase, 7 to 40 hex chars (usage error naming --sha otherwise)."""
    lowered = sha.strip().lower()
    if not _SHA.fullmatch(lowered):
        raise usage(f"--sha is 7 to 40 hex chars, got {sha!r}")
    return lowered


def acceptor(conn, text):
    """The actor a criterion's `accept-by:` marker names (a role resolves to its holder), or None when it names no
    one that exists."""
    found = _ACCEPT_BY.search(text or "")
    if found is None:
        return None
    name = found.group(1).lower()
    if conn.execute("SELECT 1 FROM actors WHERE id = ?", (name,)).fetchone():
        return name
    return roles.holder(conn, name)


def _program_close_reason(conn, row, texts):
    """Why a program `close` row cannot count, or None (`PROGRAM_CLOSE_REASONS`)."""
    if row["kind"] == "judgment":
        return "program-judgment"
    if row["kind"] in RELEASE_RUNS and not row["sha"]:
        return "no-release-sha"
    if row["kind"] == "human-accept":
        named = acceptor(conn, texts.get(row["criterion_n"]))
        if named is None:
            return "no-acceptor-named"
        if row["actor"] != named or actors.kind(conn, row["actor"]) != "human":
            return "not-acceptor"
    return None


def gates_of(row):
    return ITEM_GATES.get((row["kind"], row["form"]), ())


def _check_table(row, kind, gate, fields):
    """Refuse a row that breaks the kinds table (usage error naming the field)."""
    allowed = KINDS[kind]
    if gate not in gates_of(row):
        what = row["form"] or row["kind"]
        raise usage(f"--gate {gate} is not a gate of a {what} (its gates: {', '.join(gates_of(row)) or 'none'})")
    if ANY not in allowed and gate not in allowed:
        raise usage(f"--gate: {kind} evidence names {' or '.join(allowed)}, not {gate}")
    for field in allowed.get(gate, allowed.get(ANY)):
        if fields[field] is None:
            raise usage(f"{_FLAG[field]}: {kind} evidence for {gate} needs it")
    rule = VERDICT[kind]
    verdict = fields["verdict"]
    if rule is None and verdict is not None:
        raise usage(f"--verdict: {kind} evidence carries none")
    if rule == "required" and verdict is None:
        raise usage(f"--verdict: {kind} evidence needs pass or fail")
    if rule == "pass" and verdict == "fail":
        raise usage(f"--verdict: {kind} evidence always passes; to withdraw one, retract it")


def _new_id(conn):
    while True:
        candidate = "".join(secrets.choice(ids.ALPHABET) for _ in range(ID_LENGTH))
        if not conn.execute("SELECT 1 FROM evidence WHERE id = ?", (candidate,)).fetchone():
            return candidate


def normalize_id(raw):
    """An input evidence id read Core's way: case-insensitive, `i l o u` as `1 1 0 v`."""
    return ids.normalize(f"x-{raw}")[2:]


def record(tx, row, kind, gate, *, criterion=None, ref=None, sha=None, verdict=None, note, detail=None):
    """Write one evidence row and its `evidence.added` feed row, unchecked against the kinds table;
    `criterion` is a criterion row (its current version is recorded). Returns the new id."""
    from . import items

    conn = tx.conn
    evidence_id = _new_id(conn)
    n = criterion["n"] if criterion is not None else None
    version = criterion["version"] if criterion is not None else None
    data = {"id": evidence_id, "gate": gate, "kind": kind, "criterion": criteria.handle(n) if n else None,
            "criterion_version": version, "sha": sha, "verdict": verdict, "ref": ref,
            "note": criteria.excerpt(note)}
    seq = tx.journal("evidence.added", items.feed_fields(conn, row["id"]), data=data)
    conn.execute(
        "INSERT INTO evidence (id, item_id, gate, criterion_n, criterion_version, kind, ref, sha, verdict, note,"
        " detail, actor, session, seq, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (evidence_id, row["id"], gate, n, version, kind, ref, sha, verdict, note,
         json.dumps(detail) if detail is not None else None, tx.actor, tx.session, seq, clock.stamp(tx.now)),
    )
    conn.execute("UPDATE items SET updated_at = ? WHERE id = ?", (clock.stamp(tx.now), row["id"]))
    if kind in ("pr", "test-run"):  # a new builder session: an open RED-read ask's byline may change
        items.refresh_builder_asks(tx, row["id"])
    return evidence_id


def add(tx, raw_id, kind, gate, *, criterion=None, ref=None, sha=None, verdict=None, note=None, direct=False,
        red_first=False):
    """`evidence add`: one row of a kind agents record, checked against the kinds table; returns its id.
    A `human-review` pass also answers the ticket's open RED-read ask when it counts (`blast.answer_red_ask`).
    `direct` (only `blast.review_record` passes it) allows the reserved `direct:` ref.
    `red_first` records a run that failed on purpose: it needs a run kind, gate `verified` and a criterion, and
    takes no verdict but `fail`."""
    from . import items

    if kind in MANAGED:
        raise usage(f"--kind {kind} is written by `{MANAGED[kind]}`, with its checks, not by `trk evidence add`")
    if kind not in KINDS:
        raise usage(f"--kind is one of {', '.join(k for k in KINDS if k not in MANAGED)}, got {kind!r}")
    note = check_note(note)
    if ref is not None:
        ref = ref.strip() or None
    sha = check_sha(sha) if sha is not None else None
    row = items.get(tx.conn, raw_id)
    if row["status"] == "closed" and kind not in ON_CLOSED:
        raise refused("closed", f"{row['id']} is closed; it takes only {' or '.join(ON_CLOSED)} evidence",
                      item=row["id"])
    if kind == "human-review" and not direct and ref is not None and ref.lower().startswith("direct:"):
        raise refused("reserved-ref", "a ref starting direct: marks a person's direct approval, which only"
                      " `trk review record` without --ref writes", item=row["id"])
    criterion_row = _live_criterion(tx.conn, row, criterion) if criterion is not None else None
    if red_first:
        if kind not in RED_FIRST_KINDS or gate != "verified" or criterion_row is None:
            raise usage(f"--red-first: a {' or '.join(RED_FIRST_KINDS)} row for gate verified that names a --criterion")
        if verdict not in (None, "fail"):
            raise usage("--red-first: a red-first run fails by definition; drop --verdict or use --verdict fail")
        verdict = "fail"
    fields = {"ref": ref, "sha": sha, "criterion": criterion_row, "detail": None, "verdict": verdict}
    _check_table(row, kind, gate, fields)
    if kind == "judgment" and gate in ("accept", "specd"):
        from . import stages

        stages.check_spec_judge(tx, row, gate)
    if kind in BUILDER_KINDS and row["form"] in ("spec_bound", "self_contained"):
        claims.require_held(tx, row, f"{kind} evidence")
    if VERDICT[kind] == "pass":
        verdict = "pass"
    evidence_id = record(tx, row, kind, gate, criterion=criterion_row, ref=ref, sha=sha, verdict=verdict, note=note,
                         detail={"red_first": True} if red_first else None)
    if kind == "human-review" and verdict == "pass":
        from . import blast  # a gates module: loaded only for a human review

        blast.answer_red_ask(tx, row, evidence_id, ref)
    return evidence_id


def _live_criterion(conn, row, raw_handle):
    criteria.check_takes(row)
    found = criteria.get(conn, row["id"], raw_handle)
    if found["retired"]:
        raise usage(f"--criterion: {criteria.handle(found['n'])} is retired; it takes no evidence")
    return found


def retract(tx, raw_id, raw_evidence_id, note):
    """`evidence retract`: a `retract` row withdrawing one row of the same item; returns its id.
    Retracting a retraction, a `skip` row, another item's row or a row already retracted is `bad-retract`."""
    from . import items

    note = check_note(note)
    row = items.get(tx.conn, raw_id)
    evidence_id = normalize_id(raw_evidence_id)
    target = tx.conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    if target is None:
        raise not_found(f"no evidence {raw_evidence_id!r}", item=row["id"])
    if target["item_id"] != row["id"]:
        raise refused("bad-retract", f"{evidence_id} is evidence of {target['item_id']}, not {row['id']}", item=row["id"])
    if target["kind"] in ("retract", "skip"):
        raise refused("bad-retract", f"{evidence_id} is a {target['kind']} row; it cannot be retracted", item=row["id"])
    if tx.conn.execute("SELECT 1 FROM evidence WHERE kind = 'retract' AND ref = ?", (evidence_id,)).fetchone():
        raise refused("bad-retract", f"{evidence_id} is already retracted", item=row["id"])
    return record(tx, row, "retract", target["gate"], ref=evidence_id, note=note)


# --- weighing ---

class Weighing:
    """Every evidence row of one item in seq order, each with `counts` and `reason`, plus the head sha
    (latest counting `pr`) and the merge sha (latest counting `merge`)."""

    def __init__(self, rows, head, merge, repo=None):
        self.rows, self.head, self.merge, self.repo = rows, head, merge, repo

    def tried(self, sha):
        """What a `sha-unverifiable` need says about the checkout looked in (`gitrepo.tried`)."""
        return gitrepo.tried_in(self.repo.all() if self.repo is not None else [], sha)

    def latest(self, kinds, gate, criterion_n=None):
        """The group's decider: the latest counting row of `kinds` for `gate` (and criterion), or None."""
        return self._last(kinds, gate, criterion_n, lambda row: row["counts"] and not row["red_first"])

    def unverifiable(self, kinds, gate, criterion_n=None):
        """The group's latest row when only git's answer keeps it from counting (`sha-unverifiable`), else None."""
        row = self._last(kinds, gate, criterion_n, lambda row: not row["red_first"]
                         and row["reason"] in (None, "wrong-sha", "sha-unverifiable"))
        return row if row is not None and row["reason"] == "sha-unverifiable" else None

    def _last(self, kinds, gate, criterion_n, keep):
        for row in reversed(self.rows):
            if (row["kind"] in kinds and row["gate"] == gate
                    and (criterion_n is None or row["criterion_n"] == criterion_n) and keep(row)):
                return row
        return None


def weigh(conn, item):
    """Weigh every evidence row of an item: a row counts when it is not retracted, weighed at all, names
    its criterion's current version, is newer than the ticket's latest move back below its gate (and,
    for `accept`, than the project's latest return to draft), and its sha is valid for its gate. Built's and Merged's checks
    against the head sha are skipped while there is none (the gate lists `pr` then); a `verified` row
    is `sha-unverifiable` until there is a merge sha (or Merged was skipped) to contain."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM evidence WHERE item_id = ? ORDER BY seq", (item["id"],))]
    retracted = {r["ref"] for r in rows if r["kind"] == "retract"}
    versions = dict(conn.execute("SELECT n, version FROM criteria WHERE item_id = ?", (item["id"],)).fetchall())
    texts = dict(conn.execute("SELECT n, text FROM criteria WHERE item_id = ?", (item["id"],)).fetchall())
    program = item["kind"] == "program"
    drafted = _latest_draft(conn, item) if any(r["gate"] == "accept" for r in rows) else None
    cut = _cut_offs(conn, item) if rows else {}
    for r in rows:
        if r["id"] in retracted:
            r["reason"] = "retracted"
        elif r["kind"] in NOT_WEIGHED:
            r["reason"] = "not-weighed"
        elif r["criterion_n"] is not None and r["criterion_version"] != versions.get(r["criterion_n"]):
            r["reason"] = "old-version"
        elif program and r["gate"] == "close" and (why := _program_close_reason(conn, r, texts)):
            r["reason"] = why
        elif r["seq"] < cut.get(r["gate"], 0):
            r["reason"] = "before-reopen"
        elif r["gate"] == "accept" and drafted is not None and r["seq"] < drafted:
            r["reason"] = "before-draft"
        else:
            r["reason"] = None
    head = _latest_sha(rows, "pr", "built")
    merge = _latest_sha(rows, "merge", "merged")
    repo = _Repo(conn, item["id"])
    for r in rows:
        r["red_first"] = _is_red_first(r) and not _at_or_after_base(repo, r, head, merge, bool(item["merged_skipped"]))
        if r["reason"] is None:
            r["reason"] = _sha_reason(repo, r, head, merge, bool(item["merged_skipped"]))
        r["counts"] = r["reason"] is None
    _count_landing_reviews(repo, rows, head, merge)
    paired = {(r["criterion_n"]) for r in rows
              if r["counts"] and not r["red_first"] and r["gate"] == "verified" and r["kind"] in RUNS
              and r["verdict"] == "pass"}
    for r in rows:
        if r["counts"] and r["red_first"] and r["criterion_n"] not in paired:
            r["reason"], r["counts"] = "unpaired-red-first", False
    return Weighing(rows, head, merge, repo)


def _count_landing_reviews(repo, rows, head, merge):
    """A `human-review` at the merge sha counts, as one at the head would, while the ticket's latest counting
    `blast-radius` row is a late-rules rating of that merge (`_late_rating`) listing exactly the paths the merge
    landed: the reviewer read the merge's diff, so the rating must hold no path outside it (`_covers_landing`).
    Otherwise a review is of the head only."""
    if not head or not merge or gitrepo.same(repo.get(head, merge), head, merge) is not False:
        return
    waiting = [r for r in rows if r["kind"] == "human-review" and r["gate"] == "merged" and r["reason"] == "wrong-sha"
               and r["sha"] and gitrepo.same(repo.get(r["sha"], merge), r["sha"], merge) is True]
    rating = next((r for r in reversed(rows) if r["kind"] == "blast-radius" and r["gate"] == "merged" and r["counts"]), None)
    if waiting and rating is not None and gitrepo.same(repo.get(head, rating["sha"]), head, rating["sha"]) is False \
            and _covers_landing(repo, rating, head, merge):
        for r in waiting:
            r["reason"], r["counts"] = None, True


def _covers_landing(repo, row, head, merge):
    """Whether the late rating `row` lists exactly what `merge` landed: its recorded merge is `merge`, `base...head` holds
    no path outside `pre...merge` (`gitrepo.landing_paths`), and the files the row itself lists (with its sensitive
    counts) are that many paths of `pre...merge`, as a rating made before late ratings listed only the landing, which
    carries a later PR's files, is not."""
    detail = json.loads(row["detail"]) if row["detail"] else {}
    anchor, base = detail.get("merge"), detail.get("base")
    if not anchor or not base:
        return False
    placed = repo.get(merge, head, base, anchor)
    if gitrepo.same(placed, anchor, merge) is not True:
        return False
    landed = gitrepo.landing(placed, head, merge)
    found = gitrepo.landing_paths(placed, base, head, merge, landed) if landed else None
    if found is None or not set(found[1]) <= set(found[0]):
        return False
    listed = {f["path"] for f in detail.get("files", ())}
    hidden = sum(d.get("green", 0) + d.get("yellow", 0) + d.get("red", 0) for d in detail.get("sensitive", ()))
    return listed <= set(found[0]) and len(listed) + hidden == len(set(found[0]))


def _at_or_after_base(repo, row, head, merge, merged_skipped):
    """Whether a red-first row's sha is the merge (or head, once Merged is skipped) or a descendant of it. Such a
    row is no base before the fix, so it is weighed as an ordinary fail run: a failure there must not hide."""
    base = merge or (head if merged_skipped else None)
    return bool(base) and gitrepo.contains(repo.get(row["sha"], base), base, row["sha"]) is True


def _is_red_first(row):
    """Whether the row is a red-first proof (`detail.red_first`)."""
    return bool(row["detail"]) and bool(json.loads(row["detail"]).get("red_first"))


def _latest_draft(conn, item):
    """The seq of the project's latest return to draft (`spec.changed` to draft), or None. Evidence rows
    store their `evidence.added` journal seq, so the two compare directly."""
    return conn.execute("SELECT MAX(seq) FROM journal WHERE item = ? AND change = 'spec.changed'"
                        " AND json_extract(data, '$.new') = 'draft'", (item["id"],)).fetchone()[0]


# The backward stage moves that cut off the evidence of every gate above the stage they reach.
CUTTING = ("reopen-stage", "reopen")


def _cut_offs(conn, item):
    """{ticket gate: the seq of the ticket's latest backward move (`reopen-stage` or a Verified reopen) to a
    stage below it}; gates no such move went below are absent. `spec draft`'s move back cuts nothing off."""
    from . import stages

    cut = {}
    for seq, new in conn.execute(
            "SELECT seq, json_extract(data, '$.new') FROM journal WHERE item = ? AND change = 'stage.changed'"
            f" AND json_extract(data, '$.via') IN ({', '.join('?' * len(CUTTING))}) ORDER BY seq",
            (item["id"], *CUTTING)):
        for gate in stages.STAGES[stages.STAGES.index(new) + 1:]:
            cut[gate] = seq
    return cut


def _latest_sha(rows, kind, gate):
    found = [r["sha"] for r in rows if r["kind"] == kind and r["gate"] == gate and r["reason"] is None]
    return found[-1] if found else None


class _Repo:
    """The item's checkouts, looked up on first use only (it costs a git call)."""

    def __init__(self, conn, item_id):
        self.conn, self.item_id, self.paths, self.known = conn, item_id, [], False

    def all(self):
        """Every checkout of the item, in the order searched."""
        self.get()
        return self.paths

    def get(self, *shas):
        """The checkout holding every one of `shas` (`gitrepo.place`); None when the item has none."""
        if not self.known:
            self.paths, self.known = gitrepo.repos_of(self.conn, self.item_id), True
        return gitrepo.place(self.paths, *shas)


def _sha_reason(repo, row, head, merge, merged_skipped):
    """`wrong-sha` or `sha-unverifiable` when the row's sha is not valid for its gate, else None."""
    kind, gate, sha = row["kind"], row["gate"], row["sha"]
    if row["red_first"]:  # a base before the fix: an ancestor of the merge, not a descendant
        base = merge or (head if merged_skipped else None)
        verdict = gitrepo.contains(repo.get(sha, base), sha, base) if base else None
    elif kind == "test-run" and gate == "built" or kind in ("blast-radius", "human-review") and gate == "merged":
        verdict = gitrepo.same(repo.get(head, sha), head, sha) if head and sha else True
        if kind == "blast-radius" and verdict is not True and merge and sha and head:
            verdict = _late_rating(repo, row, head, merge, sha, verdict)
    elif kind == "audit" and gate == "merged":
        verdict = gitrepo.contains(repo.get(sha, head), sha, head) if head and sha else True
    elif kind in RUNS and gate == "verified":
        base = merge or (head if merged_skipped else None)
        verdict = gitrepo.contains(repo.get(base, sha), base, sha) if base else None  # unplaceable until there is a merge
    else:
        verdict = True
    reason = {True: None, False: "wrong-sha", None: "sha-unverifiable"}[verdict]
    if reason is None and kind == "blast-radius" and gate == "merged":
        reason = _base_hides(repo, row, head, merge, sha)
    return reason


def _base_hides(repo, row, head, merge, sha):
    """`base-hides-pr` (or `sha-unverifiable`) for a `blast-radius` row whose recorded base leaves out commits of the PR
    (`gitrepo.hides_pr`), the check `trk blast-radius` made then made again with what the ticket knows now: a merge row
    that is a merge commit of the head, or a squash or rebase merge, gives the branch the PR merged into as it stood
    before (its first parent), which a rating recorded earlier could not see. None for a row that records no base (made before the check) or has none
    hidden."""
    base = (json.loads(row["detail"]) if row["detail"] else {}).get("base")
    if not base or not head or not sha:
        return None
    path = repo.get(head, sha, base, *([merge] if merge else []))
    if path is None:  # no checkout holds them (the rating named one with --repo): as before
        return None
    before = gitrepo.merge_parent(path, head, merge) if merge else None
    hides = gitrepo.hides_pr(path, base, sha, head, before)
    return "sha-unverifiable" if hides is None else "base-hides-pr" if hides else None


def _late_rating(repo, row, head, merge, sha, otherwise):
    """A `blast-radius` row at neither the head nor the merge sha (a repo that got its rules after the PR merged
    cannot be rated at either): valid, as one at the head would be, when `trk blast-radius` rated it against this
    same merge row (its detail's `merge`, set only when the merge was in the ticket then, so a rating made before
    the merge row never counts) and `gitrepo.late_rating` holds for its `base`. `otherwise` is the answer for the
    head check when not."""
    detail = json.loads(row["detail"]) if row["detail"] else {}
    anchor, base = detail.get("merge"), detail.get("base")
    if not anchor or not base:
        return otherwise
    placed = repo.get(merge, sha, head, base, anchor)
    if gitrepo.same(placed, anchor, merge) is not True:
        return otherwise
    held = gitrepo.late_rating(placed, head, merge, sha, base)
    return held if held is not False else otherwise


def summary(row):
    """The short form of a row a missing list or criterion status names; None for no row."""
    if row is None:
        return None
    return {"id": row["id"], "kind": row["kind"], "sha": row["sha"], "verdict": row["verdict"]}


def criterion_statuses(conn, item):
    """{n: {"status": pass|fail|none, "latest": summary}} from each criterion's latest counting row:
    gate `verified` for tickets, `close` for projects and programs."""
    weighing = weigh(conn, item)
    gate = "verified" if item["kind"] == "work_item" else "close"
    kinds = RUNS if gate == "verified" else (*RUNS, "judgment")
    result = {}
    for (n,) in conn.execute("SELECT n FROM criteria WHERE item_id = ?", (item["id"],)):
        latest = weighing.latest(kinds, gate, n)
        result[n] = {"status": latest["verdict"] if latest else "none", "latest": summary(latest)}
    return result


def to_json(row):
    """One evidence row as `trk evidence` lists it."""
    detail = row["detail"]
    return {
        "id": row["id"], "item": row["item_id"], "gate": row["gate"], "kind": row["kind"],
        "criterion": criteria.handle(row["criterion_n"]) if row["criterion_n"] else None,
        "criterion_version": row["criterion_version"], "ref": row["ref"], "sha": row["sha"],
        "verdict": row["verdict"], "note": row["note"], "detail": json.loads(detail) if detail else None,
        "actor": row["actor"], "session": row["session"], "seq": row["seq"], "created_at": row["created_at"],
        "counts": row["counts"], "reason": row["reason"], "red_first": row["red_first"],
    }


def verdict_label(row):
    """What a listing or page shows for a row's verdict: `failed as intended` for a red-first run, else the
    verdict itself (None when it has none)."""
    return RED_FIRST_LABEL if row["red_first"] else row["verdict"]


# Why a row does not count, in words: `trk evidence` prints it beside the reason code (`why` in JSON).
WHY = {"retracted": "a later retract row withdrew it",
       "not-weighed": "this gate does not weigh this kind",
       "old-version": "it judged an older version of its criterion",
       "before-reopen": "recorded before the item was reopened",
       "before-draft": "recorded before the spec went back to draft",
       "sha-unverifiable": "git cannot place its sha in the ticket's checkouts",
       "unpaired-red-first": "a red-first run counts only beside a passing run of the same criterion",
       "base-hides-pr": "its recorded base leaves out commits of the PR"}


def why_not(row, head):
    """The words for a row that does not count; `wrong-sha` names the head it missed and who moves it."""
    if row["counts"]:
        return None
    if row["reason"] == "wrong-sha":
        if row["kind"] == "test-run" and row["gate"] == "built" and head:
            return (f"its sha is not the head {head[:7]} (the latest pr row's sha): if {(row['sha'] or '?')[:7]} is "
                    "the real head, the builder records pr there")
        return f"its sha does not fit the {row['gate']} gate" + (f" at the head {head[:7]}" if head else "")
    return WHY.get(row["reason"])


def listing(conn, raw_id, gate=None):
    """`trk evidence`: (item id, its rows in seq order, each weighed), optionally one gate's rows only."""
    from . import items

    item = items.get(conn, raw_id)
    if gate is not None and gate not in gates_of(item):
        raise usage(f"--gate {gate} is not a gate of {item['id']} (its gates: {', '.join(gates_of(item)) or 'none'})")
    weighing = weigh(conn, item)
    return item["id"], [{**to_json(r), "why": why_not(r, weighing.head)}
                        for r in weighing.rows if gate is None or r["gate"] == gate]


def get(conn, evidence_id):
    """One evidence row, weighed with the rest of its item's."""
    from . import items

    found = conn.execute("SELECT item_id FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    rows = weigh(conn, items.get(conn, found["item_id"])).rows
    return to_json(next(r for r in rows if r["id"] == evidence_id))
