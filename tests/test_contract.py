"""contract/changes.schema.json describes every change kind Core, Asks and owners, Handoffs and Stage gates write, from the
specs, so later tickets' rows are checked as they land; it allows unknown kinds and additional fields."""

import copy
import json
import sqlite3

import pytest

from feed_contract import assert_feed_valid, feed_validator

AT = "2026-10-10T21:52:03.123Z"


def item_row(change, data, **fields):
    row = {
        "seq": 7, "at": AT, "change": change, "item": "tr-k7m2q", "kind": "work-item", "form": "decision",
        "title": "D", "parent": "tr-p0000", "root": "tr-p0000", "depth": 1, "actor": "michael",
        "session": None, "owner": "michael", "status_old": None, "status_new": None, "data": data,
        "addressee": None,
    }
    row.update(fields)
    return row


def actor_row(change="actor.added", data=None):
    """A row with no item: actor.*, role.set, config.changed."""
    return {
        "seq": 1, "at": AT, "change": change, "item": None, "kind": None, "form": None, "title": None,
        "parent": None, "root": None, "depth": None, "actor": "michael", "session": None, "owner": None,
        "status_old": None, "status_new": None, "data": data or {"id": "herdr", "kind": "agent", "display": "herdr"},
        "addressee": None,
    }


POSITION_OLD = {"parent": "tr-p0000", "root": "tr-p0000", "depth": 1, "rank": "V"}
POSITION_NEW = {"parent": None, "root": "tr-k7m2q", "depth": 0, "rank": "W"}
CLAIM = {"claim_actor": "agent-a", "lease_until": AT, "forced": False}

# The keys ask.answered and ask.withdrawn share.
ASK_CLOSING = {"addressee_role": "product", "owner_kind": "human", "parent_claim_session": "s-1",
               "parent_claim_live": False}

TICKET = {"form": "spec-bound"}

# One row per change kind Core, Asks and owners, Handoffs and Stage gates write, shaped as the specs' feed sections state.
VALID_ROWS = {
    "item.created": item_row(
        "item.created",
        {"subtype": "research", "rank": "V", "urgent": False, "jira_url": None, "repos": [], "sections": ["question"]},
        status_new="open",
    ),
    "item.created (program)": item_row(
        "item.created",
        {"subtype": None, "rank": "V", "urgent": True, "jira_url": "https://x/PROJ-1", "repos": ["/r"], "sections": []},
        kind="program", form=None, parent=None, root="tr-k7m2q", depth=0, owner=None, status_new="open",
    ),
    "item.created (ticket)": item_row(
        "item.created",
        {"subtype": None, "rank": "V", "urgent": False, "jira_url": None, "repos": [], "sections": ["what-to-build"],
         "owner": None, "stage": "specd", "criteria": ["ac-1", "ac-2"]},
        status_new="open", **TICKET,
    ),
    "item.updated (import)": item_row(
        "item.updated",
        {"fields": ["section:acceptance-criteria", "section:acceptance-criteria-imported"], "old": {}, "new": {},
         "via": "migration"},
        **TICKET,
    ),
    "stage.changed": item_row("stage.changed", {"old": None, "new": "framed", "via": "create"}, **TICKET),
    "stage.changed (reason)": item_row(
        "stage.changed", {"old": "merged", "new": "built", "via": "reopen-stage", "reason": "verification failed"},
        form="self-contained",
    ),
    "criterion.added": item_row("criterion.added", {"handle": "ac-1", "text": "It works.", "version": 1, "rank": "V"},
                                **TICKET),
    "criterion.added (migration, program)": item_row(
        "criterion.added", {"handle": "ac-2", "text": "Agents find work.", "version": 1, "rank": "W", "via": "migration"},
        kind="program", form=None, parent=None, root="tr-k7m2q", depth=0,
    ),
    "criterion.edited": item_row(
        "criterion.edited", {"handle": "ac-1", "old_version": 1, "version": 2, "text": "It works well."},
        kind="project", form=None,
    ),
    "criterion.retired": item_row("criterion.retired", {"handle": "ac-3", "reason": "Out of scope now."}, **TICKET),
    "item.updated": item_row(
        "item.updated",
        {"fields": ["title", "section:notes"], "old": {"title": "C"}, "new": {"title": "D"}},
    ),
    "item.closed": item_row(
        "item.closed", {"resolution": "superseded", "outcome": "Replaced.", "by": "tr-zzzzz"},
        status_old="open", status_new="closed",
    ),
    "item.reopened": item_row(
        "item.reopened", {"resolution": "wont-do"}, status_old="closed", status_new="open",
    ),
    "item.moved": item_row(
        "item.moved", {"old": POSITION_OLD, "new": POSITION_NEW},
        parent=None, root="tr-k7m2q", depth=0, kind="project", form=None,
    ),
    "item.moved (rekey)": item_row("item.moved", {"old": POSITION_OLD, "new": POSITION_OLD, "rekey": True}),
    "owner.changed": item_row("owner.changed", {"old": None, "new": "michael"}),
    "owner.changed (moved-top)": item_row("owner.changed", {"old": None, "new": "michael", "reason": "moved-top"}),
    "owner.changed (migration)": item_row("owner.changed", {"old": None, "new": "michael", "reason": "migration"}),
    "claim.taken": item_row("claim.taken", {**CLAIM, "forced": True}, session="s-1"),
    "claim.renewed": item_row("claim.renewed", CLAIM, session="s-1"),
    "claim.released": item_row("claim.released", {**CLAIM, "reason": "closed"}, session="s-1", actor="herdr"),
    "claim.expired": item_row("claim.expired", CLAIM, session="s-1", actor="agent-a"),
    "spec.changed": item_row("spec.changed", {"old": "draft", "new": "accepted"}, kind="project", form=None),
    "spec.changed (accepted)": item_row("spec.changed", {"old": "draft", "new": "accepted", "acceptor": "worker",
                                                         "judgment": "e7k2m9"}, kind="project", form=None),
    "spec.changed (draft)": item_row("spec.changed", {"old": "accepted", "new": "draft", "reason": "Rethink."},
                                     kind="project", form=None),
    "edge.added": item_row("edge.added", {"type": "blocks", "src": "tr-k7m2q", "dst": "tr-abcde"}),
    "edge.removed": item_row("edge.removed", {"type": "relates", "src": "tr-k7m2q", "dst": "tr-abcde"}),
    "actor.added": actor_row(),
    "actor.updated": actor_row("actor.updated", {"id": "michael", "kind": "human", "display": "Michael"}),
    "actor.updated (service user)": actor_row("actor.updated", {"id": "kim", "kind": "human", "display": "kim",
                                                                "fields": ["service_user"]}),
    "role.set": actor_row("role.set", {"role": "product", "old": None, "new": "michael"}),
    "role.set (reassigned)": actor_row("role.set", {"role": "product", "old": "michael", "new": "kim"}),
    "config.changed": actor_row("config.changed", {"key": "default-owner", "old": None, "new": "michael"}),
    "config.changed (shared agent)": actor_row("config.changed", {"key": "shared-agent", "old": "worker",
                                                                  "new": "herdr"}),
    "config.changed (template parent)": actor_row("config.changed", {"key": "template-parent", "old": None,
                                                                     "new": "tr-k7m2q"}),
    "config.changed (publish)": actor_row("config.changed", {"key": "publish-team", "old": "tracker", "new": "scratch"}),
    "ask.opened": item_row(
        "ask.opened",
        {"subtype": None, "rank": "V", "urgent": False, "jira_url": None, "repos": [], "sections": ["question"],
         "owner": None, "addressee_role": "product", "addressee_kind": "human", "question": "Which unit?",
         "asker": "worker"},
        form="ask", actor="worker", addressee="kim", status_new="open",
    ),
    "ask.opened (tied to a ledger row)": item_row(
        "ask.opened",
        {"subtype": None, "rank": "V", "urgent": False, "jira_url": None, "repos": [], "sections": ["question"],
         "owner": None, "addressee_role": "engineering", "addressee_kind": "human", "question": "Which wins?",
         "asker": "worker", "ledger": "ledger-2"},
        form="ask", actor="worker", addressee="michael", status_new="open",
    ),
    "ask.opened (parent role, agent)": item_row(
        "ask.opened",
        {"subtype": None, "rank": "V", "urgent": True, "jira_url": None, "repos": [], "sections": ["question"],
         "owner": None, "addressee_role": "parent", "addressee_kind": "agent", "question": "x" * 239 + "…",
         "asker": "michael"},
        form="ask", addressee="worker", status_new="open",
    ),
    "ask.answered": item_row(
        "ask.answered",
        {**ASK_CLOSING, "resolution": "done", "outcome": "Use m³/h", "answer": "m³/h throughout.", "proxy": True,
         "via": "cli"},
        form="ask", actor="kim", addressee="michael", status_old="open", status_new="closed",
    ),
    "ask.answered (page)": item_row(
        "ask.answered",
        {**ASK_CLOSING, "resolution": "done", "outcome": "Use m³/h", "answer": "m³/h.", "proxy": False,
         "via": "page", "slug": "tr-p0000", "comment_id": "c-17", "owner_kind": None, "parent_claim_live": True},
        form="ask", actor="kim", addressee="kim", owner=None, status_old="open", status_new="closed",
    ),
    "ask.answered (page, confirmed)": item_row(
        "ask.answered",
        {**ASK_CLOSING, "resolution": "done", "outcome": "Use m³/h", "answer": "m³/h.", "proxy": False,
         "via": "page", "slug": "tr-p0000", "comment_id": "c-17", "confirmed_by": "worker"},
        form="ask", actor="kim", addressee="kim", status_old="open", status_new="closed",
    ),
    "ask.replied": item_row(
        "ask.replied", {"slug": "tr-p0000", "comment_id": "c-17", "excerpt": "What is the Q19 acceptance?"},
        form="ask", actor="kim", addressee="kim",
    ),
    "comment.routed": item_row(
        "comment.routed", {"slug": "tr-p0000", "handle": "what-to-build", "comment_id": "c-18",
                           "excerpt": "Should this also cover imports?"},
        form="spec-bound", actor="kim", addressee="michael",
    ),
    "comment.routed (Needs-you page)": {
        **actor_row("comment.routed", {"slug": "tr-n0000", "handle": "header", "comment_id": "c-19", "excerpt": "Hi",
                                       "page": "needs-you:kim"}),
        "actor": "kim", "addressee": "kim",
    },
    "comment.acknowledged": item_row(
        "comment.acknowledged", {"comment_id": "c-18", "ask": "tr-a1b2c"}, form="spec-bound", actor="worker",
    ),
    "ask.withdrawn": item_row(
        "ask.withdrawn",
        {**ASK_CLOSING, "resolution": "wont-do", "outcome": "withdrawn: not needed", "reason": "not needed"},
        form="ask", actor="worker", addressee="kim", status_old="open", status_new="closed",
    ),
    "ask.withdrawn (parent closed)": item_row(
        "ask.withdrawn",
        {**ASK_CLOSING, "resolution": "wont-do", "outcome": "withdrawn: parent closed (done)", "reason": "parent_closed"},
        form="ask", addressee="kim", status_old="open", status_new="closed",
    ),
    "ask.withdrawn (duplicate)": item_row(
        "ask.withdrawn",
        {**ASK_CLOSING, "resolution": "duplicate", "outcome": "Asked twice", "of": "tr-zzzzz", "reason": None},
        form="ask", actor="worker", addressee="kim", status_old="open", status_new="closed",
    ),
    "ask.withdrawn (superseded)": item_row(
        "ask.withdrawn",
        {**ASK_CLOSING, "resolution": "superseded", "outcome": "Reworded", "by": "tr-zzzzz", "reason": None},
        form="ask", actor="worker", addressee="kim", status_old="open", status_new="closed",
    ),
    "ask.reopened": item_row(
        "ask.reopened",
        {"subtype": None, "rank": "V", "urgent": False, "jira_url": None, "repos": [], "sections": ["question", "answer"],
         "owner": None, "addressee_role": "product", "addressee_kind": "human", "question": "Which unit?",
         "asker": "worker", "reason": "need units", "reopened_by": "worker"},
        form="ask", actor="worker", addressee="kim", status_old="closed", status_new="open",
    ),
    "handoff.added": item_row("handoff.added", {"id": 3, "first_line": "Stopped at the migration."}, session="s-1"),
    "handoff.added (no session)": item_row("handoff.added", {"id": 4, "first_line": "x" * 200}),
    "page.created": item_row("page.created", {"slug": "fake-0001", "url": "https://a.test/artifacts/fake-0001"},
                             actor="publisher"),
    "evidence.added": item_row(
        "evidence.added",
        {"id": "e7k2m9", "gate": "built", "kind": "test-run", "criterion": None, "criterion_version": None,
         "sha": "3f2a1c0", "verdict": "pass", "ref": "run-17", "note": "Full suite green."},
        session="s-1", **TICKET,
    ),
    "ledger.added": item_row(
        "ledger.added",
        {"ledger": "ledger-1", "n": 1, "kind": "fork", "decided": "Kept the cache", "alternatives": ["Dropped it"],
         "why": "Reads are hot", "would_have_asked": None, "conflicts_with": None, "files": ["src/a.py"],
         "origin": "builder", "noise": False, "repeat_of": None, "topic": None, "refers_to": None, "sha": None},
        session="s-1", **TICKET,
    ),
    "ledger.added (auditor)": item_row(
        "ledger.added",
        {"ledger": "ledger-2", "n": 2, "kind": "contradiction", "decided": "Two rows", "alternatives": [],
         "why": "Simpler", "would_have_asked": "Is one enough?", "conflicts_with": "spec:Evidence", "files": [],
         "origin": "auditor", "noise": True, "repeat_of": "tr-aaaaa:3", "topic": "units", "refers_to": "ledger-1",
         "sha": "3f2a1c0"},
        session="s-2", **TICKET,
    ),
    "ledger.verdict": item_row(
        "ledger.verdict",
        {"ledger": "ledger-1", "n": 1, "kind": "reward-hack", "verdict": "confirmed", "note": "Golden regenerated",
         "sha": "3f2a1c0"},
        session="s-2", **TICKET,
    ),
    "ledger.settled": item_row(
        "ledger.settled",
        {"ledger": "ledger-2", "n": 2, "kind": "contradiction", "settlement": "fixed", "sha": "3f2a1c0",
         "ask": "tr-qqqqq", "ticket": None, "adr": None, "note": None},
        **TICKET,
    ),
    "ledger.promoted": item_row(
        "ledger.promoted", {"ledger": "ledger-1", "n": 1, "kind": "fork", "adr": "docs/adr/0003-cache.md"}, **TICKET,
    ),
    "evidence.added (criterion, project)": item_row(
        "evidence.added",
        {"id": "0abcde", "gate": "close", "kind": "judgment", "criterion": "ac-2", "criterion_version": 3,
         "sha": None, "verdict": "pass", "ref": None, "note": "Delivered by tr-aaaaa and tr-bbbbb."},
        kind="project", form=None,
    ),
    "evidence.added (retract)": item_row(
        "evidence.added",
        {"id": "z9y8x7", "gate": "built", "kind": "retract", "criterion": None, "criterion_version": None,
         "sha": None, "verdict": None, "ref": "e7k2m9", "note": "Ran the wrong suite."},
        **TICKET,
    ),
}


def response(*rows):
    return {"schema": 1, "epoch": "3f0b6c1e-8a43-4d4e-9f57-2b1f0c3d9a10", "head": 9, "next": 9, "changes": list(rows)}


def errors(value):
    return list(feed_validator().iter_errors(value))


@pytest.mark.parametrize("name", VALID_ROWS)
def test_every_change_kind_core_writes_validates(name):
    assert errors(response(VALID_ROWS[name])) == []


def test_empty_response_validates():
    assert errors({"schema": 1, "epoch": "3f0b6c1e-8a43-4d4e-9f57-2b1f0c3d9a10", "head": 0, "next": 0, "changes": []}) == []


def test_a_response_without_an_epoch_is_invalid():
    assert errors({"schema": 1, "head": 0, "next": 0, "changes": []}) != []


def test_unknown_change_kinds_and_additional_fields_are_allowed():
    future = item_row("ledger.disputed", {"anything": [1, 2]}, extra="x", status_new="open")
    reply = response(future, {**actor_row("ask.snoozed", {"whatever": True}), "addressee": "kim"})
    reply["future_field"] = {"x": 1}
    assert errors(reply) == []


def broken(name, **fields):
    """A valid row with `fields` replaced; in `data`, a value of ... deletes that key."""
    row = copy.deepcopy(VALID_ROWS[name])
    data = fields.pop("data", None)
    row.update(fields)
    if data is not None:
        row["data"] = {k: v for k, v in {**row["data"], **data}.items() if v is not ...}
    return row


def without(name, field):
    row = copy.deepcopy(VALID_ROWS[name])
    del row[field]
    return row


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(without("item.created", "owner"), id="missing-field"),
        pytest.param(broken("item.created", kind="work_item"), id="internal-kind-spelling"),
        pytest.param(broken("item.created", form="spec_bound"), id="internal-form-spelling"),
        pytest.param(broken("item.created", at="2026-10-10 21:52:03"), id="at-not-utc-millis"),
        pytest.param(broken("item.created", data=None, actor=None), id="null-actor"),
        pytest.param(broken("item.created", data={"rank": ...}), id="created-without-rank"),
        pytest.param(broken("item.created", status_new="closed"), id="created-not-open"),
        pytest.param(broken("item.created", form=None), id="work-item-without-form"),
        pytest.param(broken("item.created (program)", form="decision"), id="program-with-form"),
        pytest.param(broken("item.created", parent=None), id="depth-1-without-parent"),
        pytest.param(broken("item.updated", status_old="open"), id="status-on-update"),
        pytest.param(broken("item.closed", data={"resolution": "wont_do"}), id="internal-resolution-spelling"),
        pytest.param(broken("item.closed", data={"outcome": "two\nlines"}), id="outcome-with-newline"),
        pytest.param(broken("item.closed", data={"outcome": "trailing\n"}), id="outcome-with-trailing-newline"),
        pytest.param(broken("item.closed", data={"outcome": "x" * 301}), id="outcome-too-long"),
        pytest.param(broken("item.closed", data={"by": ...}), id="superseded-without-by"),
        pytest.param(broken("item.closed", data={"resolution": "duplicate", "by": ...}), id="duplicate-without-of"),
        pytest.param(broken("item.moved", data={"old": {"root": "tr-p0000"}}), id="moved-old-incomplete"),
        pytest.param(broken("claim.taken", session=None), id="claim-without-session"),
        pytest.param(broken("claim.taken", data={"claim_actor": ...}), id="claim-without-claimant"),
        pytest.param(broken("spec.changed", kind="program"), id="spec-on-program"),
        pytest.param(broken("spec.changed (accepted)", data={"judgment": ...}), id="acceptor-without-judgment"),
        pytest.param(broken("spec.changed (accepted)", data={"judgment": "E7K2M9"}), id="judgment-not-an-evidence-id"),
        pytest.param(broken("spec.changed (draft)", data={"reason": ""}), id="empty-draft-reason"),
        pytest.param(broken("edge.added", data={"type": "depends"}), id="unknown-edge-type"),
        pytest.param(broken("actor.added", root="tr-p0000"), id="actor-row-with-root"),
        pytest.param(broken("actor.added", data={"kind": "robot"}), id="unknown-actor-kind"),
        pytest.param({**VALID_ROWS["actor.added"], "data": "{}"}, id="data-as-text"),
        pytest.param(without("item.created", "addressee"), id="missing-addressee"),
        pytest.param(broken("owner.changed", addressee="kim"), id="addressee-off-an-ask-row"),
        pytest.param({**actor_row("ask.opened", {}), "addressee": None}, id="ask-row-without-addressee"),
        pytest.param(broken("owner.changed", data={"reason": "edited"}), id="unknown-owner-reason"),
        pytest.param(broken("ask.opened", form="decision"), id="ask-opened-not-an-ask"),
        pytest.param(broken("ask.opened", status_new=None), id="ask-opened-not-open"),
        pytest.param(broken("ask.opened", data={"asker": ...}), id="ask-opened-without-asker"),
        pytest.param(broken("ask.opened", data={"sections": []}), id="ask-opened-without-question"),
        pytest.param(broken("ask.opened", data={"addressee_kind": "robot"}), id="ask-opened-unknown-kind"),
        pytest.param(broken("ask.opened", data={"owner": "michael"}), id="ask-opened-with-own-owner"),
        pytest.param(broken("ask.opened", data={"subtype": "task"}), id="ask-opened-with-subtype"),
        pytest.param(broken("ask.opened", data={"repos": ["/r"]}), id="ask-opened-with-repos"),
        pytest.param(broken("ask.opened", data={"question": "x" * 241}), id="ask-opened-question-too-long"),
        pytest.param(broken("ask.opened", data={"question": "two\nlines"}), id="ask-opened-question-not-plain"),
        pytest.param(broken("ask.opened", data={"addressee_role": "Product"}), id="ask-opened-role-not-a-slug"),
        pytest.param(broken("actor.updated (service user)", data={"fields": ["email"]}), id="actor-updated-unknown-field"),
        pytest.param(broken("ask.answered", data={"via": ...}), id="answered-without-via"),
        pytest.param(broken("ask.replied", data={"comment_id": ...}), id="replied-without-comment"),
        pytest.param(broken("ask.replied", data={"excerpt": "two\nlines"}), id="replied-excerpt-not-plain"),
        pytest.param(broken("ask.replied", status_new="closed"), id="replied-closes"),
        pytest.param(broken("comment.routed", data={"comment_id": ...}), id="routed-without-comment"),
        pytest.param(broken("comment.routed", addressee=None), id="routed-without-addressee"),
        pytest.param(broken("comment.routed", data={"excerpt": "two\nlines"}), id="routed-excerpt-not-plain"),
        pytest.param(broken("comment.acknowledged", data={"comment_id": ...}), id="acknowledged-without-comment"),
        pytest.param(broken("comment.acknowledged", addressee="kim"), id="acknowledged-with-addressee"),
        pytest.param(broken("ask.answered", data={"via": "email"}), id="answered-unknown-via"),
        pytest.param(broken("ask.answered (page)", data={"comment_id": ...}), id="answered-on-a-page-without-comment"),
        pytest.param(broken("ask.answered", data={"resolution": "wont-do"}), id="answered-not-done"),
        pytest.param(broken("ask.answered", data={"proxy": ...}), id="answered-without-proxy"),
        pytest.param(broken("ask.answered", data={"answer": "two\nlines"}), id="answered-answer-not-plain"),
        pytest.param(broken("ask.answered", data={"parent_claim_live": ...}), id="answered-without-claim-live"),
        pytest.param(broken("ask.answered", data={"owner_kind": "robot"}), id="answered-unknown-owner-kind"),
        pytest.param(broken("ask.answered", status_new="open"), id="answered-not-closed"),
        pytest.param(broken("ask.answered", form="decision"), id="answered-not-an-ask"),
        pytest.param(broken("ask.withdrawn", data={"resolution": "done"}), id="withdrawn-done"),
        pytest.param(broken("ask.withdrawn", data={"reason": None}), id="withdrawn-wont-do-without-reason"),
        pytest.param(broken("ask.withdrawn", data={"reason": ...}), id="withdrawn-reason-missing"),
        pytest.param(broken("ask.withdrawn (duplicate)", data={"reason": "x"}), id="withdrawn-duplicate-with-reason"),
        pytest.param(broken("ask.withdrawn (duplicate)", data={"of": ...}), id="withdrawn-duplicate-without-of"),
        pytest.param(broken("ask.withdrawn (superseded)", data={"by": ...}), id="withdrawn-superseded-without-by"),
        pytest.param(broken("ask.withdrawn", data={"parent_claim_session": ...}), id="withdrawn-without-claim-session"),
        pytest.param(broken("ask.withdrawn", data={"outcome": "x" * 301}), id="withdrawn-outcome-too-long"),
        pytest.param(broken("ask.reopened", data={"reason": ...}), id="reopened-without-reason"),
        pytest.param(broken("ask.reopened", data={"reopened_by": ...}), id="reopened-without-reopened-by"),
        pytest.param(broken("ask.reopened", data={"asker": ...}), id="reopened-without-ask-opened-fields"),
        pytest.param(broken("ask.reopened", status_old="open"), id="reopened-not-from-closed"),
        pytest.param(broken("role.set", data={"role": "parent"}), id="role-set-parent"),
        pytest.param(broken("role.set", data={"role": "Product"}), id="role-not-a-slug"),
        pytest.param(broken("role.set", data={"new": None}), id="role-set-to-nobody"),
        pytest.param(broken("role.set", item="tr-k7m2q"), id="role-set-with-item"),
        pytest.param(broken("role.set", status_new="open"), id="role-set-with-status"),
        pytest.param(broken("config.changed", data={"key": "lease-minutes"}), id="config-unknown-key"),
        pytest.param(broken("config.changed", data={"old": ...}), id="config-without-old"),
        pytest.param(broken("handoff.added", data={"id": ...}), id="handoff-without-id"),
        pytest.param(broken("handoff.added", data={"id": "3"}), id="handoff-id-as-text"),
        pytest.param(broken("handoff.added", data={"first_line": ...}), id="handoff-without-first-line"),
        pytest.param(broken("handoff.added", data={"first_line": ""}), id="handoff-empty-first-line"),
        pytest.param(broken("handoff.added", data={"first_line": "two\nlines"}), id="handoff-first-line-newline"),
        pytest.param(broken("handoff.added", data={"first_line": "x" * 201}), id="handoff-first-line-too-long"),
        pytest.param(broken("handoff.added", data={"body": "the whole body"}), id="handoff-with-body"),
        pytest.param({**actor_row("handoff.added", {"id": 3, "first_line": "x"})}, id="handoff-without-item"),
        pytest.param(broken("handoff.added", status_new="open"), id="handoff-with-status"),
        pytest.param(broken("page.created", data={"slug": ...}), id="page-created-without-slug"),
        pytest.param(broken("page.created", data={"url": ""}), id="page-created-empty-url"),
        pytest.param(broken("page.created", form="ask"), id="page-created-on-an-ask"),
        pytest.param(broken("page.created", actor="michael"), id="page-created-not-by-the-publisher"),
        pytest.param({**actor_row("page.created", {"slug": "s", "url": "u"}), "actor": "publisher"},
                     id="page-created-without-item"),
        pytest.param(broken("page.created", status_new="open"), id="page-created-with-status"),
        pytest.param(broken("item.created (ticket)", data={"stage": "done"}), id="created-unknown-stage"),
        pytest.param(broken("item.created (ticket)", data={"criteria": ["1"]}), id="created-bad-criterion-handle"),
        pytest.param(broken("stage.changed", data={"via": "force"}), id="stage-unknown-via"),
        pytest.param(broken("stage.changed", data={"new": None}), id="stage-to-nothing"),
        pytest.param(broken("stage.changed", form="decision"), id="stage-on-a-decision"),
        pytest.param(broken("stage.changed", status_new="open"), id="stage-with-status"),
        pytest.param(broken("criterion.added", data={"version": 2}), id="added-not-version-1"),
        pytest.param(broken("criterion.added", data={"handle": "ac-0"}), id="criterion-handle-from-0"),
        pytest.param(broken("criterion.added", data={"text": "x" * 241}), id="criterion-text-not-an-excerpt"),
        pytest.param(broken("criterion.added", form="ask"), id="criterion-on-an-ask"),
        pytest.param(broken("criterion.edited", data={"old_version": ...}), id="edited-without-old-version"),
        pytest.param(broken("criterion.retired", data={"reason": ...}), id="retired-without-reason"),
        pytest.param(broken("evidence.added", data={"id": "E7K2M9"}), id="evidence-id-not-lowercase"),
        pytest.param(broken("evidence.added", data={"id": "e7k2m"}), id="evidence-id-too-short"),
        pytest.param(broken("evidence.added", data={"gate": "done"}), id="evidence-unknown-gate"),
        pytest.param(broken("evidence.added", data={"kind": "vibe"}), id="evidence-unknown-kind"),
        pytest.param(broken("evidence.added", data={"sha": "3F2A1C0"}), id="evidence-sha-not-lowercase"),
        pytest.param(broken("evidence.added", data={"sha": "3f2a1c"}), id="evidence-sha-too-short"),
        pytest.param(broken("evidence.added", data={"verdict": "maybe"}), id="evidence-unknown-verdict"),
        pytest.param(broken("evidence.added", data={"criterion": "1"}), id="evidence-bad-criterion"),
        pytest.param(broken("evidence.added", data={"note": "x" * 241}), id="evidence-note-not-an-excerpt"),
        pytest.param(broken("evidence.added", data={"criterion_version": ...}), id="evidence-without-version"),
        pytest.param(broken("evidence.added", form="ask"), id="evidence-on-an-ask"),
        pytest.param(broken("evidence.added", status_new="open"), id="evidence-with-status"),
        pytest.param(broken("ledger.added", data={"ledger": "1"}), id="ledger-bad-handle"),
        pytest.param(broken("ledger.added", data={"kind": "hunch"}), id="ledger-unknown-kind"),
        pytest.param(broken("ledger.added", data={"origin": "reviewer"}), id="ledger-unknown-origin"),
        pytest.param(broken("ledger.added", data={"why": ...}), id="ledger-without-why"),
        pytest.param(broken("ledger.added", data={"decided": "x" * 241}), id="ledger-decided-not-an-excerpt"),
        pytest.param(broken("ledger.added", form="decision"), id="ledger-on-a-decision"),
        pytest.param(broken("ledger.added", status_new="open"), id="ledger-with-status"),
        pytest.param(broken("ledger.verdict", data={"verdict": "fine"}), id="ledger-unknown-verdict"),
        pytest.param(broken("ledger.verdict", data={"note": ...}), id="ledger-verdict-without-note"),
        pytest.param(broken("ledger.settled", data={"settlement": "dismissed"}), id="ledger-unknown-settlement"),
        pytest.param(broken("ledger.settled", data={"sha": "3F2A1C0"}), id="ledger-sha-not-lowercase"),
        pytest.param(broken("ledger.promoted", data={"adr": ...}), id="ledger-promoted-without-adr"),
        pytest.param(broken("ask.opened", data={"ledger": "row-1"}), id="ask-tied-to-a-bad-handle"),
    ],
)
def test_schema_rejects_rows_that_break_the_spec(row):
    assert errors(response(row)) != []


# The claim, edge and actor kinds herdr reads: each one rejects a row whose data breaks its shape.
MALFORMED_DATA = {
    "claim.taken": {"forced": "yes"},
    "claim.renewed": {"lease_until": "2026-10-10 21:52"},
    "claim.released": {"reason": ""},
    "claim.expired": {"lease_until": ...},
    "edge.added": {"dst": ...},
    "edge.removed": {"src": None},
    "actor.added": {"id": "Herdr"},
}


@pytest.mark.parametrize("name", MALFORMED_DATA)
def test_each_claim_edge_and_actor_kind_rejects_malformed_data(name):
    assert errors(response(broken(name, data=MALFORMED_DATA[name]))) != []


def test_schema_rejects_a_bad_envelope():
    assert errors({"schema": 2, "head": 0, "next": 0, "changes": []}) != []
    assert errors({"schema": 1, "head": 0, "changes": []}) != []


@pytest.mark.feed_unchecked
def test_suite_fixture_catches_an_invalid_journal_row(trk, trk_home):
    trk("init", "--me", "michael")
    conn = sqlite3.connect(trk_home / "store.db")
    conn.execute(
        "INSERT INTO journal (at, change, actor, data) VALUES (?, 'item.created', 'michael', ?)",
        (AT, json.dumps({"rank": "V"})),
    )
    conn.commit()
    conn.close()
    with pytest.raises(AssertionError, match="contract"):
        assert_feed_valid(trk_home)
