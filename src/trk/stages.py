"""Stages and gates: the one `gate()` function, ticket stage moves, and the schema 4 migration's steps.

A ticket's stage is stored, never derived, and moves only through the verbs the spec names; each
stage crossed gets one `stage.changed` row. `items` is imported inside the functions that need it.
"""

import json

from . import claims, clock, criteria, sections
from .errors import gate_unmet, refused, usage

STAGES = ("framed", "specd", "built", "merged", "verified")

# The written sections each ticket form must have non-empty to be Framed.
WRITTEN = {"spec_bound": ("what-to-build",), "self_contained": ("current-behaviour", "desired-behaviour")}


def is_ticket(row):
    return row["kind"] == "work_item" and row["form"] in WRITTEN


def _empty(conn, item_id, handle):
    return not (sections.body(conn, item_id, handle) or "").strip()


def require_ticket(row, verb):
    """A usage error unless the item is a ticket: `<id> is a <what>; <verb> tickets … only`."""
    from . import items

    if not is_ticket(row):
        what = items.FORM_OUT.get(row["form"]) or items.KIND_OUT[row["kind"]]
        raise usage(f"{row['id']} is a {what}; {verb} tickets (spec-bound, self-contained) only")


def _framed(conn, row):
    missing = [{"need": "section", "section": handle} for handle in WRITTEN[row["form"]]
               if _empty(conn, row["id"], handle)]
    if not criteria.live_count(conn, row["id"]):
        missing.append({"need": "criterion"})
    return missing


def _specd(conn, row):
    from . import items

    if row["form"] == "spec_bound":
        project = items.nearest_project(conn, row["id"])
        if project is None or project["spec_state"] != "accepted":
            return [{"need": "spec-accepted", "project": project["id"] if project is not None else None}]
        return []
    from . import evidence

    return [] if evidence.weigh(conn, row).latest(("judgment",), "specd") else [{"need": "judgment"}]


def _run_need(weighing, kinds, gate, need):
    """`need` when the group's latest counting row is not a pass, or `sha-unverifiable` naming the row
    git could not place when that row would otherwise decide."""
    from . import evidence

    latest = weighing.latest(kinds, gate)
    if latest is not None and latest["verdict"] == "pass":
        return []
    stuck = weighing.unverifiable(kinds, gate)
    if stuck is not None and (latest is None or stuck["seq"] > latest["seq"]):
        return [_stuck_need(weighing, stuck)]
    return [{"need": need, "latest": evidence.summary(latest)}]


def _stuck_need(weighing, row):
    """The need for a row only git's answer keeps from counting, naming the checkout tried."""
    return {"need": "sha-unverifiable", "evidence": row["id"], "sha": row["sha"], **weighing.tried(row["sha"])}


def _built(conn, row, weighing=None):
    from . import evidence

    weighing = weighing or evidence.weigh(conn, row)
    missing = [] if weighing.head else [{"need": "pr"}]
    return missing + _run_need(weighing, ("test-run",), "built", "test-run-pass")


def _merged(conn, row):
    """Built still holding at the head sha, a `merge` row and a `blast-radius` row at the head sha. A
    YELLOW or RED lane also needs its review (the Merged gate additions: an independent `audit`, then a
    verdict and a settlement per ledger row), and for RED a complete `review-brief` section and a `human-review`
    pass at the head sha."""
    from . import blast, evidence, ledger

    weighing = evidence.weigh(conn, row)
    missing = _built(conn, row, weighing)
    if weighing.merge is None:
        missing.append({"need": "merge"})
    if weighing.latest(("blast-radius",), "merged") is None:
        stuck = weighing.unverifiable(("blast-radius",), "merged")
        missing.append(_stuck_need(weighing, stuck) if stuck else {"need": "blast-radius"})
    else:
        lane = blast.lane(conn, row, weighing)
        if blast.needs_review(lane):
            missing += ledger.review_needs(conn, row, weighing)
        if lane == "red":
            missing += blast.brief_needs(conn, row, weighing)
            missing += _red_reviews(conn, row, weighing)
    return missing


def _red_reviews(conn, row, weighing):
    """One `human-review` need for each role that reviews a RED file whose holder has no counting pass at
    the head: only reviews recorded by the actor the role resolves to count for it."""
    from . import asks, blast, evidence

    detail = json.loads(weighing.latest(("blast-radius",), "merged")["detail"])
    missing = []
    for role in blast.red_roles(detail):
        holder = asks.resolve_role(conn, role, row["id"])
        theirs = evidence.Weighing([r for r in weighing.rows if r["kind"] != "human-review" or r["actor"] == holder],
                                   weighing.head, weighing.merge, weighing.repo)
        missing += [{**need, "role": role, "actor": holder}
                    for need in _run_need(theirs, ("human-review",), "merged", "human-review")]
    return missing


def _verified(conn, row):
    """Every live criterion's latest counting `verified` run passes, at a sha containing the merge sha
    (the head sha when Merged was skipped). With no merge sha to contain, `merge` is missing first; open
    children but asks are `open-children`, since reaching Verified closes it (open asks refuse that close: `open-asks`)."""
    from . import evidence, items

    weighing = evidence.weigh(conn, row)
    placed = weighing.merge is not None or (row["merged_skipped"] and weighing.head is not None)
    missing = [] if placed else [{"need": "merge"}]
    children = [c["id"] for c in items.children(conn, row["id"], open_only=True) if c["form"] != "ask"]
    if children:
        missing.append({"need": "open-children", "children": children})
    for criterion in criteria.live(conn, row["id"]):
        n, label = criterion["n"], criteria.handle(criterion["n"])
        latest = weighing.latest(evidence.RUNS, "verified", n)
        if latest is not None and latest["verdict"] == "pass":
            continue
        stuck = weighing.unverifiable(evidence.RUNS, "verified", n) if placed else None
        if stuck is not None and (latest is None or stuck["seq"] > latest["seq"]):
            missing.append({**_stuck_need(weighing, stuck), "criterion": label})
        else:
            missing.append({"need": "criterion-pass", "criterion": label, "version": criterion["version"],
                            "latest": evidence.summary(latest)})
    return missing


# The project sections `accept` needs non-empty; `acceptance-criteria` is generated, so a live criterion stands in.
SPEC_SECTIONS = ("problem", "solution", "user-stories", "implementation-decisions", "testing-decisions",
                 "out-of-scope")


def _accept(conn, row):
    """Every spec section written, a live criterion, and a counting `judgment` for `accept` (one recorded
    after the project's latest return to draft)."""
    from . import evidence

    missing = [{"need": "section", "section": handle} for handle in SPEC_SECTIONS if _empty(conn, row["id"], handle)]
    if not criteria.live_count(conn, row["id"]):
        missing.append({"need": "criterion"})
    if evidence.weigh(conn, row).latest(("judgment",), "accept") is None:
        missing.append({"need": "judgment"})
    return missing


def _criterion_passes(conn, row, weighing, gate, kinds):
    """`criterion` when no criterion is live, else `criterion-pass` for each live criterion whose latest
    counting row of `kinds` for `gate` is not a pass."""
    from . import evidence

    live = criteria.live(conn, row["id"])
    if not live:
        return [{"need": "criterion"}]
    missing = []
    for criterion in live:
        latest = weighing.latest(kinds, gate, criterion["n"])
        if latest is None or latest["verdict"] != "pass":
            need = {"need": "criterion-pass", "criterion": criteria.handle(criterion["n"]),
                    "version": criterion["version"], "latest": evidence.summary(latest)}
            turned_away = [{"id": r["id"], "kind": r["kind"], "reason": r["reason"]} for r in weighing.rows
                       if r["gate"] == gate and r["criterion_n"] == criterion["n"]
                       and r["reason"] in evidence.PROGRAM_CLOSE_REASONS]
            if turned_away:  # a program's close turned these rows away: say why, so the next row can be right
                need["uncounted"] = turned_away
            missing.append(need)
    return missing


def _close(conn, row):
    """Closing `done`: every child but an ask closed (Core's `open-children`; `close` refuses an open ask first, `open-asks`),
    then a decision's `resolution` written, or a passing `close` row for every live criterion of a
    project or program (a run, or a `judgment` citing what delivered it). A project also needs its spec
    accepted (`spec-accepted`, amended 2026-10-11, tr-afrvc). A program's rows are weighed harder
    (`evidence.PROGRAM_CLOSE_REASONS`, tr-43mme): a `judgment` never counts, so its criteria pass on runs."""
    from . import evidence, items, release

    open_children = [c["id"] for c in items.children(conn, row["id"], open_only=True) if c["form"] != "ask"]
    missing = [{"need": "open-children", "children": open_children}] if open_children else []
    if row["form"] == "decision":
        return missing + ([{"need": "resolution"}] if _empty(conn, row["id"], "resolution") else [])
    if row["kind"] == "project" and row["spec_state"] != "accepted":
        missing.append({"need": "spec-accepted", "project": row["id"]})
    weighing = evidence.weigh(conn, row)
    return (missing + _criterion_passes(conn, row, weighing, "close", (*evidence.RUNS, "judgment"))
            + release.pending(conn, row, weighing))


# Gate name -> check(conn, row) -> missing list. Later gates plug in here.
GATES = {"framed": _framed, "specd": _specd, "built": _built, "merged": _merged, "verified": _verified,
         "accept": _accept, "close": _close}

# Gates a ticket passes on its own whenever they hold from data alone, keyed by the stage they enter.
AUTO = {"specd": lambda row: row["form"] == "spec_bound"}


def gate(conn, row, gate_name):
    """The missing list for one gate on one item: empty when the gate holds. The only place a gate is evaluated."""
    return GATES[gate_name](conn, row)


def _following(stage):
    return STAGES[STAGES.index(stage) + 1] if stage != STAGES[-1] else None


def next_gate(row):
    """The gate an item faces next: a ticket's next stage, `accept` for a draft project, `close` for an
    accepted project, a program or a decision; None for closed items and asks."""
    if row["status"] == "closed" or row["form"] == "ask":
        return None
    if is_ticket(row):
        following = _following(row["stage"])
        return following if following in GATES else None
    if row["kind"] == "project" and row["spec_state"] == "draft":
        return "accept"
    return "close"


def gate_json(conn, row):
    """The item JSON's `gate`: the next gate and what it still lacks, from gate()."""
    following = next_gate(row)
    return {"next": following, "missing": gate(conn, row, following) if following in GATES else []}


def spec_handles(row, gate):
    """The sections a spec judgment judges: a project's spec at `accept`, a ticket's written sections at `specd`."""
    return SPEC_SECTIONS if gate == "accept" else WRITTEN.get(row["form"], ())


def spec_writers(conn, row, gate):
    """The sessions that wrote any of the sections a `gate` judgment judges, from the sections' own writes."""
    handles = set(spec_handles(row, gate))
    found = set()
    for write in conn.execute("SELECT change, session, data FROM journal WHERE item = ? AND session IS NOT NULL"
                              " AND change IN ('item.created', 'item.updated')", (row["id"],)):
        data = json.loads(write["data"])
        written = (data.get("sections") or () if write["change"] == "item.created"
                   else [f[len("section:"):] for f in data.get("fields", ()) if f.startswith("section:")])
        if handles & set(written):
            found.add(write["session"])
    return found


def check_spec_judge(tx, row, gate):
    """A spec judgment (`accept` or `specd`) comes from a session that wrote none of the sections it judges
    (rule `not-independent`): agents share an actor, so independence is by session. An agent needs a session
    for the check (rule `session-required`); a person at a terminal, with no session, may judge."""
    from . import actors

    if not tx.session:
        if actors.kind(tx.conn, tx.actor) == "agent":
            raise refused("session-required", f"a spec judgment by an agent needs a session (--session or "
                          "$TRK_SESSION): independence from the spec's writer is checked by session", item=row["id"])
        return
    if tx.session in spec_writers(tx.conn, row, gate):
        raise refused("not-independent", f"session {tx.session} wrote {row['id']}'s spec; its judgment needs a "
                      "session that wrote none of it (a separate Claude session, not a subagent)", item=row["id"])


def advance(tx, raw_id, judgment=None, outcome=None):
    """`advance`: move a ticket exactly one stage forward when that stage's gate holds (rule `gate-unmet`
    with the missing list otherwise, nothing written). `judgment` first writes the `judgment` row that
    judges a self-contained ticket at Framed complete, in the same transaction. Advancing into Verified
    takes `outcome` (a usage error without it) and closes the ticket `done` with it through Core's close,
    after the `stage.changed` row: open child asks are withdrawn, then `item.closed`, then the claim ends."""
    from . import evidence, items

    row = items.get(tx.conn, raw_id)
    item_id = row["id"]
    require_ticket(row, "advance moves")
    if row["status"] == "closed":
        raise refused("closed", f"{item_id} is closed", item=item_id)
    target = _following(row["stage"])
    if target is None:
        raise usage(f"{item_id} is at {row['stage']}, the last stage")
    if row["stage"] != "framed":  # framed to specd is a judgment, which the claimant need not hold
        claims.require_held(tx, row, "advance")
    if target == "verified":
        outcome = items.check_outcome(outcome, "advancing into verified closes the ticket done: it needs "
                                      "--outcome \"<one sentence>\"")
    elif outcome is not None:
        raise usage(f"--outcome goes with advancing into verified only; {item_id} is advancing into {target}")
    if judgment is not None:
        if row["form"] != "self_contained" or row["stage"] != "framed":
            raise usage("--judgment judges a self-contained ticket at framed complete; "
                        f"{item_id} is a {items.FORM_OUT[row['form']]} ticket at {row['stage']}")
        check_spec_judge(tx, row, "specd")
        evidence.record(tx, row, "judgment", "specd", verdict="pass", note=evidence.check_note(judgment, "--judgment"))
    missing = gate(tx.conn, row, target)
    if missing:
        raise gate_unmet(item_id, target, missing)
    tx.conn.execute("UPDATE items SET stage = ?, updated_at = ? WHERE id = ?", (target, clock.stamp(tx.now), item_id))
    journal_moves(tx, item_id, row["stage"], [target], "advance")
    if target == "verified":
        items.close_verified(tx, item_id, outcome)
    return item_id


def _check_stage(raw):
    """A stage name as given (usage error naming the stages otherwise)."""
    if raw not in STAGES:
        raise usage(f"the stage is one of {', '.join(STAGES)}, got {raw!r}")
    return raw


def _open_ticket(tx, raw_id, verb):
    from . import items

    row = items.get(tx.conn, raw_id)
    require_ticket(row, verb)
    if row["status"] == "closed":
        raise refused("closed", f"{row['id']} is closed", item=row["id"])
    return row


def skip(tx, raw_id, stage, reason):
    """`skip`: meet Merged by a `skip` row, for a no-repo-change ticket only. Allowed at Built when the head
    `pr` row's ref is `none`, no `merge` row counts and no counting `blast-radius` row is YELLOW or RED;
    rule `not-skippable` otherwise, and for any stage name but merged, so a skip never routes around review.
    Built must still hold at the head sha, as for advancing into Merged (`gate-unmet`, gate `merged`).
    Writes the `skip` row, sets `merged_skipped` and moves to Merged (`via: skip`)."""
    from . import blast, evidence

    reason = check_reason(reason, "skip", limit=evidence.NOTE_MAX)  # it is the skip row's note
    row = _open_ticket(tx, raw_id, "skip moves")
    item_id = row["id"]
    if stage != "merged":
        raise refused("not-skippable", f"only merged may be skipped, not {stage!r}", item=item_id)
    if row["stage"] != "built":
        raise refused("not-skippable", f"{item_id} is at {row['stage']}; merged is skipped from built only",
                      item=item_id)
    weighing = evidence.weigh(tx.conn, row)
    pr = weighing.latest(("pr",), "built")
    if pr is None or pr["ref"] != "none":
        raise refused("not-skippable", f"{item_id}'s head pr row has ref {pr['ref'] if pr else None!r}; only a ticket"
                      " that changes no repo (pr --ref none) may skip merged", item=item_id)
    if weighing.merge is not None:
        raise refused("not-skippable", f"{item_id} has a merge row; advance it through merged instead", item=item_id)
    lane = blast.lane(tx.conn, row, weighing)
    if blast.needs_review(lane):
        raise refused("not-skippable", f"{item_id} is rated {lane}; its review cannot be skipped", item=item_id)
    missing = _built(tx.conn, row, weighing)
    if missing:  # Merged's own first condition: Built still holds at the head sha
        raise gate_unmet(item_id, "merged", missing)
    evidence.record(tx, row, "skip", "merged", note=reason)
    tx.conn.execute("UPDATE items SET stage = 'merged', merged_skipped = 1, updated_at = ? WHERE id = ?",
                    (clock.stamp(tx.now), item_id))
    journal_moves(tx, item_id, "built", ["merged"], "skip", reason)
    return item_id


def check_reason(reason, verb, limit=None):
    """A stage move's one-line reason (usage error otherwise)."""
    from . import items

    return items.check_outcome(reason, f"{verb} needs --reason \"<one line>\"", limit or items.OUTCOME_MAX,
                               name="a reason")


def reopen_stage(tx, raw_id, stage, reason):
    """`reopen-stage`: move an open ticket back to an earlier stage (rule `not-backward` for the current
    or a later one), with the counting cut-off for every gate above it (evidence.weigh reads the move)."""
    reason = check_reason(reason, "reopen-stage")
    stage = _check_stage(stage)
    row = _open_ticket(tx, raw_id, "reopen-stage moves")
    if STAGES.index(stage) >= STAGES.index(row["stage"]):
        raise refused("not-backward", f"{row['id']} is at {row['stage']}; reopen-stage moves it to an earlier stage,"
                      f" not {stage}", item=row["id"])
    move_back(tx, row, stage, "reopen-stage", reason)
    return row["id"]


def move_back(tx, row, stage, via, reason):
    """Set a ticket back at an earlier `stage`, clearing `merged_skipped` at Built or earlier, and journal
    one `stage.changed` row per stage crossed."""
    skipped = row["merged_skipped"] and STAGES.index(stage) >= STAGES.index("merged")
    tx.conn.execute("UPDATE items SET stage = ?, merged_skipped = ?, updated_at = ? WHERE id = ?",
                    (stage, int(bool(skipped)), clock.stamp(tx.now), row["id"]))
    crossed = list(reversed(STAGES[STAGES.index(stage):STAGES.index(row["stage"])]))
    journal_moves(tx, row["id"], row["stage"], crossed, via, reason)
    if STAGES.index(stage) <= STAGES.index("specd"):  # a claim held now counts as a builder's: refresh the byline
        from . import items

        items.refresh_builder_asks(tx, row["id"])


def closes_gated(row):
    """Whether closing this item `done` checks gate `close`: projects, programs and decisions."""
    return row["kind"] in ("program", "project") or row["form"] == "decision"


def check_accept(conn, row):
    """Gate `accept` for `spec accept` (rule `gate-unmet` otherwise); returns the `spec.changed` data it
    adds: the acceptor (the deciding judgment's actor) and that judgment's evidence id."""
    from . import evidence

    missing = gate(conn, row, "accept")
    if missing:
        raise gate_unmet(row["id"], "accept", missing)
    judgment = evidence.weigh(conn, row).latest(("judgment",), "accept")
    return {"acceptor": judgment["actor"], "judgment": judgment["id"]}


def _project_tickets(conn, project_id, stage):
    """The open spec-bound tickets at `stage` whose nearest project is this one."""
    from . import items

    return [row for row, _ in items.outline(conn, project_id)[1:]
            if row["form"] == "spec_bound" and row["stage"] == stage
            and items.nearest_project(conn, row["id"])["id"] == project_id]


def follow_spec(tx, project_id, state, reason=None):
    """After a spec state change: acceptance auto-advances the project's Framed spec-bound tickets
    (`via: spec-accept`); a return to draft moves its Spec'd ones back to Framed (`via: spec-draft`,
    no evidence cut-off). Tickets at Built or later keep their stage."""
    now = clock.stamp(tx.now)
    if state == "accepted":
        for row in _project_tickets(tx.conn, project_id, "framed"):
            path = _auto_path(tx.conn, row, "framed")
            if path:
                tx.conn.execute("UPDATE items SET stage = ?, updated_at = ? WHERE id = ?", (path[-1], now, row["id"]))
                journal_moves(tx, row["id"], "framed", path, "spec-accept")
    else:
        for row in _project_tickets(tx.conn, project_id, "specd"):
            tx.conn.execute("UPDATE items SET stage = 'framed', updated_at = ? WHERE id = ?", (now, row["id"]))
            journal_moves(tx, row["id"], "specd", ["framed"], "spec-draft", reason)


def _fresh(conn, item_id):
    from . import items

    return items.get(conn, item_id)


def set_born(conn, row):
    """Set the stage a ticket starts at: Framed, then every stage it reaches by auto-advance; returns the path."""
    path = ["framed", *_auto_path(conn, row, "framed")]
    conn.execute("UPDATE items SET stage = ? WHERE id = ?", (path[-1], row["id"]))
    return path


def _auto_path(conn, row, start):
    """The stages a ticket at `start` reaches by auto-advance alone, in order."""
    path, stage = [], start
    while True:
        following = _following(stage)
        if following not in AUTO or not AUTO[following](row) or gate(conn, row, following):
            return path
        path.append(following)
        stage = following


def _creation_hint(row):
    """The flags a ticket of this form needs at creation, with an example `trk new`."""
    from . import items

    form = items.FORM_OUT[row["form"]]
    sections_flags = " ".join(f"--section {handle}=@FILE" for handle in WRITTEN[row["form"]])
    example_sections = " ".join(f"--section {handle}=@{handle}.md" for handle in WRITTEN[row["form"]])
    return (f"a {form} ticket is created with {sections_flags} (or =- for stdin) and at least one "
            f"--criterion \"<one line>\" (repeatable), e.g. trk new work-item \"<title>\" --form {form} "
            f"--parent <id> {example_sections} --criterion \"<what done means>\"")


def birth(tx, item_id):
    """Check `framed` on a just-inserted ticket (rule `gate-unmet`, rolled back with the rest) and set
    the stage it is born at; returns the stages it entered, in order, for journal_moves."""
    row = _fresh(tx.conn, item_id)
    missing = gate(tx.conn, row, "framed")
    if missing:
        raise gate_unmet(None, "framed", missing, _creation_hint(row))  # nothing is written, so no id is named
    return set_born(tx.conn, row)


def journal_moves(tx, item_id, old, path, via, reason=None):
    """One `stage.changed` row per stage crossed, from `old` through each stage of `path`."""
    from . import items

    fields = items.feed_fields(tx.conn, item_id)
    for new in path:
        data = {"old": old, "new": new, "via": via}
        if reason is not None:
            data["reason"] = reason
        tx.journal("stage.changed", fields, data=data)
        old = new

