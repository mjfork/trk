"""The CLI: parse arguments, call the store layers, print. No rules live here."""

import argparse
import json
import os
import sqlite3
import sys

from . import (actors, asks, claims, clock, comments, config, edges, feed, handoffs, items, ready, roles, sections, store,
               worktrees)
from .errors import TrkError, usage


PARENT_ROLE_NOTE = "parent is relative: it resolves to the effective owner of the asked item's parent"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise usage(message)


def _common(json_flag=True):
    common = Parser(add_help=False)
    common.add_argument("--as", dest="as_actor", metavar="ACTOR", help="writing actor")
    common.add_argument("--session", help="writing session id")
    if json_flag:
        common.add_argument("--json", action="store_true", help="print JSON")
    return common


def build_parser():
    parser = Parser(prog="trk", description="Agent-first issue tracker. `trk --version [--json]` prints the version, "
                    "source commit, whether it is the installed trk, and the store schema.")
    verbs = parser.add_subparsers(dest="verb", required=True, parser_class=Parser)
    common = _common()

    init = verbs.add_parser("init", parents=[common], help="create the store")
    init.add_argument("--me", required=True, metavar="ACTOR", help="the default human actor")
    init.add_argument("--display", help="display name for --me")
    init.add_argument("--prefix", default="tr", help="id prefix (default tr)")
    init.set_defaults(handler=cmd_init)

    new = verbs.add_parser("new", parents=[common], help="create an item")
    new.add_argument("kind", choices=list(items.KIND_IN))
    new.add_argument("title")
    new.add_argument("--form", choices=list(items.FORM_IN), help="work items: the form")
    new.add_argument("--subtype", choices=list(items.SUBTYPES), help="decisions: the subtype")
    new.add_argument("--parent", metavar="ID", help="parent item (default: top level)")
    _placement(new)
    new.add_argument("--owner", metavar="ACTOR", help="the actor driving it (default: inherited)")
    new.add_argument("--client", metavar="NAME", help="the client it belongs to (children inherit it)")
    new.add_argument("--urgent", action="store_true", help="lift to the front of the frontier")
    new.add_argument("--jira", metavar="URL", help="JIRA issue URL")
    new.add_argument("--repo", action="append", default=[], metavar="R", help="a repo it touches (repeatable)")
    new.add_argument("--section", action="append", default=[], metavar="HANDLE=@FILE|HANDLE=-",
                     help="a section body from a file or stdin (repeatable)")
    new.add_argument("--criterion", action="append", default=[], metavar="TEXT",
                     help="an acceptance criterion or destination statement, in order (repeatable)")
    new.set_defaults(handler=cmd_new)

    ask = verbs.add_parser("ask", parents=[common], help="open an ask: a question under an item, waiting on a role")
    ask.add_argument("parent", metavar="ID", help="the item that waits on the answer")
    ask.add_argument("--to", required=True, metavar="ROLE", help="a function role (see `trk roles`) or parent")
    ask.add_argument("title")
    ask.add_argument("--question", metavar="@FILE|-", help="the question's body, from a file or stdin (required)")
    ask.add_argument("--urgent", action="store_true", help="multiply its urgency score by 10 in the waiting list")
    ask.add_argument("--ledger", metavar="N", help="tie it to ledger row ledger-N of its parent ticket")
    _placement(ask)
    ask.set_defaults(handler=cmd_ask)

    readdress = verbs.add_parser("readdress", parents=[common], help="address an open ask to another role")
    readdress.add_argument("id", metavar="ASK")
    readdress.add_argument("--to", required=True, metavar="ROLE", help="a function role (see `trk roles`) or parent")
    readdress.set_defaults(handler=cmd_readdress)

    waiting = verbs.add_parser("waiting", parents=[common], help="open asks waiting on an actor, highest urgency score first",
                                 description="Each open ask scores held_up x cache factor x 10 if urgent, computed on read: "
                                 "held_up is the open items it holds up (its parent, and what the parent blocks through `blocks` edges); "
                                 "the factor rises from 1 to 10 over the first 60 minutes of waiting, while the asker's cache is warm, "
                                 "then drops to 1. A `trk readdress` restarts the wait; a role, owner or move re-route does not. "
                                 "Ties go to the lower id.")
    whose = waiting.add_mutually_exclusive_group()
    whose.add_argument("--me", metavar="ACTOR", help="asks resolving to ACTOR (default: the writing actor)")
    whose.add_argument("--all", action="store_true", help="every open ask")
    waiting.add_argument("--root", metavar="ID", help="only asks under this item")
    waiting.add_argument("--client", metavar="NAME", help="only asks whose effective client is NAME")
    waiting.set_defaults(handler=cmd_waiting)

    answer = verbs.add_parser("answer", parents=[common], help="answer an ask and close it done")
    answer.add_argument("id")
    answer.add_argument("--outcome", required=True, metavar="TEXT", help="the answer's gist in one line")
    body = answer.add_mutually_exclusive_group(required=True)
    body.add_argument("--answer", metavar="@FILE|-", help="the answer's body, from a file or stdin")
    body.add_argument("--from-comment", metavar="COMMENT", help="confirm a reply (a page comment, or reply-N from `trk reply`) as the answer")
    answer.set_defaults(handler=cmd_answer)

    reply = verbs.add_parser("reply", parents=[common],
                             help="record a reply to an ask with no page behind it; the ask stays open")
    reply.add_argument("id", metavar="ASK")
    reply.add_argument("--body", required=True, metavar="@FILE|-", help="the reply's text, from a file or stdin")
    reply.set_defaults(handler=cmd_reply)

    withdraw = verbs.add_parser("withdraw", parents=[common], help="withdraw an ask no longer needed")
    withdraw.add_argument("id")
    withdraw.add_argument("--reason", required=True, metavar="TEXT", help="why, in one line")
    withdraw.set_defaults(handler=cmd_withdraw)

    edit = verbs.add_parser("edit", parents=[common], help="change an item's scalar fields")
    edit.add_argument("id")
    edit.add_argument("--title")
    owner = edit.add_mutually_exclusive_group()
    owner.add_argument("--owner", metavar="ACTOR", help="set its own owner")
    owner.add_argument("--no-owner", action="store_true", help="clear its own owner so it inherits again")
    edit.add_argument("--client", metavar="NAME|none", help="set its own client, or `none` to inherit again")
    edit.add_argument("--archive", metavar="PATH|none",
                      help="where its worktrees' tmp/ is archived, <tree> standing for the tree's directory name"
                           " (children inherit it), or `none` to inherit again")
    urgent = edit.add_mutually_exclusive_group()
    urgent.add_argument("--urgent", dest="urgent", action="store_const", const=True)
    urgent.add_argument("--no-urgent", dest="urgent", action="store_const", const=False)
    jira = edit.add_mutually_exclusive_group()
    jira.add_argument("--jira", metavar="URL")
    jira.add_argument("--no-jira", action="store_true")
    edit.add_argument("--add-repo", action="append", default=[], metavar="R")
    edit.add_argument("--remove-repo", action="append", default=[], metavar="R")
    # Kind and form are immutable; these exist only to say so.
    edit.add_argument("--kind", help=argparse.SUPPRESS)
    edit.add_argument("--form", help=argparse.SUPPRESS)
    edit.set_defaults(handler=cmd_edit)

    repos_verb = verbs.add_parser("repos", help="the repos items name")
    repos_verbs = repos_verb.add_subparsers(dest="repos_verb", required=True, parser_class=Parser)
    absolutize = repos_verbs.add_parser("absolutize", parents=[common],
                                        help="make every relative repo absolute once (resolved against ~/projects)")
    absolutize.add_argument("--dry-run", action="store_true", help="print what would change and what cannot be placed")
    absolutize.set_defaults(handler=cmd_repos_absolutize)

    move = verbs.add_parser("move", parents=[common], help="reparent and/or reorder an item (placed --last unless told otherwise)")
    move.add_argument("id")
    target = move.add_mutually_exclusive_group()
    target.add_argument("--parent", metavar="ID", help="the new parent (default: its parent now)")
    target.add_argument("--top", action="store_true", help="lift it to the top level")
    _placement(move)
    move.set_defaults(handler=cmd_move)

    close = verbs.add_parser("close", parents=[common], help="close an item, children first")
    close.add_argument("id")
    close.add_argument("--resolution", required=True, choices=list(items.RESOLUTION_IN))
    close.add_argument("--outcome", required=True, metavar="TEXT", help="the result in one line")
    close.add_argument("--by", metavar="ID", help="superseded: the replacement")
    close.add_argument("--of", metavar="ID", help="duplicate: the original")
    close.set_defaults(handler=cmd_close)

    for name, handler, help_text in (("link", cmd_link, "add an edge"), ("unlink", cmd_unlink, "remove an edge")):
        edge_verb = verbs.add_parser(name, parents=[common], help=help_text)
        edge_verb.add_argument("src", metavar="A")
        edge_verb.add_argument("type", choices=edges.TYPES)
        edge_verb.add_argument("dst", metavar="B")
        edge_verb.set_defaults(handler=handler)

    reopen = verbs.add_parser("reopen", parents=[common], help="reopen a closed item")
    reopen.add_argument("id")
    reopen.add_argument("--reason", metavar="TEXT", help="asks: why the answer did not settle it; tickets closed"
                        " at verified: why they go back (required for both)")
    reopen.add_argument("--to-stage", metavar="STAGE",
                        help="a ticket closed at verified: the earlier stage it goes back to (required)")
    reopen.set_defaults(handler=cmd_reopen)

    spec = verbs.add_parser("spec", help="accept a project's spec or put it back to draft")
    spec_verbs = spec.add_subparsers(dest="spec_verb", required=True, parser_class=Parser)
    for state, verb, help_text in (("accepted", "accept", "accept the spec"), ("draft", "draft", "put the spec back to draft")):
        spec_verb = spec_verbs.add_parser(verb, parents=[common], help=help_text)
        spec_verb.add_argument("id", metavar="PROJECT")
        if verb == "draft":
            spec_verb.add_argument("--reason", metavar="TEXT", help="why the spec goes back to draft (required)")
        spec_verb.set_defaults(handler=cmd_spec, state=state, reason=None)

    ready_verb = verbs.add_parser("ready", parents=[common], help="the frontier: items to start now, urgent first")
    ready_verb.add_argument("--root", metavar="ID", help="only this subtree")
    ready_verb.add_argument("--owner", metavar="ACTOR", help="only items this actor effectively owns")
    ready_verb.add_argument("--client", metavar="NAME", help="only items whose effective client is NAME")
    ready_verb.add_argument("--limit", type=_count(1), metavar="N", help="at most N items")
    ready_verb.add_argument("--stage", metavar="STAGE", help="only tickets at this stage (framed, specd, built, merged, verified)")
    ready_verb.set_defaults(handler=cmd_ready)

    claim = verbs.add_parser(
        "claim", parents=[common], help="take a lease on an item, or on the next ready one",
        epilog="See also `trk claim renew` and `trk claim release`.",
    )
    claim.add_argument("id", nargs="?", help="the item to claim")
    claim.add_argument("--next", action="store_true", help="claim the first item `ready` returns")
    claim.add_argument("--root", metavar="ID", help="--next: only this subtree")
    claim.add_argument("--owner", metavar="ACTOR", help="--next: only items this actor effectively owns")
    claim.add_argument("--force", action="store_true", help="claim a program (a root's orchestrator holding it)")
    claim.add_argument("--worktree", metavar="PATH", help="also record PATH as the item's worktree (trk worktree set)")
    _lease(claim, "lease length (default: the store's lease_minutes)")
    claim.set_defaults(handler=cmd_claim)
    renew = verbs.add_parser("claim renew", parents=[common], help="renew a session's leases that are past half",
                           epilog="Without --lease only claims with under half their lease left are renewed; "
                                  "the rest are listed as not due.")
    _lease(renew, "renew every live claim now, whether due or not, and store this length")
    renew.set_defaults(handler=cmd_claim_renew)
    release = verbs.add_parser("claim release", parents=[common], help="release a session's claims, or one")
    release.add_argument("id", nargs="?", help="only the claim on this item")
    release.set_defaults(handler=cmd_claim_release)

    claims_verb = verbs.add_parser("claims", parents=[common], help="list live claims")
    claims_verb.add_argument("--item", metavar="ID", help="only the claim on this item")
    claims_verb.set_defaults(handler=cmd_claims)

    sweep = verbs.add_parser("sweep", parents=[common], help="end every lapsed lease now")
    sweep.set_defaults(handler=cmd_sweep)

    tree = verbs.add_parser("tree", parents=[common], help="outline open items in rank order")
    tree.add_argument("id", nargs="?")
    tree.add_argument("--all", action="store_true", help="include closed items, each with its outcome")
    tree.set_defaults(handler=cmd_tree)

    actor = verbs.add_parser("actor", help="register or edit an actor")
    actor_verbs = actor.add_subparsers(dest="actor_verb", required=True, parser_class=Parser)
    actor_add = actor_verbs.add_parser("add", parents=[common], help="register a person or agent role")
    actor_add.add_argument("id", help="a lowercase slug")
    actor_add.add_argument("--kind", required=True, choices=actors.KINDS)
    actor_add.add_argument("--display", help="display name (default: the id)")
    actor_add.set_defaults(handler=cmd_actor_add)
    actor_edit = actor_verbs.add_parser("edit", parents=[common], help="change an actor's display name")
    actor_edit.add_argument("id")
    actor_edit.add_argument("--display", required=True)
    actor_edit.set_defaults(handler=cmd_actor_edit)
    actor_link = actor_verbs.add_parser("link", parents=[common],
                                        help="link an actor to a page commenter's author id (page comments)")
    actor_link.add_argument("id")
    actor_link.add_argument("--service-user", required=True, metavar="ID",
                            help="the author id `trk pages authors` lists")
    actor_link.set_defaults(handler=cmd_actor_link)

    actors_verb = verbs.add_parser("actors", parents=[common], help="list registered actors")
    actors_verb.set_defaults(handler=cmd_actors)

    roles_verb = verbs.add_parser("roles", parents=[common], help="list function roles and who holds each",
                                  epilog="See also `trk roles set`.")
    roles_verb.set_defaults(handler=cmd_roles)
    roles_set = verbs.add_parser("roles set", parents=[common], help="create or reassign a function role")
    roles_set.add_argument("role", help="a lowercase slug")
    roles_set.add_argument("actor", help="a registered actor")
    roles_set.set_defaults(handler=cmd_roles_set)

    config_verb = verbs.add_parser("config", parents=[common], help="list store settings",
                                   epilog="See also `trk config set`.")
    config_verb.set_defaults(handler=cmd_config)
    config_set = verbs.add_parser("config set", parents=[common], help="change a store setting")
    config_set.add_argument("key", help=f"the setting: {', '.join(config.SETTABLE)}")
    config_set.add_argument("value", metavar="VALUE", help="an actor; for template-parent, an item id")
    config_set.set_defaults(handler=cmd_config_set)

    worktree = verbs.add_parser("worktree", help="record or remove an item's worktree")
    worktree_verbs = worktree.add_subparsers(dest="worktree_verb", required=True, parser_class=Parser)
    worktree_set = worktree_verbs.add_parser("set", parents=[common], help="record the item's worktree, replacing one",
                                             epilog="--session is the writer's session; the tree records it too.")
    worktree_set.add_argument("id")
    worktree_set.add_argument("--path", required=True, metavar="P", help="the tree's directory (resolved with realpath)")
    worktree_set.add_argument("--branch", metavar="B", help="its branch (default: read from git)")
    worktree_set.set_defaults(handler=cmd_worktree_set)
    worktree_clear = worktree_verbs.add_parser("clear", parents=[common], help="remove the item's worktree")
    worktree_clear.add_argument("id")
    worktree_clear.set_defaults(handler=cmd_worktree_clear)

    where_verb = verbs.add_parser("where", parents=[common], help="which items record the tree holding a path")
    where_verb.add_argument("path")
    where_verb.set_defaults(handler=cmd_where)

    show = verbs.add_parser("show", parents=[common], help="show one item")
    show.add_argument("id")
    show.add_argument("--no-sections", action="store_true", help="omit section bodies")
    show.set_defaults(handler=cmd_show)

    section = verbs.add_parser("section", help="write, remove or read one section")
    section_verbs = section.add_subparsers(dest="section_verb", required=True, parser_class=Parser)
    section_set = section_verbs.add_parser("set", parents=[common], help="write a section (stdin by default)")
    section_set.add_argument("id")
    section_set.add_argument("handle")
    section_set.add_argument("--file", metavar="F", help="read the body from F instead of stdin")
    section_set.set_defaults(handler=cmd_section_set)
    section_rm = section_verbs.add_parser("rm", parents=[common], help="remove a section")
    section_rm.add_argument("id")
    section_rm.add_argument("handle")
    section_rm.set_defaults(handler=cmd_section_rm)
    section_get = section_verbs.add_parser("get", parents=[common], help="print a section's raw body")
    section_get.add_argument("id")
    section_get.add_argument("handle")
    section_get.set_defaults(handler=cmd_section_get)

    handoff = verbs.add_parser("handoff", help="add or read an item's handoffs (checkpoint records)")
    handoff_verbs = handoff.add_subparsers(dest="handoff_verb", required=True, parser_class=Parser)
    handoff_add = handoff_verbs.add_parser("add", parents=[common], help="append a handoff (stdin by default)")
    handoff_add.add_argument("id")
    handoff_add.add_argument("--file", metavar="F", help="read the body from F instead of stdin")
    handoff_add.set_defaults(handler=cmd_handoff_add)
    handoff_show = handoff_verbs.add_parser("show", parents=[common], help="print the latest handoff's raw body")
    handoff_show.add_argument("id")
    handoff_show.add_argument("--all", action="store_true", help="every handoff, oldest first, each under a header")
    handoff_show.set_defaults(handler=cmd_handoff_show)

    comment = verbs.add_parser("comment", help="answer or acknowledge a page comment routed to an owner")
    comment_verbs = comment.add_subparsers(dest="comment_verb", required=True, parser_class=Parser)
    comment_reply = comment_verbs.add_parser("reply", parents=[common],
                                             help="reply in a routed comment's thread (the page outbox sends it)")
    comment_reply.add_argument("comment", metavar="COMMENT", help="a comment id, as `trk show <item> --json` lists it")
    comment_reply.add_argument("--body", required=True, metavar="@file|-", help="the reply's text")
    comment_reply.set_defaults(handler=cmd_comment_reply)
    comment_ack = comment_verbs.add_parser("ack", parents=[common],
                                           help="take a routed comment off `show`'s list")
    comment_ack.add_argument("comment", metavar="COMMENT")
    comment_ack.add_argument("--ask", metavar="ID", help="the ask that took the point on")
    comment_ack.set_defaults(handler=cmd_comment_ack)

    criterion = verbs.add_parser("criterion", help="add, edit or retire one acceptance criterion")
    criterion_verbs = criterion.add_subparsers(dest="criterion_verb", required=True, parser_class=Parser)
    criterion_add = criterion_verbs.add_parser("add", parents=[common], help="add a criterion; prints its handle")
    criterion_add.add_argument("id")
    criterion_add.add_argument("text")
    where = criterion_add.add_mutually_exclusive_group()
    where.add_argument("--first", dest="where", action="store_const", const="first", help="first in the list")
    where.add_argument("--last", dest="where", action="store_const", const="last", help="last in the list (default)")
    where.add_argument("--before", metavar="ac-N", help="just before criterion ac-N")
    where.add_argument("--after", metavar="ac-N", help="just after criterion ac-N")
    criterion_add.set_defaults(handler=cmd_criterion_add)
    criterion_edit = criterion_verbs.add_parser("edit", parents=[common], help="new text at the next version")
    criterion_edit.add_argument("id")
    criterion_edit.add_argument("handle", metavar="ac-N")
    criterion_edit.add_argument("text")
    criterion_edit.set_defaults(handler=cmd_criterion_edit)
    criterion_retire = criterion_verbs.add_parser("retire", parents=[common], help="retire a criterion, kept struck through")
    criterion_retire.add_argument("id")
    criterion_retire.add_argument("handle", metavar="ac-N")
    criterion_retire.add_argument("--reason", required=True, metavar="TEXT", help="why, in one line")
    criterion_retire.set_defaults(handler=cmd_criterion_retire)

    criteria_verb = verbs.add_parser("criteria", parents=[common], help="list an item's criteria with their evidence status")
    criteria_verb.add_argument("id")
    criteria_verb.set_defaults(handler=cmd_criteria)

    advance = verbs.add_parser("advance", parents=[common], help="move a ticket one stage forward through its gate")
    advance.add_argument("id")
    advance.add_argument("--judgment", metavar="NOTE",
                         help="a self-contained ticket at framed: judge it complete (writes the judgment row)")
    advance.add_argument("--outcome", metavar="TEXT",
                         help="into verified (required there): the result in one line; the ticket closes done")
    advance.set_defaults(handler=cmd_advance)

    skip = verbs.add_parser("skip", parents=[common], help="meet merged by a skip, for a ticket that changes no repo")
    skip.add_argument("id")
    skip.add_argument("stage", help="the stage to skip: merged is the only one")
    skip.add_argument("--reason", metavar="TEXT", help="why, in one line (required)")
    skip.set_defaults(handler=cmd_skip)

    reopen_stage = verbs.add_parser("reopen-stage", parents=[common],
                                    help="move an open ticket back to an earlier stage")
    reopen_stage.add_argument("id")
    reopen_stage.add_argument("stage", help="the earlier stage it goes back to")
    reopen_stage.add_argument("--reason", metavar="TEXT", help="why, in one line (required)")
    reopen_stage.set_defaults(handler=cmd_reopen_stage)

    blast_verb = verbs.add_parser("blast-radius", parents=[common],
                                  help="rate a ticket's changed files RED/YELLOW/GREEN and record its lane")
    blast_verb.add_argument("id")
    blast_verb.add_argument("--sha", metavar="S", help="the commit to rate (default: the head sha)")
    blast_verb.add_argument("--base", metavar="REF",
                            help="diff against the merge-base with REF (default: a merge commit's first parent, else"
                                 " the PR's base branch fetched now, else origin/HEAD, else main, else master; an empty"
                                 " default diff is refused, and so is a diff with files the PR never touched)")
    blast_verb.add_argument("--repo", metavar="PATH", help="the checkout to diff in (default: the ticket's repo)")
    blast_verb.set_defaults(handler=cmd_blast_radius)

    review_verb = verbs.add_parser("review record", parents=[common],
                                   help="record a review as human-review evidence; prints its id",
                                   epilog="Without --ref, a person's direct review: --as must be a person, --sha the head,"
                                          " and the head must have a RED rating and an open RED-read ask for"
                                          " --as. The ref stored is direct:<actor>@<head>.")
    review_verb.add_argument("id")
    review_verb.add_argument("--ref", metavar="URL", help="the GitHub review's URL (default: a direct approval)")
    review_verb.add_argument("--sha", required=True, metavar="S", help="the head sha it reviewed")
    review_verb.add_argument("outcome", choices=("approved", "changes-requested"))
    review_verb.add_argument("--note", required=True, metavar="TEXT", help="what it says, in one line")
    review_verb.set_defaults(handler=cmd_review_record)

    evidence_verb = verbs.add_parser("evidence", parents=[common], help="list an item's evidence, each row weighed",
                                     epilog="See also `trk evidence add` and `trk evidence retract`.")
    evidence_verb.add_argument("id")
    evidence_verb.add_argument("--gate", metavar="G", help="only rows naming this gate")
    evidence_verb.set_defaults(handler=cmd_evidence)
    evidence_add = verbs.add_parser("evidence add", parents=[common], help="record one evidence row; prints its id")
    evidence_add.add_argument("id")
    evidence_add.add_argument("--kind", required=True, metavar="K", help="judgment, pr, test-run, webtester-run, "
                              "human-accept, merge, human-review, review, plan or doc")
    evidence_add.add_argument("--gate", required=True, metavar="G", help="the gate it is evidence for")
    evidence_add.add_argument("--criterion", metavar="ac-N", help="the criterion it proves")
    evidence_add.add_argument("--ref", metavar="R", help="PR URL, branch, run id, log path, URL…")
    evidence_add.add_argument("--sha", metavar="S", help="the commit, 7 to 40 hex chars")
    evidence_add.add_argument("--verdict", choices=("pass", "fail"))
    evidence_add.add_argument("--note", required=True, metavar="TEXT", help="what it shows, in one line")
    evidence_add.set_defaults(handler=cmd_evidence_add)
    evidence_retract = verbs.add_parser("evidence retract", parents=[common],
                                        help="withdraw one evidence row with a retract row; prints its id")
    evidence_retract.add_argument("id")
    evidence_retract.add_argument("evidence", metavar="EVIDENCE-ID")
    evidence_retract.add_argument("--note", required=True, metavar="TEXT", help="why, in one line")
    evidence_retract.set_defaults(handler=cmd_evidence_retract)

    # `pages` lists on its own and also carries subverbs, whose defaults would overwrite a --json given before
    # the subverb; so its own --json has a dest of its own, and _pages_flags hands it on (or refuses the rest).
    pages = verbs.add_parser("pages", help="item pages: list them, set up publishing, render one")
    pages.add_argument("--json", dest="pages_json", action="store_true", help="print JSON")
    pages.add_argument("--dirty", action="store_true", help="only pages waiting to be sent")
    pages.add_argument("--errors", action="store_true",
                       help="only pages whose last attempt failed, and replies that failed for good")
    pages.set_defaults(handler=cmd_pages)
    pages_verbs = pages.add_subparsers(dest="pages_verb", parser_class=Parser)
    pages_setup = pages_verbs.add_parser("setup", parents=[common],
                                         help="configure publishing and check the backend (a token is never stored or printed)")
    pages_setup.add_argument("--backend", metavar="NAME",
                             help="where pages go: folder (built in) or an installed backend (default: as set)")
    pages_setup.add_argument("--dir", metavar="PATH", help="folder: the directory the pages are written to")
    pages_setup.add_argument("--base-url", metavar="URL",
                             help="a token backend's service, https://…; folder: where --dir is served (optional)")
    pages_setup.add_argument("--token-file", default=None, metavar="PATH",
                             help="a token backend's `tracker` token, mode 0600 (default ~/.config/trk/token)")
    pages_setup.add_argument("--expires", metavar="YYYY-MM-DD", help="a token backend's token expiry date")
    pages_setup.add_argument("--home", required=True, metavar="ID", help="the open item the publisher's asks sit under")
    pages_setup.add_argument("--team", metavar="SLUG", help="the team every page is shared on (default: as set, tracker)")
    pages_setup.set_defaults(handler=cmd_pages_setup)
    pages_flush = pages_verbs.add_parser("flush", parents=[common], help="publish due pages")
    pages_flush.add_argument("--now", action="store_true", help="treat every dirty page as due, failed ones included")
    pages_flush.add_argument("--item", action="append", metavar="ID", help="only this item's page, or needs-you:<actor> (repeatable)")
    pages_flush.add_argument("--wait", action="store_true", help="keep flushing until no page is waiting")
    pages_flush.set_defaults(handler=cmd_pages_flush)
    pages_pull = pages_verbs.add_parser("pull", parents=[common], help="run the comment pull and the outbox")
    pages_pull.set_defaults(handler=cmd_pages_pull)
    pages_authors = pages_verbs.add_parser("authors", parents=[common],
                                           help="author ids seen on page comments and their linked actors")
    pages_authors.set_defaults(handler=cmd_pages_authors)
    pages_reply = pages_verbs.add_parser("reply", parents=[common],
                                         help="settle a reply that failed for good: send it again, or dismiss it")
    pages_reply.add_argument("action", choices=("resend", "dismiss"))
    pages_reply.add_argument("reply", type=int, metavar="REPLY-ID", help="as `trk pages --errors` lists it")
    pages_reply.add_argument("--reason", metavar="TEXT", help="dismiss: why it needs no reply, in one line (required)")
    pages_reply.set_defaults(handler=cmd_pages_reply)
    pages_tidy = pages_verbs.add_parser("tidy", parents=[common],
                                        help="resolve the threads of asks already closed, on one page")
    pages_tidy.add_argument("id", help="an item id, or needs-you:<actor> for a person's Needs-you page")
    pages_tidy.add_argument("--dry-run", action="store_true", help="list the threads it would close and change nothing")
    pages_tidy.set_defaults(handler=cmd_pages_tidy)
    pages_tick = pages_verbs.add_parser("tick", parents=[common], help="pull, then flush (the timer's command)")
    pages_tick.set_defaults(handler=cmd_pages_tick)
    pages_timer = pages_verbs.add_parser("timer", parents=[common],
                                         help="the systemd user timer that runs `trk pages tick` every minute")
    pages_timer.add_argument("action", choices=("install", "remove", "status"))
    pages_timer.set_defaults(handler=cmd_pages_timer)
    pages_render = pages_verbs.add_parser("render", parents=[common],
                                          help="render one item's page locally, without network")
    pages_render.add_argument("id", help="an item id, or needs-you:<actor> for a person's Needs-you page")
    pages_render.add_argument("--out", metavar="F", default="-", help="write the HTML to F (default -: stdout)")
    pages_render.set_defaults(handler=cmd_pages_render)
    pages_republish = pages_verbs.add_parser("republish", parents=[common],
                                             help="send an item's whole page again, ignoring the stored hashes (repair)")
    pages_republish.add_argument("id", help="an item id, or needs-you:<actor>")
    pages_republish.set_defaults(handler=cmd_pages_republish)

    _ledger_parsers(verbs, common)

    backup = verbs.add_parser("backup", parents=[common], help="write a consistent copy of the store to PATH")
    backup.add_argument("path", help="the new file; never overwritten")
    backup.set_defaults(handler=cmd_backup)

    restore = verbs.add_parser("restore", parents=[common],
                               help="replace the store with a backup and rotate its epoch (never copy files by hand)")
    restore.add_argument("path", help="a file `trk backup` wrote; read, never changed")
    restore.set_defaults(handler=cmd_restore)

    changes = verbs.add_parser("changes", parents=[common], help="read the change feed")
    changes.add_argument("--since", required=True, type=_count(0), metavar="SEQ", help="exclusive cursor (0: all)")
    changes.add_argument("--root", metavar="ID", help="only rows under this top-level item")
    changes.add_argument("--limit", type=_count(1), default=feed.DEFAULT_LIMIT, metavar="N",
                         help=f"at most N rows (default {feed.DEFAULT_LIMIT})")
    changes.set_defaults(handler=cmd_changes)
    return parser


def _ledger_parsers(verbs, common):
    ledger = verbs.add_parser("ledger", help="a ticket's decision ledger: forks, verdicts, settlements")
    ledger_verbs = ledger.add_subparsers(dest="ledger_verb", required=True, parser_class=Parser)
    add = ledger_verbs.add_parser("add", parents=[common], help="log a fork, contradiction or reward-hacking flag;"
                                  " prints ledger-<n>")
    add.add_argument("id")
    add.add_argument("--kind", required=True, metavar="K", help="fork, contradiction or reward-hack")
    add.add_argument("--decided", metavar="TEXT", help="what was decided, in one line")
    add.add_argument("--alternative", action="append", default=[], metavar="TEXT",
                     help="what else was weighed (repeatable; a fork needs one)")
    add.add_argument("--why", metavar="TEXT", help="why, in one line")
    add.add_argument("--would-have-asked", metavar="TEXT", help="the question you would have asked")
    add.add_argument("--conflicts-with", metavar="REF", help="a contradiction's ADR path or spec:<section>")
    add.add_argument("--file", action="append", default=[], metavar="PATH", help="a file it touches (repeatable)")
    add.add_argument("--sha", metavar="S", help="the commit, 7 to 40 hex chars")
    add.add_argument("--origin", default="builder", metavar="O", help="builder (default) or auditor")
    add.add_argument("--noise", action="store_true", help="auditor: mark the row noise")
    add.add_argument("--repeat-of", metavar="TICKET:N", help="auditor: the same guess in another ticket's ledger")
    add.add_argument("--topic", metavar="TEXT", help="with --repeat-of: the missing ticket-template field")
    add.add_argument("--refers-to", metavar="N", help="the earlier row this one corrects")
    add.set_defaults(handler=cmd_ledger_add)
    listing = ledger_verbs.add_parser("list", parents=[common], help="a ticket's ledger rows with their latest "
                                      "verdict, settlement, promotion and asks")
    listing.add_argument("id")
    listing.set_defaults(handler=cmd_ledger_list)
    judged = ledger_verbs.add_parser("verdict", parents=[common], help="judge a row from an independent session")
    judged.add_argument("id")
    judged.add_argument("n", metavar="N")
    judged.add_argument("verdict", help="sound, risky or wrong; confirmed or refuted on a reward-hack flag")
    judged.add_argument("--note", metavar="TEXT", help="why, in one line (required)")
    judged.add_argument("--sha", metavar="S", help="the commit judged (default: the head sha)")
    judged.set_defaults(handler=cmd_ledger_verdict)
    settle = ledger_verbs.add_parser("settle", parents=[common], help="settle a judged row")
    settle.add_argument("id")
    settle.add_argument("n", metavar="N")
    settle.add_argument("settlement", help="fixed, deferred (files a ticket for the row), accepted or overruled")
    settle.add_argument("--sha", metavar="S", help="fixed: the fix commit")
    settle.add_argument("--ask", metavar="ID", help="the answered engineering ask tied to the row (accepted, "
                        "overruled, and fixed on a contradiction, unless an auditor settles a contradiction "
                        "it raised after its own sound verdict at the fix)")
    settle.add_argument("--adr", metavar="PATH", help="accepted: the ADR amended or superseded because the code wins")
    settle.add_argument("--note", metavar="TEXT", help="one line")
    settle.set_defaults(handler=cmd_ledger_settle)
    audit = ledger_verbs.add_parser("audit", parents=[common], help="record the auditor's pass at the head sha;"
                                    " prints the evidence id; a RED ticket needs its review-brief section first")
    audit.add_argument("id")
    audit.add_argument("--note", required=True, metavar="TEXT", help="what the audit covered, in one line")
    audit.set_defaults(handler=cmd_ledger_audit)
    promote = ledger_verbs.add_parser("promote", parents=[common], help="record a row's promotion to an ADR (a person)")
    promote.add_argument("id")
    promote.add_argument("n", metavar="N")
    promote.add_argument("--adr", required=True, metavar="PATH", help="the ADR it became")
    promote.set_defaults(handler=cmd_ledger_promote)


def _placement(parser):
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--first", dest="where", action="store_const", const="first", help="first among its siblings")
    where.add_argument("--last", dest="where", action="store_const", const="last", help="last among its siblings (default)")
    where.add_argument("--before", metavar="ID", help="just before sibling ID")
    where.add_argument("--after", metavar="ID", help="just after sibling ID")


def _where(args):
    """(where, anchor) from the placement flags."""
    if args.before:
        return "before", args.before
    if args.after:
        return "after", args.after
    return args.where or "last", None


def _lease(parser, help_text):
    parser.add_argument("--lease", type=_duration, metavar="DUR", help=f"{help_text}: 45m, 2h, 1h30m")


def _duration(text):
    """An argparse type: lease minutes from a duration."""
    try:
        return claims.parse_lease(text)
    except TrkError as exc:
        raise argparse.ArgumentTypeError(exc.message) from None


def _count(minimum):
    """An argparse type: an integer >= minimum."""

    def parse(text):
        try:
            value = int(text)
        except ValueError:
            value = None
        if value is None or value < minimum:
            raise argparse.ArgumentTypeError(f"expected an integer >= {minimum}, got {text!r}")
        return value

    return parse


def cmd_init(args):
    st = store.create(args.prefix, args.me, args.display)
    st.close()
    if args.json:
        _print_json({"schema": 1, "store": str(st.dir), "default_actor": args.me, "default_owner": args.me,
                     "prefix": args.prefix})
    else:
        print(f"created store {st.dir} (default actor {args.me}, prefix {args.prefix})")


def _read_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise usage(f"cannot read {path}: {exc}") from exc


def _is_source(source):
    return source == "-" or (source.startswith("@") and len(source) > 1)


def _read_source(source):
    """A body from `@file` or `-` (stdin)."""
    return sys.stdin.read() if source == "-" else _read_file(source[1:])


def _section_flags(flags):
    """[(handle, body)] from `--section handle=@file|handle=-`, read before any write lock is taken."""
    result, used_stdin = [], False
    for flag in flags:
        handle, sep, source = flag.partition("=")
        if not sep or not _is_source(source):
            raise usage(f"--section takes handle=@file or handle=-, got {flag!r}")
        sections.check_handle(handle)
        if source == "-":
            if used_stdin:
                raise usage("only one --section can read stdin")
            used_stdin = True
        result.append((handle, _read_source(source)))
    return result


# One-line free-text flags (by argparse dest) that also take `@file` or `-`, like `--question`.
TEXT_FLAGS = ("note", "outcome", "decided", "why", "alternative", "judgment", "reason")


def _text_flags(args):
    """Replace each given one-line text flag's `@file` / `-` by the text read, before any write lock is taken.

    The text is stripped of surrounding whitespace, so a file's trailing newline is not a second line; the
    flag's own checks (one line, length cap) then apply to what was read. Only one flag may read stdin.
    """
    used_stdin = False
    for dest in TEXT_FLAGS:
        value = getattr(args, dest, None)
        many = isinstance(value, list)
        read = []
        for item in (value if many else [value]):
            if isinstance(item, str) and _is_source(item):
                if item == "-":
                    if used_stdin:
                        raise usage("only one flag can read stdin")
                    used_stdin = True
                item = _read_source(item).strip()
            read.append(item)
        if value is not None:
            setattr(args, dest, read if many else read[0])


def _body_flag(flag, source):
    """The body a `--flag @file|-` names, read before any write lock is taken; None when not given."""
    if source is None:
        return None
    if not _is_source(source):
        raise usage(f"{flag} takes @file or -, got {source!r}")
    return _read_source(source)


def cmd_ask(args):
    question = _body_flag("--question", args.question)
    where, anchor = _where(args)
    _write_item(args, lambda tx: asks.open_ask(tx, args.parent, args.to, args.title, question, urgent=args.urgent,
                                               where=where, anchor=anchor, ledger=args.ledger))


def cmd_readdress(args):
    _write_item(args, lambda tx: asks.readdress(tx, args.id, args.to))


def cmd_waiting(args):
    st = store.open_store()
    try:
        if args.all:
            me = None
        elif args.me:
            me = store.require_actor(st.conn, args.me)
        else:
            me = store.resolve_actor(st, args.as_actor, guard=False)
        now = clock.now()
        listed = [asks.waiting_json(st.conn, row, now) for row in asks.waiting(st.conn, me, args.root, args.client, now)]
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "asks": listed})
        return
    for ask in listed:
        urgent = "! " if ask["urgent"] else ""
        parent = ask["parent"]
        print(f"{ask['id']}  {urgent}{_addressed(ask)}  {parent['id']} {parent['title']}  {ask['title']}"
              f"  {ask['urgency']['reason']}")


def cmd_answer(args):
    if args.from_comment is not None:
        _write_item(args, lambda tx: asks.answer_from_comment(tx, args.id, args.from_comment, args.outcome))
        return
    body = _body_flag("--answer", args.answer)
    _write_item(args, lambda tx: asks.answer(tx, args.id, args.outcome, body))


def cmd_reply(args):
    body = _body_flag("--body", args.body)
    _write_item(args, lambda tx: asks.reply(tx, args.id, body))


def cmd_withdraw(args):
    _write_item(args, lambda tx: asks.withdraw(tx, args.id, args.reason))


def cmd_new(args):
    section_bodies = _section_flags(args.section)
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            where, anchor = _where(args)
            item_id = items.create(
                tx, args.kind, args.title, form=args.form, subtype=args.subtype, parent=args.parent,
                where=where, anchor=anchor, urgent=args.urgent, jira_url=args.jira, repos=args.repo,
                owner=args.owner, section_bodies=section_bodies, criterion_texts=args.criterion, client=args.client,
            )
        _print_item(st, item_id, args.json, plain=item_id)
    finally:
        st.close()


def cmd_edit(args):
    changes = (args.title, args.urgent, args.jira, args.owner, args.kind, args.form, args.client, args.archive)
    flags = (args.no_jira, args.no_owner, args.add_repo, args.remove_repo)
    if all(value is None for value in changes) and not any(flags):
        raise usage("nothing to edit")
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            item_id = items.edit(
                tx, args.id, title=args.title, urgent=args.urgent, jira_url=args.jira, clear_jira=args.no_jira,
                add_repos=args.add_repo, remove_repos=args.remove_repo, owner=args.owner, clear_owner=args.no_owner,
                kind=args.kind, form=args.form, client=None if args.client == "none" else args.client,
                clear_client=args.client == "none", archive=None if args.archive == "none" else args.archive,
                clear_archive=args.archive == "none",
            )
        _print_item(st, item_id, args.json, plain=item_id)
    finally:
        st.close()


def cmd_repos_absolutize(args):
    """Relative repo rows of old items made absolute: placed at ~/projects/<repo> when that is a git checkout,
    otherwise left and listed. One `item.updated` row per item changed; --dry-run writes nothing."""
    root = os.path.expanduser("~/projects")
    st = store.open_store()
    try:
        changes, unresolved = items.plan_absolutize(st.conn, root)
        if changes and not args.dry_run:
            actor = store.resolve_actor(st, args.as_actor)
            with st.transaction(actor, store.resolve_session(args.session)) as tx:
                items.apply_absolutize(tx, changes)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "dry_run": args.dry_run, "changed": changes, "unresolved": unresolved})
        return
    verb = "would change" if args.dry_run else "changed"
    for change in changes:
        print(f"{verb} {change['id']}: {', '.join(change['old'])} -> {', '.join(change['new'])}")
    for left in unresolved:
        print(f"cannot place {left['item']}: {left['repo']} (no checkout at {os.path.join(root, left['repo'])})")


def cmd_move(args):
    where, anchor = _where(args)
    _write_item(args, lambda tx: items.move(tx, args.id, parent=args.parent, top=args.top, where=where, anchor=anchor))


def cmd_close(args):
    _write_item(args, lambda tx: items.close(tx, args.id, args.resolution, args.outcome, by=args.by, of=args.of))


def cmd_reopen(args):
    _write_item(args, lambda tx: items.reopen(tx, args.id, args.reason, args.to_stage))


def cmd_spec(args):
    _write_item(args, lambda tx: items.set_spec(tx, args.id, args.state, args.reason))


def cmd_link(args):
    _write_edge(args, edges.link)


def cmd_unlink(args):
    _write_edge(args, edges.unlink)


def _write_edge(args, change):
    """Print the edge as stored (relates puts the lower id first), or its JSON."""
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            src, edge_type, dst = change(tx, args.src, args.type, args.dst)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "edge": {"src": src, "type": edge_type, "dst": dst}})
    else:
        print(f"{src} {edge_type} {dst}")


def _write_item(args, change):
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            item_id = change(tx)
        _print_item(st, item_id, args.json, plain=item_id)
    finally:
        st.close()


def cmd_tree(args):
    st = store.open_store()
    try:
        outline = items.outline(st.conn, args.id, include_closed=args.all)
        if args.json:
            _print_json({"schema": 1, "items": [items.to_json(st.conn, row) for row, _ in outline]})
            return
        for row, level in outline:
            item = items.to_json(st.conn, row)
            mark = ready.marker(item)
            marker = f"  [{mark}]" if mark else ""
            closed = f"  [{item['resolution']}] {item['outcome']}" if item["status"] == "closed" else ""
            suffix = f"  → {_addressed(item)}" if item["form"] == "ask" else _owner_suffix(item)
            stage = f" [{item['stage']}]" if item["stage"] else ""
            print(f"{'  ' * level}{item['id']}  {_kind_label(item)}{stage}  {item['title']}{marker}{suffix}{closed}")
    finally:
        st.close()


def cmd_ready(args):
    st = store.open_store()
    try:
        rows = ready.frontier(st.conn, args.root, args.owner, args.limit, args.stage, args.client)
        listed = [items.to_json(st.conn, row) for row in rows]
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "items": listed})
        return
    for item in listed:
        urgent = "  urgent" if item["urgent"] else ""
        print(f"{_item_line(item)}{urgent}{_owner_suffix(item)}")


def _claim_session(args):
    session = store.resolve_session(args.session)
    if session is None:
        raise usage("claims need --session <uuid> (or $TRK_SESSION)")
    return session


def cmd_claim(args):
    if (args.id is None) != args.next:
        raise usage("claim takes an item id or --next, not both" if args.next else "claim takes an item id or --next")
    if not args.next and (args.root or args.owner):
        raise usage("--root and --owner go with --next")
    if args.next and args.force:
        raise usage("--force goes with an item id, not --next")
    session = _claim_session(args)
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, session) as tx:
            if args.next:
                item_id = claims.take_next(tx, session, args.root, args.owner, args.lease)
            else:
                item_id = claims.take(tx, args.id, session, args.lease, args.force)
            if args.worktree is not None and item_id is not None:
                worktrees.record(tx, item_id, args.worktree)
        item = items.to_json(st.conn, items.get(st.conn, item_id)) if item_id is not None and args.json else None
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "claimed": item})
    elif item_id is not None:
        print(item_id)


def _named_actor(st, explicit):
    """The actor `--as` or $TRK_ACTOR names, registered; None when neither is given."""
    named = explicit or os.environ.get("TRK_ACTOR")
    return store.require_actor(st.conn, named) if named else None


def cmd_claim_renew(args):
    session = _claim_session(args)
    st = store.open_store()
    try:
        renewed, not_due = claims.renew(st, _named_actor(st, args.as_actor), session, args.lease)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "renewed": renewed, "not_due": not_due})
        return
    for label, found in (("renewed", renewed), ("not due", not_due)):
        for claim in found:
            print(f"{label} {claim['item']} until {claim['lease_until']} ({claim['lease_minutes']}m lease)")


def cmd_claim_release(args):
    session = _claim_session(args)
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, session) as tx:
            released = claims.release(tx, session, args.id)
    finally:
        st.close()
    _print_ids(args, "released", released)


def cmd_sweep(args):
    st = store.open_store(sweep=False)
    try:
        expired = claims.sweep(st, store.resolve_actor(st, args.as_actor))
    finally:
        st.close()
    _print_ids(args, "expired", expired)


def _print_ids(args, key, item_ids):
    if args.json:
        _print_json({"schema": 1, key: item_ids})
    else:
        for item_id in item_ids:
            print(item_id)


def cmd_claims(args):
    st = store.open_store()
    try:
        item_id = items.get(st.conn, args.item)["id"] if args.item is not None else None
        listing = claims.listing(st.conn, args.session, item_id)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "claims": listing})
        return
    for claim in listing:
        print(f"{claim['item']}  {claim['session']}  {claim['actor']}  {claim['lease_until']}")


def _item_line(item):
    return f"{item['id']}  {_kind_label(item)}  {item['title']}"


def _owner_suffix(item):
    return f"  @{item['effective_owner']}" if item["effective_owner"] else ""


def cmd_actor_add(args):
    _write_actor(args, lambda tx: actors.add(tx, args.id, args.kind, args.display))
    if args.kind == "human" and not args.json:
        print("Their first page comment links to this person when the display name matches.", file=sys.stderr)


def cmd_actor_edit(args):
    _write_actor(args, lambda tx: actors.edit(tx, args.id, args.display))


def cmd_actor_link(args):
    _write_actor(args, lambda tx: actors.link(tx, args.id, args.service_user))


def _write_actor(args, change):
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            change(tx)
        row = actors.get(st.conn, args.id)
        if args.json:
            _print_json({"schema": 1, "actor": actors.to_json(row)})
        else:
            print(row["id"])
    finally:
        st.close()


def cmd_actors(args):
    st = store.open_store()
    try:
        listing = actors.listing(st.conn)
        if args.json:
            _print_json({"schema": 1, "actors": listing})
            return
        for actor in listing:
            print(f"{actor['id']}  {actor['kind']}  {actor['display']}")
    finally:
        st.close()


def cmd_roles(args):
    st = store.open_store()
    try:
        listing = roles.listing(st.conn)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "roles": listing})
        return
    for role in listing:
        print(_role_line(role))
    print(PARENT_ROLE_NOTE)


def _role_line(role):
    return f"{role['role']} → {role['actor']} ({role['display']})"


def cmd_roles_set(args):
    role = _write_then_read(args, lambda tx: roles.set_role(tx, args.role, args.actor),
                            lambda st: roles.entry(st.conn, args.role))
    if args.json:
        _print_json({"schema": 1, "role": role})
    else:
        print(_role_line(role))


def cmd_config(args):
    st = store.open_store()
    try:
        settings = config.listing(st)
    finally:
        st.close()
    _print_config(args, settings)


def cmd_config_set(args):
    settings = _write_then_read(args, lambda tx: config.set_value(tx, args.key, args.value), config.listing)
    _print_config(args, settings)


def _write_then_read(args, change, read):
    """One write as the resolved actor and session, then `read(store)` before the store closes."""
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            change(tx)
        return read(st)
    finally:
        st.close()


def _print_config(args, settings):
    if args.json:
        _print_json({"schema": 1, "config": settings})
        return
    for name, value in settings.items():
        print(f"{name}  {value}")


def cmd_worktree_set(args):
    _write_item(args, lambda tx: worktrees.record(tx, args.id, args.path, args.branch))


def cmd_worktree_clear(args):
    _write_item(args, lambda tx: worktrees.clear(tx, args.id))


def cmd_where(args):
    st = store.open_store()
    try:
        tree, owners = worktrees.where(st.conn, args.path)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "path": os.path.realpath(args.path), "tree": tree, "items": owners})
        return
    print(f"tree: {tree}")
    for owner in owners:
        claim = owner["claim"]
        held = f"claimed by {claim['actor']}, session {claim['session']}, lease until {claim['lease_until']}" \
            if claim else "unclaimed"
        print(f"{owner['id']}  {owner['kind']}  {owner['title']}  [{owner['status']}]")
        print(f"  root: {owner['root']}  {owner['root_title']}")
        print(f"  owner: {owner['effective_owner'] or '-'}  branch: {owner['branch'] or '-'}  {held}")
        if owner["effective_archive"]:
            print(f"  archive: {owner['effective_archive']}" + ("" if owner["archive"] else " (inherited)"))


def cmd_show(args):
    st = store.open_store()
    try:
        _print_item(st, args.id, args.json, section_bodies=not args.no_sections)
    finally:
        st.close()


def cmd_section_set(args):
    body = _read_file(args.file) if args.file is not None else sys.stdin.read()
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            item_id = items.set_section(tx, args.id, args.handle, body)
        _print_item(st, item_id, args.json, plain=item_id)
    finally:
        st.close()


def cmd_section_rm(args):
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            item_id = items.remove_section(tx, args.id, args.handle)
        _print_item(st, item_id, args.json, plain=item_id)
    finally:
        st.close()


def cmd_section_get(args):
    st = store.open_store()
    try:
        row = items.get(st.conn, args.id)
        item_id = row["id"]
        body = sections.read(st.conn, row, args.handle)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "item": item_id, "handle": args.handle, "body": body})
    else:
        sys.stdout.write(body)


def cmd_comment_reply(args):
    body = _body_flag("--body", args.body)
    _write_comment(args, lambda tx: comments.reply(tx, args.comment, body))


def cmd_comment_ack(args):
    _write_comment(args, lambda tx: comments.acknowledge(tx, args.comment, args.ask))


def _write_comment(args, change):
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            comment_id = change(tx)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "comment": comment_id})
    else:
        print(comment_id)


def cmd_handoff_add(args):
    body = _read_file(args.file) if args.file is not None else sys.stdin.read()
    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            handoff_id = handoffs.add(tx, args.id, body)
        record = handoffs.get(st.conn, handoff_id)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "item": record["item"], "handoff": record})
    else:
        print(record["item"])


def cmd_handoff_show(args):
    st = store.open_store()
    try:
        if args.all:
            item_id, records = handoffs.listing(st.conn, args.id)
        else:
            record = handoffs.latest(st.conn, args.id)
    finally:
        st.close()
    if args.all and args.json:
        _print_json({"schema": 1, "item": item_id, "handoffs": records})
    elif args.all:
        sys.stdout.write("\n".join(_human_handoff(record) for record in records))
    elif args.json:
        _print_json({"schema": 1, "item": record["item"], "handoff": record})
    else:
        sys.stdout.write(record["body"])


def _human_handoff(record):
    """A header line naming the handoff, a blank line, then its body ending in one newline."""
    session = f"  session {record['session']}" if record["session"] else ""
    body = record["body"].rstrip("\n")
    return f"## handoff {record['id']}  {record['at']}  {record['actor']}{session}\n\n{body}\n"


def cmd_criterion_add(args):
    where, anchor = _where(args)
    _write_criterion(args, lambda criteria, tx: criteria.add(tx, args.id, args.text, where, anchor))


def cmd_criterion_edit(args):
    _write_criterion(args, lambda criteria, tx: criteria.edit(tx, args.id, args.handle, args.text))


def cmd_criterion_retire(args):
    _write_criterion(args, lambda criteria, tx: criteria.retire(tx, args.id, args.handle, args.reason))


def _write_criterion(args, change):
    """Print the criterion's handle, or its JSON as `criteria` lists it."""
    from . import criteria  # the gates modules load only for the verbs that need them

    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            handle = change(criteria, tx)
        listed = criteria.listing(st.conn, items.get(st.conn, args.id)["id"])
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "criterion": next(c for c in listed if c["handle"] == handle)})
    else:
        print(handle)


def cmd_criteria(args):
    from . import criteria

    st = store.open_store()
    try:
        item_id, listed, text = criteria.read(st.conn, args.id)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "item": item_id, "criteria": listed})
    else:
        sys.stdout.write(text)


def cmd_advance(args):
    from . import stages

    _write_item(args, lambda tx: stages.advance(tx, args.id, args.judgment, args.outcome))


def cmd_skip(args):
    from . import stages

    _write_item(args, lambda tx: stages.skip(tx, args.id, args.stage, args.reason))


def cmd_reopen_stage(args):
    from . import stages

    _write_item(args, lambda tx: stages.reopen_stage(tx, args.id, args.stage, args.reason))


def cmd_blast_radius(args):
    from . import blast

    notes = []
    _write_evidence(args, lambda evidence, tx: blast.record(tx, args.id, args.sha, args.base, args.repo, notes),
                    text=lambda row: row["detail"]["lane"])
    for note in notes:
        print(f"note: {note}", file=sys.stderr)


def cmd_review_record(args):
    from . import blast

    _write_evidence(args, lambda evidence, tx: blast.review_record(tx, args.id, args.ref, args.sha, args.outcome,
                                                                   args.note))


def cmd_evidence_add(args):
    _write_evidence(args, lambda evidence, tx: evidence.add(
        tx, args.id, args.kind, args.gate, criterion=args.criterion, ref=args.ref, sha=args.sha,
        verdict=args.verdict, note=args.note))


def cmd_evidence_retract(args):
    _write_evidence(args, lambda evidence, tx: evidence.retract(tx, args.id, args.evidence, args.note))


def _write_evidence(args, change, text=None):
    """Print the new row's id (or `text(row)`), or its JSON as `evidence` lists it."""
    from . import evidence

    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            evidence_id = change(evidence, tx)
        row = evidence.get(st.conn, evidence_id) if args.json or text else None
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "evidence": row})
    else:
        print(text(row) if text else evidence_id)


def cmd_evidence(args):
    from . import evidence

    st = store.open_store()
    try:
        item_id, listed = evidence.listing(st.conn, args.id, args.gate)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "item": item_id, "evidence": listed})
        return
    for row in listed:
        print(_evidence_line(row))


def _evidence_line(row):
    """id, gate, kind, criterion, verdict, sha, ref, whether it counts, then the note."""
    parts = [row["id"], row["gate"], row["kind"]]
    if row["criterion"]:
        parts.append(f"{row['criterion']} (v{row['criterion_version']})")
    parts += [row["verdict"], row["sha"] and row["sha"][:7], row["ref"]]
    parts.append("counts" if row["counts"] else f"not counted ({row['reason']})")
    return "  ".join(part for part in parts if part) + f"  — {row['note']}"


def cmd_pages(args):
    from . import pages  # the page modules load only for the verbs that need them

    st = store.open_store()
    try:
        set_up = pages.is_set_up(st)
        rows = pages.listing(st.conn, dirty_only=args.dirty, errors_only=args.errors) if set_up else []
        failed = pages.failed_replies(st.conn) if set_up else []
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "set_up": set_up, "pages": rows, "failed_replies": failed})
    elif not set_up:
        print("publishing is not set up: run `trk pages setup` to publish pages")
    else:
        for row in rows:
            state = ("dirty, due " + (row["due_at"] or "now")) if row["dirty"] else "clean"
            parts = [row["item"], row["kind"], row["slug"] or "-", f"v{row['version']}" if row["version"] else "-",
                     row["published_at"] or "unpublished", state, row["url"] or "-",
                     *([f"error: {row['last_error']}"] if row["last_error"] else [])]
            print("  ".join(parts) + f"  {row['title']}")
        for reply in failed if args.errors else []:
            print(f"reply {reply['reply']} failed  {reply['item'] or '-'}  {reply['slug']}  "
                  f"comment {reply['comment']}  "
                  f"ask {reply['ask'] or '-'}  {reply['reason'] or 'refused'}  "
                  f"(`trk pages reply resend|dismiss {reply['reply']}`)")


def cmd_pages_reply(args):
    from . import pages

    if args.action == "dismiss":
        reason = (args.reason or "").strip()
        if not reason or "\n" in reason:
            raise usage("dismissing a failed reply needs --reason \"<one line>\": why it needs no reply")
    elif args.reason is not None:
        raise usage("--reason goes with dismiss")
    st = store.open_store()
    try:
        done = pages.settle_reply(st, args.reply, args.action, args.reason)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, **done})
    else:
        print(f"reply {done['reply']} on comment {done['comment']}: "
              + ("pending, sent by the next pull" if args.action == "resend" else "dismissed"))


def cmd_pages_setup(args):
    from . import pages, renewal

    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        done = pages.setup(st, actor, backend=args.backend, base_url=args.base_url,
                           token_file=args.token_file or pages.DEFAULT_TOKEN_FILE, expires=args.expires,
                           home=args.home, team=args.team, folder=args.dir)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, **done})
    else:
        if done["token_file"]:
            print(f"publishing set up: {done['backend']} at {done['base_url']}, team {done['team']}, "
                  f"token {done['token_file']} (expires {done['expires']}), home {done['home']}")
        else:
            served = f", served at {done['base_url']}" if done["base_url"] else ""
            print(f"publishing set up: {done['backend']} in {done['dir']}{served}, home {done['home']}")
        renewal_ask = done["renewal_ask"]
        if renewal_ask is None:
            return
        if renewal_ask["answered"]:
            print(f"answered {renewal_ask['id']}: Token renewed")
        elif renewal_ask["reason"] == renewal.EXPIRES_SOON:
            print(f"{renewal_ask['id']} left open: the token expires {done['expires']}, "
                  f"within {renewal.WINDOW.days} days")
        else:
            print(f"{renewal_ask['id']} left open: {actor} may not answer it; its addressee or any human can "
                  f"answer with `trk answer {renewal_ask['id']}`")


def cmd_pages_flush(args):
    from . import flusher, pages

    st = store.open_store()
    try:
        pages.require_set_up(st)
        if args.wait and args.now:
            raise usage("--now and --wait don't combine: --wait sends each page when it falls due")
        only = {pages.page_key(st.conn, raw) for raw in args.item} if args.item else None
        if args.wait:  # the detached flusher: like the timer, it reads the token file, never the override
            report = flusher.flush_wait(st, pages.client(st, foreground=False), only=only)
        else:
            report = flusher.flush(st, pages.client(st, foreground=True), now_all=args.now, only=only)
    finally:
        st.close()
    _print_flush_report(args, report)


def cmd_pages_tick(args):
    """The timer's command: the comment pull, then `flush` without --wait."""
    from . import flusher, pages, pull

    st = store.open_store()
    try:
        pages.require_set_up(st)
        client = pages.client(st, foreground=False)
        pulled = pull.pull(st, client)
        report = flusher.flush(st, client)
    finally:
        st.close()
    if not args.json:
        _print_pull_report(pulled, renewal=False)
    _print_flush_report(args, {**report, "pull": pulled})


def cmd_pages_pull(args):
    from . import pages, pull

    st = store.open_store()
    try:
        pages.require_set_up(st)
        report = pull.pull(st, pages.client(st, foreground=True))
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, **report})
    else:
        _print_pull_report(report, renewal=True)


def cmd_pages_tidy(args):
    from . import pages, pull

    st = store.open_store()
    try:
        pages.require_set_up(st)
        report = pull.tidy(st, pages.client(st, foreground=True), pages.page_key(st.conn, args.id),
                           dry_run=args.dry_run)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "dry_run": args.dry_run, **report})
        return
    if report["held"]:
        print(f"held: the page backend rejected the token; page reads and writes wait for `trk pages setup` "
              f"with a new one ({report['renewal_ask'] or 'no renewal ask'})")
    verb = "would close" if args.dry_run else "closing"
    print(f"{report['page']}: {verb} {len(report['closing'])} thread(s), "
          f"{len(report['skipped'])} left alone")
    for entry in report["skipped"]:
        print(f"left alone {entry['comment']}: {entry['reason']}")
    for state in ("sent", "failed", "pending"):
        if report["replies"][state]:
            print(f"replies {state}: {' '.join(report['replies'][state])}")
    if report["resolved"]:
        print(f"resolved: {' '.join(report['resolved'])}")
    for error in report["errors"]:
        print(f"error on {error['item']}: {error['error']}", file=sys.stderr)


def _print_pull_report(report, *, renewal):
    """The pull's lines; the token check's are left to the flush report when one follows."""
    if renewal and report["held"]:
        print(f"held: the page backend rejected the token; page reads and writes wait for `trk pages setup` "
              f"with a new one ({report['renewal_ask'] or 'no renewal ask'})")
    if renewal and report.get("renewal_ask_error"):
        print(f"renewal ask not opened: {report['renewal_ask_error']}", file=sys.stderr)
    for entry in report["comments"]:
        print(f"comment {entry['comment']} on {entry['slug']} ({entry['handle'] or 'no handle'}): "
              f"{entry['disposition']}" + (f" {entry['ask']}" if entry["ask"] else ""))
    for state in ("sent", "failed", "pending"):
        if report["replies"][state]:
            print(f"replies {state}: {' '.join(report['replies'][state])}")
    if report["resolved"]:
        print(f"resolved: {' '.join(report['resolved'])}")
    for error in report["errors"]:
        print(f"error on {error['item'] or 'the comment activity log'}: {error['error']}", file=sys.stderr)


def cmd_pages_authors(args):
    from . import pull

    st = store.open_store()
    try:
        rows = pull.authors(st)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "authors": rows})
        return
    for row in rows:
        print(f"{row['author_id']}  {row['display'] or '-'}  {row['actor'] or 'not linked'}")


def _print_flush_report(args, report):
    from . import flusher, renewal

    if args.json:
        _print_json({"schema": 1, **report})
        return
    if report["held"]:
        print(f"held: the page backend rejected the token; page writes wait for `trk pages setup` "
              f"with a new one ({report['renewal_ask'] or 'no renewal ask'})")
    elif report["renewal_ask"]:
        print(f"renewal ask: {report['renewal_ask']} (the token expires within {renewal.WINDOW.days} days)")
    if report.get("renewal_ask_error"):
        print(f"renewal ask not opened: {report['renewal_ask_error']}", file=sys.stderr)
    for outcome in flusher.OUTCOMES:
        if report[outcome]:
            print(f"{outcome}: {' '.join(report[outcome])}")


def cmd_pages_republish(args):
    from . import flusher, pages

    st = store.open_store()
    try:
        pages.require_set_up(st)
        item_id = pages.page_key(st.conn, args.id)
        sent = flusher.republish(st, pages.client(st, foreground=True), item_id)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, **sent})
    else:
        print(f"republished {sent['item']}: {sent['slug']} version {sent['version']}")


def cmd_pages_timer(args):
    from . import timer

    done = {"install": timer.install, "remove": timer.remove, "status": timer.status}[args.action]()
    if args.json:
        _print_json({"schema": 1, **done})
        return
    if args.action == "remove":
        print(f"removed: {' '.join(done['removed']) or 'nothing'}")
    print(f"timer {timer.TIMER} in {done['unit_dir']}: "
          f"{'installed' if done['installed'] else 'not installed'}, {done['enabled']}, {done['active']}")
    if args.action == "install":
        print(f"runs every {timer.PERIOD}: {done['exec_start']}" + (f" (TRK_HOME={done['trk_home']})"
                                                                     if done["trk_home"] else ""))


def cmd_pages_render(args):
    from . import needs_you, render  # the page modules load only for the verbs that need them

    st = store.open_store()
    try:
        item_id = needs_you.resolve_key(st.conn, args.id)
        if item_id is not None:
            page = needs_you.render_page(st.conn, needs_you.actor_of(item_id))
        else:
            item_id = items.get(st.conn, args.id)["id"]
            page = render.render(st.conn, item_id)
    finally:
        st.close()
    if args.out != "-":
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(page)
        except OSError as exc:
            raise usage(f"cannot write {args.out}: {exc.strerror or exc}") from exc
    if args.json:
        _print_json({"schema": 1, "item": item_id, "render_version": render.RENDER_VERSION, "out": args.out,
                     "bytes": len(page.encode("utf-8")), **({"html": page} if args.out == "-" else {})})
    elif args.out == "-":
        sys.stdout.write(page)


def cmd_ledger_add(args):
    _write_ledger(args, lambda ledger, tx: ledger.add(
        tx, args.id, args.kind, decided=args.decided, alternatives=args.alternative, why=args.why,
        would_have_asked=args.would_have_asked, conflicts_with=args.conflicts_with, files=args.file, sha=args.sha,
        origin=args.origin, noise=args.noise, repeat_of=args.repeat_of, topic=args.topic, refers_to=args.refers_to))


def cmd_ledger_verdict(args):
    _write_ledger(args, lambda ledger, tx: ledger.verdict(tx, args.id, args.n, args.verdict, args.note, args.sha))


def cmd_ledger_settle(args):
    _write_ledger(args, lambda ledger, tx: ledger.settle(tx, args.id, args.n, args.settlement, sha=args.sha,
                                                         ask=args.ask, adr=args.adr, note=args.note))


def cmd_ledger_promote(args):
    _write_ledger(args, lambda ledger, tx: ledger.promote(tx, args.id, args.n, args.adr))


def _write_ledger(args, change):
    """Print the row's handle, or its JSON as `ledger list` lists it."""
    from . import ledger

    st = store.open_store()
    try:
        actor = store.resolve_actor(st, args.as_actor)
        with st.transaction(actor, store.resolve_session(args.session)) as tx:
            handle = change(ledger, tx)
        listed = ledger.listing(st.conn, args.id)[1] if args.json else None
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "ledger": next(row for row in listed if row["handle"] == handle)})
    else:
        print(handle)


def cmd_ledger_audit(args):
    from . import ledger

    _write_evidence(args, lambda evidence, tx: ledger.audit(tx, args.id, args.note))


def cmd_ledger_list(args):
    from . import ledger

    st = store.open_store()
    try:
        item_id, listed = ledger.listing(st.conn, args.id)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "item": item_id, "ledger": listed})
        return
    for row in listed:
        print(ledger.line(row))


def cmd_backup(args):
    st = store.open_store()
    try:
        path, head = store.backup(st, args.path)
    finally:
        st.close()
    if args.json:
        _print_json({"schema": 1, "backup": str(path), "head": head})
    else:
        print(path)


def cmd_restore(args):
    path, head, epoch = store.restore(args.path)
    if args.json:
        _print_json({"schema": 1, "restored": str(path), "head": head, "epoch": epoch})
    else:
        print(f"restored {path}: head {head}, epoch {epoch}")


def cmd_changes(args):
    st = store.open_store()
    try:
        response = feed.changes(st.conn, args.since, args.root, args.limit)
    finally:
        st.close()
    if args.json:
        _print_json(response)
    else:
        for row in response["changes"]:
            print(_human_change(row))


def _human_change(row):
    """seq, at, change, then the item (or the actor, role or setting a row is about) and its title, then who wrote it."""
    data = row["data"]
    subject = row["item"] or data.get("id") or data.get("role") or data.get("key")
    parts = [row["seq"], row["at"], row["change"], subject, row["title"], f"by {row['actor']}"]
    return "  ".join(str(part) for part in parts if part is not None)


def _print_item(st, item_id, as_json, plain=None, section_bodies=True):
    item = items.to_json(st.conn, items.get(st.conn, item_id), section_bodies=section_bodies)
    if as_json:
        _print_json({"schema": 1, "item": item})
    elif plain is not None:
        print(plain)
    else:
        print(_human_item(item, section_bodies))


def _kind_label(item):
    kind = item["kind"] + (f"/{item['form']}" if item["form"] else "")
    return kind + (f" ({item['subtype']})" if item.get("subtype") else "")


# How an edge reads from the item being shown.
_EDGE_LABEL = {
    ("out", "blocks"): "blocks", ("in", "blocks"): "blocked by",
    ("out", "supersedes"): "supersedes", ("in", "supersedes"): "superseded by",
    ("out", "relates"): "relates",
}


def _addressed(ask):
    return f"{ask['addressee_role']} ({ask['addressee'] or '-'})"


def _human_item(item, section_bodies=True):
    lines = [f"{item['id']}  {_kind_label(item)}  {item['title']}"]
    lines.append(f"  status: {item['status']}" + (f" ({item['resolution']})" if item["resolution"] else ""))
    if item["parent"]:
        lines.append(f"  parent: {item['parent']}")
    lines.append(f"  root: {item['root']}  depth: {item['depth']}")
    lines.append(f"  owner: {item['effective_owner'] or '-'}" + ("" if item["owner"] else " (inherited)" if item["effective_owner"] else ""))
    if item["effective_client"]:
        lines.append(f"  client: {item['effective_client']}" + ("" if item["client"] else " (inherited)"))
    if item["form"] == "ask":
        lines.append(f"  to: {_addressed(item)}")
        lines.append(f"  asker: {item['asker']}")
        if item["replies"] and item["status"] == "open":
            latest = item["replies"][-1]
            lines.append(f"  replied: {len(item['replies'])}, latest {latest['comment']} by {latest['actor']}"
                         f" (awaiting confirmation: trk answer {item['id']} --from-comment {latest['comment']})")
    if item["urgent"]:
        lines.append("  urgent")
    if item["spec_state"]:
        lines.append(f"  spec: {item['spec_state']}")
    if item["stage"]:
        from .stages import STAGES

        pipeline = " → ".join(f"[{stage}]" if stage == item["stage"] else stage for stage in STAGES)
        lane = f"  lane: {item['lane']}" if item["lane"] else ""
        lines.append(f"  stage: {item['stage']}  ({pipeline}){lane}")
    if item["gate"]["next"]:
        gate = item["gate"]
        lines.append(f"  next: {gate['next']} — {len(gate['missing'])} missing")
        lines += [f"    missing: {_need_line(need)}" for need in gate["missing"]]
    lines.append("  ready: yes" if item["ready"] else f"  ready: no ({', '.join(item['why_not'])})")
    if item["blocked"]:
        lines.append("  blocked")
    if item["claim"]:
        claim = item["claim"]
        lines.append(f"  claimed: {claim['actor']}, session {claim['session']}, lease until {claim['lease_until']}")
    if item["handoff"]:
        handoff = item["handoff"]
        lines.append(f"  handoff: {handoff['at']}  {handoff['actor']}  {handoff['first_line']}"
                     f"  (trk handoff show {item['id']})")
    if item["outcome"]:
        lines.append(f"  outcome: {item['outcome']}")
    if item["jira_url"]:
        lines.append(f"  jira: {item['jira_url']}")
    for repo in item["repos"]:
        lines.append(f"  repo: {repo}")
    if item["worktree"]:
        tree = item["worktree"]
        lines.append(f"  worktree: {tree['path']} ({tree['branch'] or 'no branch'})"
                     + (f"  session {tree['session']}" if tree["session"] else ""))
    if item["effective_archive"]:
        lines.append(f"  archive: {item['effective_archive']}" + ("" if item["archive"] else " (inherited)"))
    lines.append(f"  created: {item['created_at']}  updated: {item['updated_at']}")
    if item["children"]:
        lines.append("  children:")
        for child in item["children"]:
            closed = "" if child["status"] == "open" else f"  [{child['status']}]"
            lines.append(f"    {child['id']}  {_kind_label(child)}  {child['title']}{closed}")
    if item["open_asks"]:
        lines.append("  open asks:")
        for asked in item["open_asks"]:
            urgent = "! " if asked["urgent"] else ""
            lines.append(f"    {asked['id']}  {urgent}→ {_addressed(asked)}  {asked['title']}")
    if item.get("comments"):
        lines.append("  comments to acknowledge:")
        for note in item["comments"]:
            lines.append(f"    {note['comment']}  {note['author']} on {note['handle']}: {' '.join(note['body'].split())[:80]}")
    if item["ledger_rows"]:
        from .ledger import line

        lines.append("  ledger:")
        lines += [f"    {line(row)}" for row in item["ledger_rows"]]
    if item["edges"]:
        lines.append("  edges:")
        for edge in item["edges"]:
            lines.append(f"    {_EDGE_LABEL[edge['dir'], edge['type']]} {edge['id']}  {edge['title']}"
                         + ("" if edge["status"] == "open" else f"  [{edge['status']}]"))
    if not section_bodies:
        lines.append("  sections:")
        for section in item["sections"]:
            lines.append(f"    {section['handle']}" + ("" if section["present"] else "  (empty)"))
        return "\n".join(lines)
    blocks = ["\n".join(lines)]
    for section in item["sections"]:
        body = (section["body"] or "").rstrip("\n")
        blocks.append(f"## {section['handle']}" + (f"\n\n{body}" if body else ""))
    return "\n\n".join(blocks)


def _need_line(need):
    """One missing-list entry as text: the need, then its details."""
    details = [f"{key} {_need_value(value)}" for key, value in need.items() if key != "need" and value is not None]
    return need["need"] + (f" ({', '.join(details)})" if details else "")


def _need_value(value):
    """A detail of a missing-list entry: an evidence summary as its id and verdict, anything else as is."""
    if isinstance(value, dict):
        return " ".join(str(value[key]) for key in ("id", "verdict") if value.get(key))
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return value


def _print_json(value):
    print(json.dumps(value, indent=2))


def main(argv=None):
    if argv is None:  # the `trk` script; callers that pass argv (the tests) get no store line
        note = store.home_note()
        if note:
            print(note, file=sys.stderr)
        argv = sys.argv[1:]
    argv = list(argv)
    if argv[:2] in (["claim", "renew"], ["claim", "release"], ["roles", "set"], ["config", "set"], ["evidence", "add"],
                    ["evidence", "retract"], ["review", "record"]):
        argv[:2] = [" ".join(argv[:2])]  # `claim renew`, `roles set` and the like are verbs of their own
    as_json = "--json" in argv
    gitrepo = sys.modules.get(f"{__package__}.gitrepo")  # loaded lazily; its answers last one invocation
    if gitrepo is not None:
        gitrepo.forget()
    try:
        if "--version" in argv[:2]:
            return _version(argv)
        args = build_parser().parse_args(argv)
        _pages_flags(args)
        _text_flags(args)
        args.handler(args)
        return 0
    except TrkError as exc:
        return _fail(exc, as_json)
    except sqlite3.Error as exc:  # outside BEGIN IMMEDIATE (connect, COMMIT) a busy timeout still exits 4; a corrupt file exits 1
        return _fail(store.sqlite_failure(exc), as_json)


def _version(argv):
    """`trk --version [--json]`, handled before the verb parser so it needs no verb and opens no store."""
    extra = [arg for arg in argv if arg not in ("--version", "--json")]
    if extra:
        raise usage(f"--version takes only --json, not {' '.join(extra)}")
    from . import version

    reply = version.report()
    if "--json" in argv:
        _print_json(reply)
    else:
        print(version.text(reply))
    return 0


def _pages_flags(args):
    """`trk pages --json <subverb>` means the subverb's --json; --dirty and --errors only filter the list."""
    if args.verb != "pages":
        return
    if args.pages_verb is None:
        args.json = args.pages_json
    elif args.dirty or args.errors:
        raise usage("--dirty and --errors filter `trk pages`; they take no subverb")
    elif args.pages_json:
        args.json = True


def _fail(exc, as_json):
    if as_json:
        print(json.dumps(exc.to_json()), file=sys.stderr)
    else:
        where = f" [{exc.rule}{': ' + exc.item if exc.item else ''}]" if exc.rule else ""
        print(f"trk: {exc.message}{where}", file=sys.stderr)
        for need in exc.extra.get("missing", ()):
            print(f"  missing: {_need_line(need)}", file=sys.stderr)
        if "hint" in exc.extra:
            print(f"  hint: {exc.extra['hint']}", file=sys.stderr)
    return exc.exit_code
