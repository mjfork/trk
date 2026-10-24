"""Evidence: append-only proof rows toward one gate, their retraction, and weighing which rows count.

Every evidence row is written by `record`, whichever verb writes it, with its `evidence.added` feed row.
`weigh` is the one place that decides whether a row counts and why not; the gates in `stages` and the
criterion statuses in `criteria` read only its answer. `items` is imported inside the functions that need it.
"""

import json
import re
import secrets

from . import clock, criteria, gitrepo, ids
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

# Kinds a closed item still takes (besides a retraction).
ON_CLOSED = ("review", "doc")

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
    return evidence_id


def add(tx, raw_id, kind, gate, *, criterion=None, ref=None, sha=None, verdict=None, note=None, direct=False):
    """`evidence add`: one row of a kind agents record, checked against the kinds table; returns its id.
    A `human-review` pass also answers the ticket's open RED-read ask when it counts (`blast.answer_red_ask`).
    `direct` (only `blast.review_record` passes it) allows the reserved `direct:` ref."""
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
    fields = {"ref": ref, "sha": sha, "criterion": criterion_row, "detail": None, "verdict": verdict}
    _check_table(row, kind, gate, fields)
    if VERDICT[kind] == "pass":
        verdict = "pass"
    evidence_id = record(tx, row, kind, gate, criterion=criterion_row, ref=ref, sha=sha, verdict=verdict, note=note)
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
        return self._last(kinds, gate, criterion_n, lambda row: row["counts"])

    def unverifiable(self, kinds, gate, criterion_n=None):
        """The group's latest row when only git's answer keeps it from counting (`sha-unverifiable`), else None."""
        row = self._last(kinds, gate, criterion_n, lambda row: row["reason"] in (None, "wrong-sha", "sha-unverifiable"))
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
    drafted = _latest_draft(conn, item) if any(r["gate"] == "accept" for r in rows) else None
    cut = _cut_offs(conn, item) if rows else {}
    for r in rows:
        if r["id"] in retracted:
            r["reason"] = "retracted"
        elif r["kind"] in NOT_WEIGHED:
            r["reason"] = "not-weighed"
        elif r["criterion_n"] is not None and r["criterion_version"] != versions.get(r["criterion_n"]):
            r["reason"] = "old-version"
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
        if r["reason"] is None:
            r["reason"] = _sha_reason(repo, r, head, merge, bool(item["merged_skipped"]))
        r["counts"] = r["reason"] is None
    return Weighing(rows, head, merge, repo)


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
    if kind == "test-run" and gate == "built" or kind in ("blast-radius", "human-review") and gate == "merged":
        verdict = gitrepo.same(repo.get(head, sha), head, sha) if head and sha else True
    elif kind == "audit" and gate == "merged":
        verdict = gitrepo.contains(repo.get(sha, head), sha, head) if head and sha else True
    elif kind in RUNS and gate == "verified":
        base = merge or (head if merged_skipped else None)
        verdict = gitrepo.contains(repo.get(base, sha), base, sha) if base else None  # unplaceable until there is a merge
    else:
        verdict = True
    return {True: None, False: "wrong-sha", None: "sha-unverifiable"}[verdict]


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
        "counts": row["counts"], "reason": row["reason"],
    }


def listing(conn, raw_id, gate=None):
    """`trk evidence`: (item id, its rows in seq order, each weighed), optionally one gate's rows only."""
    from . import items

    item = items.get(conn, raw_id)
    if gate is not None and gate not in gates_of(item):
        raise usage(f"--gate {gate} is not a gate of {item['id']} (its gates: {', '.join(gates_of(item)) or 'none'})")
    rows = weigh(conn, item).rows
    return item["id"], [to_json(r) for r in rows if gate is None or r["gate"] == gate]


def get(conn, evidence_id):
    """One evidence row, weighed with the rest of its item's."""
    from . import items

    found = conn.execute("SELECT item_id FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    rows = weigh(conn, items.get(conn, found["item_id"])).rows
    return to_json(next(r for r in rows if r["id"] == evidence_id))
