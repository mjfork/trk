"""Asks: leaf work items that wait on a role, opened under any open item.

An ask stores only its role. `resolve` turns the role into an actor whenever it is read, so routing,
`show`, item JSON and feed rows all name whoever holds it now.
"""

import json
import re
from datetime import UTC

from . import actors, claims, clock, items, roles, sections  # items imports asks back; all bind modules only
from .errors import refused, usage

PARENT = roles.PARENT

EXCERPT_MAX = 240

_FENCE = re.compile(r"^\s*(```|~~~)")
_LINE_MARKERS = re.compile(r"^\s*(?:(?:>|#{1,6}(?=\s)|[-*+](?=\s)|\d+[.)](?=\s))\s*)*")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_EMPHASIS = re.compile(r"[*_`]")
_SPACE = re.compile(r"\s+")


def resolve_role(conn, role, blocked_id):
    """The actor `role` names now, for an ask blocking `blocked_id`; None for a role that does not exist.

    A function role resolves through `roles`. `parent` resolves to the effective owner of the blocked
    item's parent, or to the blocked item's own owner when it is top-level.
    """
    if role != PARENT:
        return roles.holder(conn, role)
    blocked = items.get(conn, blocked_id)
    return items.hierarchy(conn, blocked["parent_id"] or blocked["id"])["effective_owner"]


def resolve(conn, ask):
    """The actor an ask row's role resolves to now."""
    return resolve_role(conn, ask["addressee_role"], ask["parent_id"])


def fields(conn, row):
    """The keys item JSON adds for an ask: `rev` and `awaiting` on every ask, and `red_read` on an open RED-read one."""
    from . import blast

    addressee = resolve(conn, row)
    replied = replies(conn, row)
    found = {"addressee_role": row["addressee_role"], "addressee": addressee,
             "addressee_kind": actors.kind(conn, addressee), "asker": row["asker"], "ledger": _tied(conn, row["id"]),
             "replies": replied, "rev": rev(conn, row), "awaiting": awaiting(row, replied)}
    red_read = blast.red_read_json(conn, row)
    if red_read is not None:
        found["red_read"] = red_read
    return found


def rev(conn, row):
    """The latest journal seq touching the ask: every row whose item is the ask, `ask.replied` rows included (a
    reply leaves `updated_at` alone). An open RED-read ask also counts its ticket's rows, since the ask shows the
    ticket's rating and brief. A client sends it back with an action to have a stale one refused."""
    from . import blast

    ids = [row["id"]]
    if row["status"] == "open" and row["title"].startswith(blast.RED_ASK_TITLE):
        ids.append(row["parent_id"])
    marks = ",".join("?" * len(ids))
    return conn.execute(f"SELECT COALESCE(MAX(seq), 0) FROM journal WHERE item IN ({marks})", ids).fetchone()[0]


def awaiting(row, replied):
    """Who must act next: `asker` when the latest reply since the opening is by anyone but the asker (the
    asker reads it, then confirms or follows up), else `addressee`; None once the ask is closed."""
    if row["status"] != "open":
        return None
    return "asker" if replied and replied[-1]["actor"] != row["asker"] else "addressee"


def _tied(conn, ask_id):
    """The `ledger-<n>` row an ask is tied to (`ask --ledger`), or None."""
    from . import ledger  # a gates module: loaded only when an ask is read

    return ledger.tied_to(conn, ask_id)


def open_under(conn, item_id):
    """An item's open child asks in rank order, as `show` lists them."""
    return [{"id": r["id"], "title": r["title"], "addressee_role": r["addressee_role"], "addressee": resolve(conn, r),
             "urgent": bool(r["urgent"])} for r in _open_rows(conn, item_id)]


def _open_rows(conn, item_id):
    """An item's open child ask rows in rank order."""
    return conn.execute(
        "SELECT * FROM items WHERE parent_id = ? AND form = 'ask' AND status = 'open' ORDER BY rank", (item_id,)
    ).fetchall()


def check_addressable(conn, role, blocked_id, asker, *, item):
    """Refuse a role an ask under `blocked_id` cannot be addressed to: no such role (rule
    `unknown-role`), or one resolving to the asker (rule `self-addressed`)."""
    addressee = resolve_role(conn, role, blocked_id)
    if addressee is None:
        raise refused("unknown-role", f"{role!r} is not a function role or {PARENT!r}; see `trk roles`", item=item)
    if addressee == asker:
        raise refused("self-addressed", f"{role} resolves to {asker}, the asker: that is a to-do, not an ask",
                      item=item)


OPTION_LABEL_MAX = 200  # leaves room for "Option N: " inside an outcome's 300


def options_body(labels, recommended):
    """The `options` section's JSON for an ask given `labels` (in order, numbered from 1) and the
    1-based `recommended` option or None; None when there are no labels. A usage error for an empty or
    multi-line label, or a `--recommend` that names no option."""
    labels = [label.strip() for label in labels or ()]
    if not labels:
        if recommended is not None:
            raise usage("--recommend names one of the --option labels; give at least one --option")
        return None
    for label in labels:
        if not label or "\n" in label or "\r" in label:
            raise usage("an --option label is one non-empty line")
        if len(label) > OPTION_LABEL_MAX:
            raise usage(f"an --option label is at most {OPTION_LABEL_MAX} chars (got {len(label)})")
    if recommended is not None and not 1 <= recommended <= len(labels):
        raise usage(f"--recommend {recommended} is out of range: {len(labels)} option(s) given, numbered from 1")
    return json.dumps({"options": [{"n": n, "label": label} for n, label in enumerate(labels, 1)],
                       "recommended": recommended}, ensure_ascii=False)


def options(conn, ask_id):
    """The ask's parsed `options` section (`{"options": [{n, label}…], "recommended": N|None}`), or None."""
    text = sections.body(conn, ask_id, "options")
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None  # a hand-written section that is not ours: no one-tap answer, free text still works
    if not isinstance(parsed, dict) or not isinstance(parsed.get("options"), list):
        return None
    return parsed


def open_ask(tx, parent, role, title, question, *, urgent=False, where="last", anchor=None, ledger=None,
             option_labels=(), recommended=None):
    """Open an ask under `parent`, addressed to `role`, with the writing actor as asker. Returns its id.

    The question is required and non-empty (usage error). Refusals run in the spec's order: unknown
    parent (exit 3), `parent-closed`, `ask-leaf`, `unknown-role`, then `self-addressed`. `ledger` ties
    the ask to that ledger row of its parent ticket (usage error when the parent has no such row).
    `option_labels` and `recommended` store an `options` section beside the question (usage error for a
    `recommended` that names none). Journals one `ask.opened` row instead of `item.created`.
    """
    if question is None or not question.strip():
        raise usage("an ask needs a non-empty question: --question @file or --question -")
    options_text = options_body(option_labels, recommended)
    title = items.check_title(title)
    sections.check_body("question", question)
    conn = tx.conn
    parent_row = items.get(conn, parent)
    items.check_parent_open(parent_row)
    items.check_not_ask(parent_row)
    check_addressable(conn, role, parent_row["id"], tx.actor, item=parent_row["id"])
    if ledger is not None:
        from . import ledger as ledger_rows  # a gates module, loaded only for --ledger; `ledger` is the row given

        ledger_n = ledger_rows.tie_target(conn, parent_row, ledger)
    bodies = [("question", question)] + ([("options", options_text)] if options_text else [])
    item_id, rank = items.insert(tx, "work_item", title, form="ask", parent_id=parent_row["id"], where=where,
                                 anchor=anchor, urgent=urgent, section_bodies=bodies,
                                 addressee_role=role, asker=tx.actor)
    if ledger is not None:
        ledger_rows.tie(conn, parent_row["id"], ledger_n, item_id)
    addressee = resolve(conn, items.get(conn, item_id))  # the row names what every later read will
    tx.journal(
        "ask.opened",
        items.feed_fields(conn, item_id),
        status_new="open",
        addressee=addressee,
        data=_opened_data(conn, items.get(conn, item_id), addressee),
    )
    return item_id


def _opened_data(conn, row, addressee):
    """`ask.opened` data, which `ask.reopened` repeats: item.created's keys at an ask's fixed values, the
    section handles present, and the ask's fields with the question's excerpt."""
    handles = [entry["handle"] for entry in sections.listing(conn, row, bodies=False) if entry["present"]]
    return {**items.created_data(rank=row["rank"], urgent=row["urgent"], sections=handles),
            "addressee_role": row["addressee_role"], "addressee_kind": actors.kind(conn, addressee),
            "question": excerpt(sections.body(conn, row["id"], "question")), "asker": row["asker"],
            "ledger": _tied(conn, row["id"])}


def open_row(conn, raw_id):
    """The row of an open ask, for a verb that acts on one: unknown ids exit 3; anything else that is
    not an ask is refused (rule `not-ask`), and so is a closed ask (rule `closed`)."""
    row = items.get(conn, raw_id)
    if row["form"] != "ask":
        raise refused("not-ask", f"{row['id']} is not an ask", item=row["id"])
    if row["status"] == "closed":
        raise refused("closed", f"{row['id']} is closed", item=row["id"])
    return row


# Who may act on an ask, by part, as a refusal names them.
_PART_NAMES = {"asker": "the asker", "owner": "the parent's owner", "addressee": "the addressee", "human": "any human"}


def check_permission(conn, ask, actor, verb, parts):
    """Refuse `actor` unless it plays one of `parts` for this ask (rule `ask-permission`).

    Parts: `asker`, `owner` (the parent's effective owner), `addressee` (the actor the role resolves
    to now) and `human` (any human actor). The refusal names who may.
    """
    holders = {"asker": ask["asker"], "owner": items.hierarchy(conn, ask["parent_id"])["effective_owner"],
               "addressee": resolve(conn, ask)}
    if any(holders.get(part) == actor for part in parts):
        return
    if "human" in parts and actors.kind(conn, actor) == "human":
        return
    allowed = [_PART_NAMES[part] + (f" ({holders[part]})" if part in holders else "") for part in parts]
    who = allowed[0] if len(allowed) == 1 else f"{', '.join(allowed[:-1])} or {allowed[-1]}"
    raise refused("ask-permission", f"{actor} may not {verb} {ask['id']}; only {who} may", item=ask["id"])


def readdress(tx, raw_id, role):
    """Change an open ask's role. Returns its id.

    Allowed for the asker, the parent's owner, the resolved addressee or any human (rule
    `ask-permission`, checked first). `unknown-role` and `self-addressed` apply as on opening. The
    same role writes nothing; any other role writes one `ask.readdressed` row (cause `readdress`),
    even when both roles resolve to the same actor.
    """
    conn = tx.conn
    row = open_row(conn, raw_id)
    check_permission(conn, row, tx.actor, "readdress", ("asker", "owner", "addressee", "human"))
    if role == row["addressee_role"]:
        return row["id"]
    check_addressable(conn, role, row["parent_id"], row["asker"], item=row["id"])
    old = resolve(conn, row)
    conn.execute("UPDATE items SET addressee_role = ?, updated_at = ? WHERE id = ?",
                 (role, clock.stamp(tx.now), row["id"]))
    _journal_readdressed(tx, items.get(conn, row["id"]), old_addressee=old, old_role=row["addressee_role"],
                         cause="readdress")
    return row["id"]


# A withdrawal's outcome is this prefix and the reason.
WITHDRAWN = "withdrawn: "

# The resolutions `close` accepts on an ask: both are withdrawals.
_CLOSE_AS_WITHDRAWAL = ("duplicate", "superseded")
_ANSWERED_VIA = {"cli": "via the CLI", "evidence": "through a recorded review"}  # ask.answered's `via`, told to replies


def answer(tx, raw_id, outcome, body):
    """Answer an open ask: set its `answer` section and close it `done`, in one transaction. Returns its id.

    The answer is required and non-empty (usage error). Allowed for the resolved addressee or any
    human (rule `ask-permission`). Journals one `ask.answered` row instead of `item.closed`, with
    `proxy` true when the answerer is not the addressee.
    """
    outcome = items.check_outcome(outcome, "answer needs --outcome \"<one-line gist>\"")
    if body is None or not body.strip():
        raise usage("an answer needs a non-empty body: --answer @file or --answer -")
    sections.check_body("answer", body)
    conn = tx.conn
    row = open_row(conn, raw_id)
    item_id = row["id"]
    check_permission(conn, row, tx.actor, "answer", ("addressee", "human"))
    _close_answered(tx, row, outcome, body, proxy=tx.actor != resolve(conn, row), source={"via": "cli"})
    return item_id


def answer_option(tx, raw_id, number, body):
    """Answer an open ask with option `number`: the outcome is `Option N: <label>`, the body `body` when given,
    else the label. Refused when the ask has no `options` section (rule `no-options`) or `number` is not
    one of its numbers (rule `bad-option`); otherwise as `answer`."""
    conn = tx.conn
    row = open_row(conn, raw_id)
    listed = options(conn, row["id"])
    if listed is None:
        raise refused("no-options", f"{row['id']} has no options; answer with --outcome and --answer", item=row["id"])
    chosen = {o.get("n"): o.get("label") for o in listed["options"] if isinstance(o, dict)}
    if number not in chosen:
        raise refused("bad-option", f"{row['id']} has options {', '.join(map(str, sorted(chosen)))}, not {number}",
                      item=row["id"])
    label = chosen[number]
    outcome = items.check_outcome(f"Option {number}: {label}", "an option needs a label")
    if body is not None and not body.strip():
        raise usage("an answer needs a non-empty body: --answer @file or --answer -")
    text = label if body is None else body
    sections.check_body("answer", text)
    check_permission(conn, row, tx.actor, "answer", ("addressee", "human"))
    _close_answered(tx, row, outcome, text, proxy=tx.actor != resolve(conn, row), source={"via": "cli"})
    return row["id"]


def answer_by_evidence(tx, row, outcome, body, evidence_id):
    """The proxy answer by evidence (spec 03, Amendments to Asks and owners): a `human-review` pass
    answers a ticket's open RED-read ask `row` whoever records it, a named exception to `answer`'s
    permission, since the approval itself happened on GitHub. `ask.answered` says `proxy: true`,
    `via: evidence` and the evidence id."""
    _close_answered(tx, row, outcome, body, proxy=True, source={"via": "evidence", "evidence": evidence_id})


def reply_by_comment(tx, row, actor, body, *, slug, comment_id):
    """The comment pull's reply (spec 04, Comments as answers): a person's comment on the open ask `row` is
    a reply the asker confirms or follows up, not yet its answer. The commenter `actor` must be one who
    could answer it (the addressee or any human, rule `ask-permission`) with this text (`answer`'s body
    limit), so `answer --from-comment` can take it later. Journals `ask.replied` as `actor`, inside the
    publisher's transaction; the ask stays open."""
    sections.check_body("answer", body)
    check_permission(tx.conn, row, actor, "answer", ("addressee", "human"))
    tx.journal("ask.replied", items.feed_fields(tx.conn, row["id"]), addressee=resolve(tx.conn, row),
               data={"slug": slug, "comment_id": comment_id, "excerpt": excerpt(body)}, actor=actor)


# A reply with no page behind it (`trk reply`) is named by its journal row. contract/changes.schema.json
# requires a non-empty `slug` on `ask.replied`, so such a row says this one; the `reply-` id is what marks it.
REPLY_ID = re.compile(r"reply-([1-9][0-9]*)\Z")
NO_PAGE = "cli"


def reply(tx, raw_id, body):
    """Record a person's reply to an open ask with no page behind it (`trk reply`, tr-zb4rn): the same
    `ask.replied` row a page comment writes, as the writer, with the comment id `reply-<journal seq>`, no
    page (`slug` is `NO_PAGE`) and the text itself in `body`, so `answer --from-comment` can take it later.
    The ask stays open. Allowed for the asker (who may answer back, though never as the answer), the addressee or any human (rule
    `ask-permission`), with `answer`'s body limit. Returns the ask's id."""
    if body is None or not body.strip():
        raise usage("a reply needs a non-empty body: --body @file or --body -")
    sections.check_body("answer", body)
    conn = tx.conn
    row = open_row(conn, raw_id)
    check_permission(conn, row, tx.actor, "reply to", ("asker", "addressee", "human"))
    # The write lock is held, so the next AUTOINCREMENT value is this row's seq.
    last = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'journal'").fetchone()
    comment_id = f"reply-{(last[0] if last else 0) + 1}"
    seq = tx.journal("ask.replied", items.feed_fields(conn, row["id"]), addressee=resolve(conn, row),
                     data={"slug": NO_PAGE, "comment_id": comment_id, "excerpt": excerpt(body), "body": body})
    assert comment_id == f"reply-{seq}", f"journal seq {seq} is not the predicted {comment_id}"
    return row["id"]


def _replied_since_opening(conn, item_id):
    """The ask's `ask.replied` journal rows since its latest opening (or reopening), oldest first."""
    opened = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM journal WHERE item = ? "
                          "AND change IN ('ask.opened', 'ask.reopened')", (item_id,)).fetchone()[0]
    return conn.execute("SELECT actor, at, data FROM journal WHERE item = ? AND change = 'ask.replied' AND seq > ?"
                        " ORDER BY seq", (item_id, opened)).fetchall()


def replies(conn, row):
    """An ask's replies since its latest opening, oldest first: {comment, actor, at, excerpt, body}; `body` is
    the full text where trk holds it (`trk reply`), None for a page comment (the page holds it)."""
    return [{"comment": data["comment_id"], "actor": r["actor"], "at": r["at"], "excerpt": data["excerpt"],
             "body": data.get("body")}
            for r in _replied_since_opening(conn, row["id"]) for data in (json.loads(r["data"]),)]


def _tell_reply_threads(tx, item_id, note, *, skip=None):
    """Queue `note` on each reply thread since the ask's latest opening (but `skip`, a comment id), once per
    comment, unresolved: an ask closed any way but by confirming a thread's reply tells that thread how
    (tr-3vgrb ledger-6), through the page outbox in the close's own transaction."""
    told = {skip}
    for r in _replied_since_opening(tx.conn, item_id):
        data = json.loads(r["data"])
        if data["comment_id"] in told:
            continue
        told.add(data["comment_id"])
        if REPLY_ID.match(data["comment_id"]):
            continue  # a reply made with `trk reply` has no page thread to tell
        tx.conn.execute("INSERT INTO page_replies (slug, comment_id, body, resolve, created_at) VALUES (?, ?, ?, 0, ?)",
                        (data["slug"], data["comment_id"], note, clock.stamp(tx.now)))


def answer_from_comment(tx, raw_id, comment_id, outcome):
    """Confirm a reply as an open ask's answer (`trk answer --from-comment`): the reply's text is the
    answer and its writer the answerer. Allowed for the asker, the parent's owner or any human (rule
    `ask-permission`); the reply must be one recorded on this ask (rule `no-reply`), its writer must still be
    one who may answer it, and it is never the asker's own (rule `own-reply`). A page comment's `ask.answered`
    has the commenter as its actor and says `via: page`, the slug, the comment id and `confirmed_by`, and the
    commenter's thread is told, unresolved, through the page outbox. A `reply-<n>` made with `trk reply` says
    `via: cli`, the comment id and `confirmed_by`, and has no thread to tell. Returns the ask's id."""
    outcome = items.check_outcome(outcome, "answer needs --outcome \"<one-line gist>\"")
    conn = tx.conn
    row = open_row(conn, raw_id)
    check_permission(conn, row, tx.actor, "confirm an answer to", ("asker", "owner", "human"))
    replied = conn.execute("SELECT actor, data FROM journal WHERE item = ? AND change = 'ask.replied' "
                           "AND json_extract(data, '$.comment_id') = ? ORDER BY seq DESC LIMIT 1",
                           (row["id"], comment_id)).fetchone()
    slug = None
    if REPLY_ID.match(comment_id):
        body = json.loads(replied["data"]).get("body") if replied is not None else None
    else:
        page = conn.execute("SELECT slug, body FROM page_comments WHERE comment_id = ? AND ask_id = ? "
                            "AND disposition = 'reply'", (comment_id, row["id"])).fetchone()
        slug, body = (page["slug"], page["body"]) if page is not None and replied is not None else (None, None)
    if not (body or "").strip():
        raise refused("no-reply", f"{comment_id} is no reply recorded on {row['id']}; see `trk show {row['id']}`",
                      item=row["id"])
    commenter = replied["actor"]
    if commenter == row["asker"]:
        raise refused("own-reply", f"{comment_id} is the asker's own reply, so it cannot be its answer;"
                      f" a reply from someone who may answer {row['id']} can", item=row["id"])
    sections.check_body("answer", body)
    check_permission(conn, row, commenter, "answer", ("addressee", "human"))
    addressee = resolve(conn, row)
    answerer = actors.display(conn, commenter)
    if slug is None:
        source = {"via": "cli", "comment_id": comment_id, "confirmed_by": tx.actor}
    else:
        source = {"via": "page", "slug": slug, "comment_id": comment_id, "confirmed_by": tx.actor}
    _close_answered(tx, row, outcome, body, proxy=commenter != addressee, actor=commenter, source=source)
    if slug is not None:
        conn.execute("INSERT INTO page_replies (slug, comment_id, body, resolve, created_at) VALUES (?, ?, ?, 0, ?)",
                     (slug, comment_id, f"Recorded as the answer to {row['title']}, by {answerer} "
                      f"(for {actors.display(conn, addressee)}). Ask closed.", clock.stamp(tx.now)))
    return row["id"]


def _close_answered(tx, row, outcome, body, *, proxy, source, actor=None):
    """Set an open ask's `answer`, close it `done` and journal its `ask.answered` row (as `actor`, by
    default the writer's), whose data ends with `proxy` and the `source` keys (`via`, and what that
    source names)."""
    conn = tx.conn
    sections.put(conn, row["id"], "answer", body, tx.now)
    items.mark_closed(tx, row["id"], "done", outcome)
    data = {"resolution": "done", "outcome": outcome, **_closing_facts(tx, row), "answer": excerpt(body),
            "proxy": proxy, **source}
    tx.journal("ask.answered", items.feed_fields(conn, row["id"]), status_old="open", status_new="closed",
               addressee=resolve(conn, row), data=data, actor=actor)
    answerer = actors.display(conn, actor or tx.actor)
    if "comment_id" in source:
        note, skip = f"Ask closed: {answerer}'s reply was recorded as the answer.", source["comment_id"]
    else:
        note, skip = f"Ask closed: answered by {answerer} {_ANSWERED_VIA[source['via']]}.", None
    _tell_reply_threads(tx, row["id"], note, skip=skip)


def withdraw(tx, raw_id, reason):
    """Withdraw an open ask the asker no longer needs: close it `wont-do` with outcome
    `withdrawn: <reason>`. Allowed for the asker or the parent's owner (rule `ask-permission`).
    Journals one `ask.withdrawn` row carrying the reason. Returns its id.
    """
    reason = items.check_outcome(reason, "withdraw needs --reason \"<one line>\"",
                                 limit=items.OUTCOME_MAX - len(WITHDRAWN), name="a reason")
    row = open_row(tx.conn, raw_id)
    check_withdraw(tx, row)
    _withdraw_row(tx, row, WITHDRAWN + reason, reason)
    return row["id"]


def _withdraw_row(tx, row, outcome, reason):
    """Close an open ask `row` `wont-do` with `outcome` and journal its `ask.withdrawn`."""
    items.mark_closed(tx, row["id"], "wont-do", outcome)
    journal_withdrawn(tx, row, {"resolution": "wont-do", "outcome": outcome}, reason=reason)


def check_close(row, resolution):
    """`close` on an ask: only `duplicate` and `superseded`, both withdrawals; any other resolution is a
    usage error pointing at `answer` or `withdraw`."""
    if resolution not in _CLOSE_AS_WITHDRAWAL:
        raise usage(f"{row['id']} is an ask: `trk answer {row['id']} --outcome … --answer @file|-` answers it, and"
                    f" `trk withdraw {row['id']} --reason …` withdraws it")


def check_withdraw(tx, row):
    """Refuse anyone but the asker or the parent's owner (rule `ask-permission`): `withdraw`, and `close`
    as `duplicate` or `superseded`, which are withdrawals too."""
    check_permission(tx.conn, row, tx.actor, "withdraw", ("asker", "owner"))


def journal_withdrawn(tx, row, data, *, reason):
    """The `ask.withdrawn` row for an ask just closed: `data` carries resolution, outcome and any
    `by`/`of`; `reason` is the closer's, `parent_closed` when the parent's close withdrew it, or None for
    duplicate and superseded."""
    conn = tx.conn
    tx.journal("ask.withdrawn", items.feed_fields(conn, row["id"]), status_old="open", status_new="closed",
               addressee=resolve(conn, row), data={**data, **_closing_facts(tx, row), "reason": reason})
    outcome = data["outcome"].rstrip(".")
    if "of" in data:
        note = f"Ask closed as a duplicate of {data['of']}: {outcome}."
    elif "by" in data:
        note = f"Ask closed, superseded by {data['by']}: {outcome}."
    else:
        note = f"Ask withdrawn: {outcome.removeprefix(WITHDRAWN)}."
    _tell_reply_threads(tx, row["id"], note)


def reopen(tx, row, reason):
    """Reopen an answered ask whose answer did not settle it (spec 04, Amendments to Asks and owners).

    An open ask is Core's `not-closed`. A withdrawn ask, or an answered one under a closed parent, is
    final (rule `ask-final`), with or without a reason. Then it needs a one-line reason (usage error). Allowed for the asker, the
    parent's owner or any human (rule `ask-permission`). Clears resolution and outcome, keeps the
    `answer` section, and journals `ask.reopened` (ask.opened's fields, the addressee resolved now,
    plus reason and reopened_by) instead of `item.reopened`. Returns its id.
    """
    conn = tx.conn
    item_id = row["id"]
    if row["status"] != "closed":
        raise refused("not-closed", f"{item_id} is not closed", item=item_id)
    if row["resolution"] != "done":
        raise refused("ask-final", f"{item_id} was withdrawn; open a new ask instead", item=item_id)
    if items.get(conn, row["parent_id"])["status"] == "closed":
        raise refused("ask-final", f"{item_id}'s parent {row['parent_id']} is closed; open a new ask instead",
                      item=item_id)
    reason = items.check_outcome(reason, "reopening an ask needs --reason \"<one line>\": why the answer did not"
                                 " settle it", name="a reason")
    check_permission(conn, row, tx.actor, "reopen", ("asker", "owner", "human"))
    items.mark_open(tx, item_id)
    addressee = resolve(conn, row)
    tx.journal("ask.reopened", items.feed_fields(conn, item_id), status_old="closed", status_new="open",
               addressee=addressee, data={**_opened_data(conn, row, addressee), "reason": reason,
                                          "reopened_by": tx.actor})
    _queue_reopen_reply(tx, item_id, reason)
    return item_id


def _queue_reopen_reply(tx, item_id, reason):
    """When the answer being reopened came from a page comment, queue "Reopened by <name>: <reason>" on
    that comment's thread in the reopen's own transaction, without resolving it: the page's outbox
    (`page_replies`, publisher bookkeeping) sends it on the next pull."""
    conn = tx.conn
    answered = conn.execute("SELECT data FROM journal WHERE item = ? AND change = 'ask.answered' "
                            "ORDER BY seq DESC LIMIT 1", (item_id,)).fetchone()
    data = json.loads(answered["data"]) if answered else {}
    if data.get("via") != "page" or not data.get("slug") or not data.get("comment_id"):
        return
    conn.execute("INSERT INTO page_replies (slug, comment_id, body, resolve, created_at) VALUES (?, ?, ?, 0, ?)",
                 (data["slug"], data["comment_id"], f"Reopened by {actors.display(conn, tx.actor)}: {reason}",
                  clock.stamp(tx.now)))


def _closing_facts(tx, row):
    """The keys `ask.answered` and `ask.withdrawn` share: addressee_role, owner_kind, and the parent's
    most recent claim (live or lapsed) with whether it is live as this row commits."""
    conn = tx.conn
    owner = items.hierarchy(conn, row["id"])["effective_owner"]
    claim = claims.latest(conn, row["parent_id"])
    return {"addressee_role": row["addressee_role"], "owner_kind": actors.kind(conn, owner),
            "parent_claim_session": claim["session"] if claim is not None else None,
            "parent_claim_live": claim is not None and claims.is_live(claim, tx.now)}


def snapshot(conn):
    """What every open ask resolves to now: {ask id: actor}. Open asks are few, so this is cheap."""
    rows = conn.execute("SELECT * FROM items WHERE form = 'ask' AND status = 'open'").fetchall()
    return {row["id"]: resolve(conn, row) for row in rows}


def reroute(tx, before, cause):
    """Implicit readdressing: after a write that may change what open asks resolve to (`roles set`,
    an `owner.changed`, an `item.moved`), one `ask.readdressed` row for each ask in `before` (a
    `snapshot` taken ahead of the write) whose resolved actor changed, in id order. `cause` is
    `role-set`, `owner-changed` or `moved`. A row may land on the asker: the write is not refused,
    and the row says `self_addressed: true`."""
    conn = tx.conn
    for ask_id, old in sorted(before.items()):
        row = items.get(conn, ask_id)
        if resolve(conn, row) != old:
            _journal_readdressed(tx, row, old_addressee=old, old_role=row["addressee_role"], cause=cause)


def _journal_readdressed(tx, row, *, old_addressee, old_role, cause):
    """One `ask.readdressed` row for an ask `row`, as it stands after the change."""
    conn = tx.conn
    addressee = resolve(conn, row)
    tx.journal(
        "ask.readdressed",
        items.feed_fields(conn, row["id"]),
        addressee=addressee,
        data={"old_addressee": old_addressee, "old_role": old_role, "addressee_role": row["addressee_role"],
              "addressee_kind": actors.kind(conn, addressee), "cause": cause,
              "self_addressed": addressee == row["asker"]},
    )


# The cache-window curve of the urgency score. An asker's prompt cache stays warm about CACHE_WINDOW_MINUTES
# after it asks; the factor climbs from 1 to just under CACHE_PEAK across the window, then drops to 1.
CACHE_WINDOW_MINUTES = 60
CACHE_PEAK = 10
URGENT_MULTIPLIER = 10


def cache_factor(minutes):
    """How much a wait of `minutes` weighs: 1 + (CACHE_PEAK - 1) x (minutes / CACHE_WINDOW_MINUTES)^2 inside the
    window, 1 from the window on (the cache is cold; waiting longer costs nothing more)."""
    if minutes >= CACHE_WINDOW_MINUTES:
        return 1
    return 1 + (CACHE_PEAK - 1) * (minutes / CACHE_WINDOW_MINUTES) ** 2


def held_up(conn, ask):
    """How many distinct open non-ask items cannot move until `ask` is answered: its parent, and every open
    item reachable from the parent over outgoing `blocks` edges alone. That is ready.py's notion of blocked: an
    open ask keeps only its parent off the frontier, so its parent's children are not held up. A closed item
    neither counts nor carries the walk on; UNION keeps each item once, so cycles end."""
    row = conn.execute(
        """
        WITH RECURSIVE held (id) AS (
            SELECT ?
            UNION
            SELECT edges.dst FROM held
            JOIN edges ON edges.src = held.id AND edges.type = 'blocks'
            JOIN items AS next ON next.id = edges.dst
            WHERE next.status = 'open' AND COALESCE(next.form, '') <> 'ask'
        )
        SELECT COUNT(*) FROM held
        """,
        (ask["parent_id"],),
    ).fetchone()
    return row[0]


def waiting_since(conn, row):
    """When an ask last started waiting: its latest `ask.opened` or `ask.reopened` row, or `ask.readdressed`
    row whose cause is `readdress` (the asker's own `trk readdress`). A re-route that only follows a role, owner
    or move change (cause `role-set`, `owner-changed`, `moved`) leaves the asker's prompt cache as it was.
    A RED-read ask is held until its ticket's brief is complete, so its wait also starts no earlier than that
    ticket's latest `review-brief` write (read from the journal) while the brief is complete."""
    latest = conn.execute("SELECT at FROM journal WHERE item = ? AND (change IN ('ask.opened', 'ask.reopened')"
                          " OR (change = 'ask.readdressed' AND json_extract(data, '$.cause') = 'readdress'))"
                          " ORDER BY seq DESC LIMIT 1", (row["id"],)).fetchone()
    since = latest["at"] if latest else row["created_at"]
    from . import blast  # a gates module: loaded only when an ask's wait is read

    if not row["title"].startswith(blast.RED_ASK_TITLE):
        return since
    parent = items.get(conn, row["parent_id"])
    written = blast.brief_written_at(conn, parent["id"]) if blast.brief_complete(conn, parent) else None
    return max(since, written) if written else since  # a RED-read ask waits on no one until its brief is complete


def age_text(seconds):
    """A wait as its largest whole unit: 45s, 12m, 3h, 2d."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def urgency(conn, row, now, as_of=False):
    """An open ask's urgency at `now`, computed on read and never stored: held_up x cache factor x 10 when the
    ask or its parent is urgent, to one decimal place; `base` is held_up x the urgent multiplier, with no time
    factor. `reason` is the line every surface shows; a page, which can be read long after it was rendered,
    passes `as_of` to end the cache note with the render time."""
    minutes = max(int((now - clock.parse(waiting_since(conn, row))).total_seconds()) // 60, 0)
    held = held_up(conn, row)
    urgent = bool(row["urgent"] or items.get(conn, row["parent_id"])["urgent"])
    base = held * (URGENT_MULTIPLIER if urgent else 1)  # the score before the time factor: a consumer with its own cache clock applies that
    score = round(base * cache_factor(minutes), 1)
    warm = minutes < CACHE_WINDOW_MINUTES
    stamp = f", as of {now.astimezone(UTC):%H:%MZ}" if as_of else ""
    waited = f"{minutes}m (cache warm, {CACHE_WINDOW_MINUTES - minutes}m left{stamp})" if warm \
        else f"{age_text(minutes * 60)} (cache cold{stamp})"
    reason = f"score {score:.1f} · holds up {held} · waiting {waited}" + (" · urgent" if urgent else "")
    return {"score": score, "base": base, "held_up": held, "minutes": minutes, "cache_warm": warm, "urgent": urgent,
            "reason": reason}


def held_reason(conn, row):
    """Why an open ask is held back from its addressee, or None: a RED-read ask whose ticket has no complete
    review brief (`blast.held_reason`). A held ask stays open and still blocks its ticket."""
    from . import blast  # a gates module: loaded only when an ask is listed

    return blast.held_reason(conn, row)


def waiting(conn, me=None, root=None, client=None, now=None, with_held=False):
    """Open ask rows whose role resolves to `me` (every open ask when `me` is None), under `root`
    (an item id; the ask itself counts) when given, and whose effective client is `client` when given.
    Highest urgency score first at `now` (the clock's when omitted), then by id.

    A held ask (`held_reason`) waits on no one yet, so it is left out, unless `with_held`: then it is listed for
    its asker (who must get the brief written) instead of its addressee, and for everyone when `me` is None."""
    return [row for row, _ in ranked(conn, me, root, client, now, with_held)]


def _listed(conn, row, me, with_held):
    """Whether `waiting` lists `row` for `me`: a held ask for its asker (or any `me` None) when `with_held`,
    never otherwise; any other ask for its addressee."""
    if held_reason(conn, row) is not None:
        return with_held and (me is None or row["asker"] == me)
    return me is None or resolve(conn, row) == me


def ranked(conn, me=None, root=None, client=None, now=None, with_held=False):
    """`waiting`'s rows with their urgency: [(row, urgency)], in `waiting`'s order."""
    now = clock.now() if now is None else now
    root_id = items.get(conn, root)["id"] if root is not None else None
    rows = conn.execute("SELECT * FROM items WHERE form = 'ask' AND status = 'open' ORDER BY id").fetchall()
    if root_id is not None:
        rows = [row for row in rows if any(link["id"] == root_id for link in items.lineage(conn, row["id"]))]
    if me is not None or not with_held:
        rows = [row for row in rows if _listed(conn, row, me, with_held)]
    if client is not None:
        rows = [row for row in rows if items.hierarchy(conn, row["id"])["effective_client"] == client]
    scored = [(row, urgency(conn, row, now)) for row in rows]
    return sorted(scored, key=lambda pair: -pair[1]["score"])  # stable: equal scores keep id order


def opened_at(conn, row):
    """When an ask was last opened: the time of its latest `ask.opened` or `ask.reopened` row, which the
    comment pull also counts answers from."""
    latest = conn.execute("SELECT at FROM journal WHERE item = ? AND change IN ('ask.opened', 'ask.reopened')"
                          " ORDER BY seq DESC LIMIT 1", (row["id"],)).fetchone()
    return latest["at"] if latest else row["created_at"]


def waiting_json(conn, row, now):
    """`waiting --json`'s entry: the item JSON plus its parent and how long it has waited."""
    parent = items.get(conn, row["parent_id"])
    age = int((now - clock.parse(row["created_at"])).total_seconds())
    return {**items.to_json(conn, row),
            "urgency": urgency(conn, row, now),
            "parent": {"id": parent["id"], "title": parent["title"], "kind": items.KIND_OUT[parent["kind"]],
                       "form": items.FORM_OUT.get(parent["form"])},
            "age_seconds": max(age, 0)}


def excerpt(markdown):
    """A section's markdown as plain text for a feed row or a page's ask box, at most EXCERPT_MAX characters.

    Drops heading, list and quote markers at line starts, code-fence lines, and `*`, `_` and backtick
    emphasis characters; turns links and images into their text; collapses whitespace. A cut ends in
    `…`, counted in the limit. Best effort, not a markdown parser.
    """
    lines = [_LINE_MARKERS.sub("", line) for line in markdown.splitlines() if not _FENCE.match(line)]
    text = _EMPHASIS.sub("", _LINK.sub(r"\1", "\n".join(lines)))
    text = _SPACE.sub(" ", text).strip()
    if len(text) <= EXCERPT_MAX:
        return text
    return text[:EXCERPT_MAX - 1].rstrip() + "…"
