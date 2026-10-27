# Spec: Asks and owners

Project 2 of the Tracker v1 program. Spec state: **accepted** (2026-10-10; see [Acceptance](#acceptance)).

Vocabulary is [CONTEXT.md](../../CONTEXT.md). This spec extends [Spec: Core store + CLI](01-core-store-and-cli.md) and changes nothing there except where it says so. Settled inputs: [Asks and owners](../../.scratch/tracker-v1/issues/11-asks-and-owners.md) (its Resolution is the content of this spec), [ADR 0002](../adr/0002-herdr-contract.md) (herdr contract, point 5 as amended by Asks and owners), [Item data model](../../.scratch/tracker-v1/issues/04-item-data-model.md).

## Problem

An agent that needs a person (or another agent) to decide something has no tracked way to say so. Today it writes "## ⚑ Needs you" at the end of a turn and a hook queues the pane; the question lives in terminal scrollback, nothing stops the frontier from offering the waiting item to another session, and when the answer comes nobody reliably wakes the session that asked. Core already reserves the `ask` form, the `open-ask` readiness rule and the `ask.*` feed kinds, but nothing writes them. Owners exist in Core, but a top-level item may have none, so "who gets woken" can resolve to nobody.

## Solution

An **ask** is a leaf work item opened under any open item and addressed to a **role**, never a person: a function role (`product`, `engineering`, extensible) held by one actor in a role table the tracker keeps, or the relative role `parent`. The ask stores only the role; the actor is resolved whenever it is read, so re-owning an item or reassigning a role re-routes open asks with no extra rule. While open, an ask keeps its direct parent off the frontier (Core's `open-ask` rule). It closes **answered** (by the resolved actor or any human) or **withdrawn** (by the asker or the parent's owner, or automatically when the parent closes). Every close and every change of resolved addressee is one `ask.*` feed row carrying the facts herdr needs to route and wake: the resolved addressee and its kind, the wake owner, and the parent's most recent claim.

Owners stay named actors. Every top-level item has its own owner, defaulted from a store setting, so an effective owner always resolves.

## User stories

1. As a worker agent stuck on a product question, I run `trk ask <ticket> --to product "Which unit for flow?" --question -` and stop; my ticket leaves the frontier and herdr routes the question to whoever holds `product`.
2. As Kim, I run `trk waiting --me kim` and see my open asks, highest urgency score first, each with its parent and asker.
3. As Kim, I `trk answer <ask> --outcome "Use m³/h" --answer -`; the ticket returns to the frontier and the session that asked is woken if its pane is still alive, otherwise the ticket's owner is.
4. As Michael, I answer an ask addressed to `product` while Kim is away; the journal records "answered by michael for kim".
5. As the asker, I withdraw an ask I no longer need, or readdress it from `product` to `engineering`.
6. As Michael, I `trk roles set product kim`, and every open ask to `product` re-routes to Kim, with one `ask.readdressed` row each.
7. As the herdr plugin, I read `ask.opened`, `ask.readdressed`, `ask.answered` and `ask.withdrawn` rows from `changes`, and route and wake from their fields alone; `roles --json` and `waiting --json` are there when I need the whole picture.
8. As any writer closing an item that still has an open ask, the close is refused and names the ask, so no question is withdrawn without someone choosing to (amended 2026-10-22, tr-jwbg9).
9. As anyone creating a top-level item without `--owner`, it gets the store's default owner, so an ask beneath it always has someone to wake.

## Implementation decisions

### Schema (version 2)

`SCHEMA_VERSION` becomes 2. `MIGRATIONS[1]` upgrades a version 1 store under the write lock (see [Migration](#migration)).

| Table | Change |
|---|---|
| `roles` | new: role (lowercase slug, primary key), actor (references actors), updated_at |
| `items` | add `addressee_role TEXT` and `asker TEXT REFERENCES actors (id)` with `ALTER TABLE … ADD COLUMN`. "Set exactly when form is `ask`" is enforced in code, not by a CHECK: adding a table CHECK would mean rebuilding `items` under `foreign_keys=ON` with four tables referencing it. |
| `journal` | add `addressee TEXT`. Every feed row then carries `addressee`, null except on `ask.*` rows (replayed version 1 rows included). |
| `meta` | add `default_owner` |

- The resolved addressee is never stored on the item, only the role. The journal stores the actor resolved when each `ask.*` row was written.
- `parent` is a reserved role name: it never appears in `roles`.

### Roles

- A **function role** maps to exactly one registered actor in `roles`. `init` seeds `product` and `engineering`, both held by the `--me` actor, journaled as `role.set` rows (Michael then runs `roles set product kim` once Kim is an actor).
- The **relative role** `parent` resolves to the effective owner of the blocked item's parent (one level above the item the ask blocks). When the blocked item is top-level, it resolves to the blocked item's own owner.
- `resolve(ask)` returns the actor for the ask's role at the moment it is called. Routing, `show`, `waiting`, pages and feed rows all go through this one function.
- `trk roles [--json]` lists function roles: `{"schema":1,"roles":[{"role","actor","actor_kind","display"}]}`, sorted by role name. Text output is one `role → actor (display)` line each, plus a closing note that `parent` is relative.
- `trk roles set <role> <actor>` creates or reassigns a role. The actor must be registered. `parent` is refused (rule `reserved-role`). It journals `role.set` (item null; data: role, old, new) and, in the same transaction, an `ask.readdressed` row for every open ask whose resolved actor changed (see [Readdressing](#readdressing)). Setting a role to its current holder writes nothing.
- Roles are never removed in v1, like actors.

### Owners

- **Default owner**: `meta.default_owner`, set by `init` to the `--me` actor and journaled as `config.changed` (old null). `trk config [--json]` lists settings and `trk config set default-owner <actor>` changes it, journaled `config.changed` (item null; data: key, old, new). `default-owner` is the only settable key in v1.
- **Every top-level item has its own owner.** `new` without `--parent` and without `--owner` stores the default owner as the item's own owner (on the `item.created` row, the row's `owner` shows it). `edit --no-owner` on a top-level item is refused (rule `owner-required`).
- `move --top` of an item with no own owner stores its effective owner before the move as its own, journaled `owner.changed` (data.reason `moved-top`) in the same transaction, before the `item.moved` row.
- An ask never has its own owner: `edit --owner` on an ask is refused (rule `ask-no-owner`). Its effective owner is therefore always its parent's effective owner, which is the "wake owner" on every `ask.*` row (ADR 0002 point 4's `owner` field, with no change of meaning).
- Owners and the `actor` on every row stay named actors; roles are only for addressing asks.

### Opening an ask

`trk ask <parent> --to <role> "<title>" [--question @file | --question -] [--urgent] [--first|--last|--before <id>|--after <id>] [--json]`

- Creates a work item with form `ask` under `<parent>`, storing `addressee_role` and `asker` = the writing actor. The `question` section is required and non-empty (stdin when `--question -`; usage error, exit 2, when missing or empty).
- `trk new work-item --form ask` is refused as a usage error pointing at `trk ask`. `trk ask` is the only way to open one, so every ask has a role, an asker and a question.
- Refusals, checked in this order:
  - unknown parent (exit 3); parent closed (rule `parent-closed`, as in Core);
  - parent is an ask (rule `ask-leaf`);
  - `--to` names no function role and is not `parent` (rule `unknown-role`);
  - the role resolves to the asker (rule `self-addressed`: that is a to-do, not an ask).
- Any registered actor may open an ask under any open item of any kind, programs included.
- Asks are leaves: `new` or `move` with an ask as the parent is refused (rule `ask-leaf`).
- Journals one `ask.opened` row (see [Feed rows](#feed-rows)) **instead of** `item.created`. Its data carries `item.created`'s keys with an ask's fixed values (subtype null, jira_url null, repos `[]`; `owner` is never set on an ask) plus the ask fields, so every item still has exactly one creation row.

### Blocking and readiness

- Unchanged from Core: an open ask blocks only its **direct parent** through the `open-ask:<id>` rule. Ancestors stay on the frontier unless a `blocks` edge says otherwise. Asks are never ready and never claimable (Core's `kind-never` and `unclaimable`).
- The parent's claim is left alone when an ask opens; its lease lapses on its own unless renewed. herdr renews a session's claims only while it shows that session idle, done or blocked (ADR 0002 point 6, amended 2026-10-12).
- Edges to and from asks follow Core's rules unchanged.

### Answering

`trk answer <ask> --outcome "<one-line gist>" [--answer @file | --answer -]`

- Sets the `answer` section (required, non-empty; stdin when `--answer -`) and closes the ask `done` with the outcome, in one transaction.
- Allowed for the actor the role resolves to, or **any human** actor as a proxy. An agent may answer only an ask that resolves to itself. Otherwise rule `ask-permission`, naming who may.
- Journals one `ask.answered` row **instead of** `item.closed`.
- Identity is the writing actor (`--as` / `$TRK_ACTOR` / default actor), which is self-declared. These checks catch mistakes, not impostors; v1 has one trusted write path.

### Withdrawing and other closes

- `trk withdraw <ask> --reason "<one line>"` closes the ask `wont-do` with outcome `withdrawn: <reason>`. Allowed for the asker or the parent's effective owner; otherwise rule `ask-permission`.
- `trk close <ask> --resolution duplicate --of <id>` and `--resolution superseded --by <id>` work as in Core, with the same permission as withdraw (a deliberate narrowing of "as for any item": both are withdrawals). They journal `ask.withdrawn` with `data.resolution` set accordingly.
- `trk close <ask>` with `done`, `wont-do` or `out-of-scope` is a usage error pointing at `answer` or `withdraw`.
- `trk reopen <ask>` is refused (rule `ask-final`, checked before Core's reopen rules): open a new ask instead. There is no `ask.reopened`.
  - *Amended 2026-10-11 by [Spec: Page publisher](04-page-publisher.md#amendments-to-asks-and-owners)*: an answered (`done`) ask may be reopened with a reason, journaled `ask.reopened`; a withdrawn ask still refuses `ask-final`. `ask.answered` adds `data.via`, and page commenters are registered automatically.

### Parent closes with an ask open

- *Amended 2026-10-22 (tr-jwbg9), Michael's ruling tr-29kgp, 2026-10-22: "Refuse to close until each open question is answered or withdrawn on purpose."* `close` is refused (exit 1, rule `open-asks`) while the item has an open ask, or a descendant does. The message names each open ask and the two routes: `trk answer <ask> …` or `trk withdraw <ask> --reason …`. The check runs first, so Core's `open-children` does not hide an ask beneath an open child. Nothing is written: a refused close withdraws nothing and has no override flag. A close never withdraws an ask; the earlier behaviour (each open child ask withdrawn `wont-do` with outcome `withdrawn: parent closed (<resolution>)` and `data.reason = "parent_closed"`) is gone. Journal rows written before the amendment keep that reason.
- Any other open child still refuses the close (`open-children`).
- This applies whatever the parent's resolution, including `superseded` and `duplicate`.
  - *Amended 2026-10-11 (tr-2ee1r)*: an ask cannot replace or duplicate a non-ask item. An ask is a question, not work. `trk close <item> --resolution superseded --by <ask>` or `duplicate --of <ask>`, where `<item>` is not an ask, is refused (exit 1, rule `ask-cannot-replace`) and writes nothing, whether the ask is the item's own or any other. Without this, an item could name one of its own open asks, which the close then withdraws and which would take over the item's blocks edges while closed. An ask may still be closed `superseded` by, or `duplicate` of, another ask (see [Withdrawing and other closes](#withdrawing-and-other-closes)).
- Reopening the parent leaves its withdrawn asks closed; open new ones if still needed.
- Resolving the ask's comment on its page is the Page publisher's job, driven by this row.

### Readdressing

- `trk readdress <ask> --to <role>` changes the role while the ask is open. Allowed for the asker, the parent's effective owner, the current resolved addressee or any human; otherwise rule `ask-permission`. `unknown-role` and `self-addressed` apply as on opening. Readdressing to the same role is a no-op that writes nothing; a change of role always writes one `ask.readdressed` row, even when both roles resolve to the same actor.
- An ask never changes parent: `move` of an ask to another parent is refused (rule `ask-parent-fixed`, checked before Core's move rules). Reordering among its siblings is allowed.
- **Implicit readdressing**: any transaction that may change what an open ask resolves to (`roles set`, an `owner.changed`, an `item.moved`) snapshots `resolve()` for every open ask before and after, and writes one `ask.readdressed` row for each ask whose resolved actor changed, after the row that caused it. Open asks are few, so the full snapshot is cheap and leaves no case to miss.
- `data.cause` says why: `readdress`, `role-set`, `owner-changed` or `moved`.
- An implicit readdress may land on the asker itself (say `roles set product worker` while `worker` waits on product). The causing write is not refused, since it is about roles or owners, not this ask: the row carries `data.self_addressed: true`, and the asker may then answer or withdraw it. Explicit readdressing still refuses `self-addressed`.

### Waiting on me

`trk waiting [--me <actor> | --all] [--root <id>] [--json]`

- Lists open asks whose role resolves to `--me` (default: the writing actor). `--all` lists every open ask. `--root` limits to one subtree.
- Order: highest urgency score first, then id (amended 2026-10-22 by tr-qwwsc, replacing urgent first, then oldest first). The score is computed on read, never stored: `held_up` × cache factor × 10 when the ask or its parent is urgent, to one decimal place. `held_up` counts the distinct open non-ask items that cannot move until the ask is answered: its parent and every open item reachable from the parent over outgoing `blocks` edges alone (transitively; a closed item stops the walk; the parent's containment children are not counted, because an open ask keeps only its parent off the frontier, as in `ready`). The cache factor is 1 + 9 × (minutes / 60)² for the first 60 whole minutes since the ask was last opened, reopened or readdressed by `trk readdress` (an `ask.readdressed` row with cause `readdress`; cause `role-set`, `owner-changed` or `moved` does not restart the wait), then 1; the constants live in `asks.py`.
- JSON: `{"schema":1,"asks":[…]}`, each the item JSON (see below) plus `parent` as `{id, title, kind, form}` and `age_seconds`, plus `urgency` `{score, base, held_up, minutes, cache_warm, urgent, reason}` (`base` is `held_up` × the urgent multiplier, without the time factor; `reason` is the line below).
- Text: one line each with id, a `!` for urgent, `role (actor)`, the parent's id and title, the ask's title and the reason line, e.g. `score 27.0 · holds up 3 · waiting 52m (cache warm, 8m left) · urgent`; past 60 minutes `waiting 3h (cache cold)`. Text output has no as-of; a page's Urgency fact ends the cache note with the render time (see the page publisher spec).

### Reads

- The item JSON for an ask (in `show`, `tree --json`, `waiting --json`) adds `addressee_role`, `addressee` (resolved now), `addressee_kind` and `asker`. Other items carry none of these keys.
- Three optional keys more, on every ask (amended 2026-10-24 by tr-bvxs5, from the decision-cards protocol v1 review): `rev`, the latest journal seq whose item is the ask, `ask.replied` rows included (a reply leaves `updated_at` alone), and for an open RED-read ask the larger of that and the same for its ticket, so a client can refuse a stale action without `updated_at`'s millisecond stamp; `awaiting`, `asker` when the latest reply since the ask's latest opening is by anyone but the asker, else `addressee`, and null once the ask is closed; and on each `replies` entry `body`, the full text of a `trk reply` (null for a page comment, which keeps only its excerpt).
- An open RED-read ask also carries `red_read` (no other item does; its presence is the marker, so a client never matches titles): `{ticket, sha, current, roles, files_count, brief, brief_lacking, brief_complete, brief_writer}`. `sha` is the full head of the ticket's latest `blast-radius` RED rating, the value for `trk review record --sha`, and `current` says whether that rating counts at the ticket's head now (false after a newer `pr` row and before a re-rating, or after a `reopen-stage`: the ask stays open and keeps the last RED rating, and a client shows it without Approve); `roles` are the roles that review its RED files and `files_count` the RED files (hidden ones counted). `brief` holds the nine headings' text as `decision`, `what_changes`, `why_red`, `impact`, `evidence`, `rollback`, `decision_criteria`, `where_to_look` and `recommendation`, each text or null. They come from the tolerant parser the audit and Merged gates use, so `brief_lacking` (the headings as written, in order) and `brief_complete` never disagree with a gate. `brief_writer` is `{actor, session}` of the section's latest write, or null with no brief. A closed ask, or one whose ticket has no RED rating, has none. An open RED-read ask is also held (tr-4bhay's rule, extended 2026-10-24 by tr-bvxs5) while no rating counts at the ticket's head: reason `rating-stale` when a newer `pr` row or a `reopen-stage` stopped the rating counting, `rating-missing` when its `blast-radius` row is retracted or its sha cannot be placed; a counting re-rating releases it. `red_read` stays on a stale ask with `current` false, so the asker's card still reads.
- `show` of any item adds `open_asks`: its open child asks as `{id, title, addressee_role, addressee, urgent}`, rank order. The subtree count rollup is computed by pages (project 4), not stored.
- `tree` shows an ask's line with `→ <role> (<actor>)` in place of the owner.
- The expected sections for an ask are `question`, `answer` (Item data model's Sections table).

### Feed rows

Within ADR 0002 point 5 as amended. Every `ask.*` row keeps the base fields. On these rows:

- `item` is the ask; `owner` is the ask's effective owner, which is the parent's effective owner (the wake owner);
- `addressee` is the actor `resolve()` returned when the row was written;
- `status_old`/`status_new` are `null`/`open` on `ask.opened`, and `open`/`closed` on `ask.answered` and `ask.withdrawn` (extending Core's list of rows that carry status);
- excerpts are plain text: from the section's markdown, drop heading, list and quote markers at line starts, code-fence lines, and `*`, `_` and backtick emphasis characters, turn `[text](url)` and `![text](url)` into `text`, then collapse every whitespace run to one space and cut to at most 240 characters including a trailing `…` when cut. Best effort, not a markdown parser.

| Change | data |
|---|---|
| `ask.opened` | rank, urgent, sections (handles), addressee_role, addressee_kind, question (excerpt), asker |
| `ask.readdressed` | old_addressee, old_role, addressee_role, addressee_kind, cause, self_addressed |
| `ask.answered` | resolution (`done`), outcome, addressee_role, owner_kind, parent_claim_session, parent_claim_live, answer (excerpt), proxy (true when the answerer is not the resolved addressee) |
| `ask.withdrawn` | resolution (`wont-do`, `duplicate` or `superseded`), outcome, `by`/`of` as on `item.closed`, addressee_role, owner_kind, parent_claim_session, parent_claim_live, reason (`parent_closed` when automatic, else the closer's reason; null for duplicate and superseded) |

- `parent_claim_session` is the session of the parent's most recent claim, live or lapsed, or null if it was never claimed; `parent_claim_live` is whether that claim is live at the row's commit.
- `owner_kind` and `addressee_kind` are the actors' kinds (`human | agent`); null when the actor is null.
- New non-ask kinds: `role.set` and `config.changed` (item null, like `actor.*`; `--root` never returns them). Consumers ignore kinds they don't know, and none of this bumps `schema`.
- `contract/changes.schema.json` adds the four ask kinds' data shapes, `role.set`, `config.changed`, and `addressee` on every row (non-null on `ask.*` rows only).
- herdr routing (ADR 0002 point 10) works on the resolved `addressee` and `addressee_kind`; the tracker only states facts and never decides who gets prompted.

### Writing verbs added

| Verb | Does |
|---|---|
| `ask <parent> --to <role> "<title>" [--question] [--urgent] [position]` | open an ask |
| `answer <ask> --outcome "…" [--answer]` | answer and close `done` |
| `withdraw <ask> --reason "…"` | close `wont-do` |
| `readdress <ask> --to <role>` | change the role |
| `roles` · `roles set <role> <actor>` | the role table |
| `config` · `config set default-owner <actor>` | store settings |
| `waiting [--me A \| --all] [--root R]` | open asks waiting on an actor |

Exit codes and the error JSON are Core's. New rules: `ask-leaf`, `ask-parent-fixed`, `ask-permission`, `ask-final`, `ask-no-owner`, `self-addressed`, `unknown-role`, `reserved-role`, `owner-required`.

### Migration

`MIGRATIONS[1]`, in one transaction under the write lock on first open by the new CLI. Migrations now receive the write `Transaction`, not a bare connection, so their rows are journaled and `feed.head` is rewritten like any other write:

1. Create `roles`; add the `items` and `journal` columns.
2. Set `meta.default_owner` to `meta.default_actor` (journaled `config.changed`); seed `product` and `engineering` to it, journaled as `role.set` rows; the default actor is the `actor` on all of them.
3. Give every top-level item without its own owner the default owner, journaled `owner.changed` with `data.reason: "migration"`.
4. Set `schema_version` to 2.

A version 1 store has no asks (Core refuses `--form ask`), so nothing else needs converting.

## Testing decisions

- Same harness as Core: pytest through `main(argv)` against a temp `$TRK_HOME`, `$TRK_NOW` under `$TRK_TEST=1`, every journal row validated against `contract/changes.schema.json`.
- A **resolution table test** covers `resolve()` for: each function role; `parent` under a nested ticket, under a top-level item, and under an item that inherits its owner; and the same cases after `roles set`, `edit --owner` on an ancestor, and `move` of an ancestor, each checking the `ask.readdressed` rows written (exactly one per changed ask, none for unchanged ones).
- A **permission matrix test**: for answer, withdraw and readdress, each of asker, resolved addressee (human and agent), parent's owner, an unrelated human and an unrelated agent, asserting allowed or `ask-permission`.
- **Readiness**: Core's `ready` golden scenarios gain an ask under a ticket, an ask under a project and an ask under a program, checking that only the direct parent leaves the frontier and that `show`'s `why_not` still matches `ready` for every item.
- **Migration test**: a version 1 store built by the Core CLI (fixture), with a top-level item without an owner, opens under the new CLI to version 2 with the default owner set, roles seeded, the expected rows journaled, and the journal's old rows untouched.
- Excerpt test: markdown markers and links stripped, whitespace collapsed, the 240-character cut with `…`.

## Acceptance criteria

End-to-end checks against a fresh store (`trk init --me michael`, then `trk actor add kim --kind human`; `worker` was registered by hand here until `trk init` started registering it, 2026-10-27) unless stated:

1. `trk roles --json` lists `engineering` and `product`, both held by `michael`. `trk roles set product kim` journals `role.set`. `trk roles set parent kim` exits 1 with rule `reserved-role`.
2. `trk new program P` stores owner `michael` (the default owner). `trk edit P --no-owner` exits 1 with rule `owner-required`. After `trk config set default-owner kim`, a new top-level item is owned by `kim`.
3. Under P, project J (draft) and decision D under J. Session S1 claims D. Then, as `worker`, `trk ask D --to product "Q?" --question -` succeeds and S1's claim is still live. `show D` has `open-ask:<ask>` in `why_not` and lists the ask under `open_asks`; `ready --root P` still returns J. The `ask.opened` row has `addressee` `kim`, `addressee_kind` `human`, `owner` `michael`, the question excerpt and `asker` `worker`, and no `item.created` row exists for the ask.
4. As `michael`, `trk ask D --to engineering "…"` exits 1 with rule `self-addressed`. `trk ask <ask> --to product "…"` exits 1 with rule `ask-leaf`. `--to finance` exits 1 with rule `unknown-role`. `trk new work-item --form ask …` exits 2.
5. `trk waiting --me kim --json` returns the ask. After `trk roles set product michael`, a new `ask.readdressed` row (cause `role-set`, old_addressee `kim`, addressee `michael`) appears, and `waiting --me kim` is empty while `waiting --me michael` lists it.
6. With `$TRK_NOW` past S1's lease: as `worker` (an agent the ask does not resolve to), `trk answer` exits 1 with rule `ask-permission`. As `kim` (a human proxy), `trk answer <ask> --outcome "Use m³/h" --answer -` closes it `done`; the `ask.answered` row has actor `kim`, addressee `michael`, `proxy: true`, `parent_claim_session` S1 and `parent_claim_live: false`. `ready --root P` returns D again.
7. As `worker`, open two asks under D to `product`, the second `--urgent`. `waiting --all` lists the urgent one first. `trk close D --resolution done --outcome "…"` exits 1 with rule `open-asks`, naming both asks, and writes nothing. After `trk withdraw` on both, the close succeeds. (Amended 2026-10-22, tr-jwbg9: the close used to withdraw both with outcome `withdrawn: parent closed (done)`.)
8. Under J, decision E; as `worker`, ask A1 under E to `product`. `trk reopen <the answered ask from AC 6>` exits 1 with rule `ask-final`. `trk move A1 --parent J` exits 1 with rule `ask-parent-fixed`. `trk withdraw A1 --reason "…" --as herdr` exits 1 with rule `ask-permission`. `trk withdraw A1 --reason "not needed" --as worker` succeeds, with outcome `withdrawn: not needed` and `data.reason` `not needed`.
9. A store created by the Core (schema 1) CLI with an ownerless top-level item opens under this build at schema 2, as in the migration test, and `changes --since 0` replays old and new rows, all validating against the updated schema.
10. herdr-extension confirms its plugin consumes the four `ask.*` rows (route on `ask.opened`/`ask.readdressed`, wake on `ask.answered`/`ask.withdrawn`, clear quietly on `reason: parent_closed`) from a live store without errors.

## Out of scope

- **Artifact comments as answers to asks** (mapping a page commenter to an actor, resolving the ask's comment): its own decision ticket and the Page publisher.
- **Pages**, including the "Waiting on people" box and per-subtree open-ask counts: project 4.
- **Stage gates** and build-discipline refusals: project 3.
- **Channels, routing and who exactly gets prompted**: herdr plugin policy (ADR 0002 points 9 and 10).
- Authenticating actors (identity stays self-declared in v1), removing roles or actors, multiple holders per role, and asks with more than one addressee. (Ask reopening was out of scope here; Page publisher adds it.)
- Other "decisions waiting on me" views beyond asks (fog: **Audience views**).

## Acceptance

Accepted by the authoring agent (wC7:pS) on 2026-10-10, after an independent review against the Asks and owners decision, the Core spec and its built code, ADR 0002 and CONTEXT.md. The review raised 12 findings, all applied. The main ones: acceptance criteria 3 and 6–8 re-staged so their setups produce what they assert; the migration adds plain columns (ask fields checked in code) and runs as a journaled write; `addressee` present on every row, null off `ask.*`; excerpts are plain text as the decision says; implicit readdressing onto the asker is allowed and flagged; auto-withdraw rows precede Core's close rows and describe the claim before release; `ask-final` and `ask-parent-fixed` checked before Core's rules. Agents accept specs under this program's Notes.
