# Spec: Core store + CLI

Project 1 of the Tracker v1 program. Spec state: **accepted** (2026-10-10; see [Acceptance](#acceptance)).

Vocabulary is [CONTEXT.md](../../CONTEXT.md). Settled inputs: [ADR 0001](../adr/0001-own-store-not-beads.md) (own SQLite store, Python CLI), [ADR 0002](../adr/0002-herdr-contract.md) (herdr contract), [Item data model](../../.scratch/tracker-v1/issues/04-item-data-model.md), [How the destination splits into projects](../../.scratch/tracker-v1/issues/03-split-destination-into-projects.md).

## Problem

Michael's agent fleet tracks its work in hand-kept markdown (`.scratch/` maps and tickets), herdr-projects `TASKS.md` files and artifact boards that reach hundreds of versions. Nothing answers "what can a session start now?" from data, nothing stops two sessions taking the same item, and herdr has nothing to watch: it cannot learn that an item was created, claimed or closed without someone telling it. Every later project (asks, stage gates, pages, workflow skills) needs one store of items with a reliable change record underneath it.

## Solution

One machine-wide SQLite store behind a small Python CLI, `trk` (working name until **Product name** resolves; renaming is mechanical). It holds items of the three kinds under the nesting rule, their bodies as named sections, `blocks` / `relates` / `supersedes` edges, a registry of actors, and leased claims. Every write goes through the CLI in one serialized transaction and appends to a journal that is never updated, deleted or compacted. The journal is the change feed: `trk changes --since <seq> --json` is what herdr polls, and `trk ready` computes the frontier from the store.

This is the thinnest slice that can track the Tracker v1 program itself. Re-entering that program by hand and retiring `.scratch/` as the source of truth is the last acceptance check.

## User stories

1. As an agent session, I run `trk ready --root <program>` and get the items I can start now, urgent first and then in tree order, so I never pick from a stale markdown map.
2. As an agent session, I `trk claim` an item with my Claude session uuid, so no other session takes it while I work. My per-turn hook renews the lease at no token cost, and if I die or `/clear`, the lease lapses on its own.
3. As an agent charting a program, I create a program, its projects and its decision work items, write their sections, wire `blocks` edges, and close each one with a resolution and a one-line outcome.
4. As the herdr plugin, I poll `trk changes --since <cursor> --json` and get every change in commit order, with root, depth and effective owner on each row, so I can place, wake and tidy without reading SQLite.
5. As the herdr plugin, on pane exit I run `trk claim release --session S`, and I read `trk claims --json` and `trk actors --json` for routing.
6. As Michael, I run `trk tree <program>` and `trk show <id>` to read where a program stands without opening a file.
7. As any writer, I get a clear refusal (rule, item, why) when an action would break an invariant: a kind ranked above its parent, a `blocks` cycle, closing with open children, an open item under a closed parent. A close without an outcome is a usage error.

## Implementation decisions

### Runtime and packaging

- Python ≥ 3.12, standard library only at runtime (`sqlite3`, `argparse`, `json`, `secrets`). No third-party dependencies: `claim renew` runs on every agent turn, so cold start matters (target under 150 ms).
- Installed with `uv tool install`. One module per area (store, ids, ranks, items, edges, claims, ready, feed, cli); the CLI layer only parses arguments and prints.

### Store location and files

- The store dir is `$TRK_HOME`, else `~/.local/share/trk/`. It contains `store.db` (WAL mode, `foreign_keys=ON`, `busy_timeout=10000`) and `feed.head`.
- `trk init --me <actor-id> [--display "..."] [--prefix tr]` creates the store, registers the human actor `<actor-id>` as the default actor and the agent role `herdr`, and records the id prefix. It refuses if a store already exists.
- `meta` table: `schema_version`, `id_prefix`, `default_actor`, `lease_minutes` (default 60) and `epoch` (an opaque uuid; see [Change feed](#change-feed)). A store from before epochs gets one on its first open, with no journal row. On open, the CLI migrates an older schema under the write lock and refuses to touch a newer one.
- `trk backup <path>` writes a consistent copy with SQLite's online backup API. That is the backup story; there is no JSONL source of truth.
- `trk restore <path>` puts a backup back (tr-acf0s).
  - It reads the backup's bytes into memory, so SQLite never opens the backup file and it is never changed.
  - It refuses:
    - a `-wal` file beside the backup (rule `backup-has-wal`): that is a file copy of a live store, not `trk backup`'s output;
    - a file that is not a store (rule `not-a-store`);
    - a backup with a newer schema (rule `schema-newer`).
  - The staged copy gets a fresh epoch and lands in the live store in one step of SQLite's online backup API. That step holds SQLite's write lock for the whole copy and writes through the live WAL, so no WAL or SHM file is left stale.
  - Then, in a second transaction under trk's write lock, it rewrites `feed.head` from the restored head. Until then the hint may still name the old head, which is harmless because polling is the contract.
  - It writes no journal row, so `head` is the backup's. A backup with an older schema is migrated by the next open, which journals as usual under the new epoch.
  - A missing backup exits 3 (rule `no-backup`). Naming the live store as the backup is a usage error.
  - It replies `{"schema":1,"restored":P,"head":H,"epoch":E}`.
  - Never restore by copying files: the copy would keep the backup's old epoch.
  - The epoch is a `meta` key, not a schema change, so `schema_version` does not move and builds from before epochs still open the store.

### Write path

- Every mutating command runs in one `BEGIN IMMEDIATE` transaction. The SQLite write lock is the single serialized write path. There is no daemon and no separate lock file. Validation, state changes and journal rows commit together or not at all.
- When a transaction has appended rows, the CLI writes the new head seq to `feed.head.tmp` and renames it over `feed.head` just before COMMIT, while it still holds the write lock. That way two writers can never leave the file going backwards. If the transaction then rolls back, the hint runs ahead by a seq that never appears, which is harmless.
- Every invocation, reads included, first sweeps lapsed leases (see [Claims](#claims)). A sweep that finds nothing takes no write lock.
- Writes record an **actor**: `--as <id>`, else `$TRK_ACTOR`, else the store's default actor. It must be a registered actor. Writes record a **session**, which is optional except on claims: `--session <uuid>`, else `$TRK_SESSION`. Agent hooks pass the Claude session uuid from the hook's JSON input. On the `claim` verbs, `--session` names the claim's session, which is not necessarily the writer's. See the `claim.*` row rule under [Change feed](#change-feed).
- Nothing is ever deleted. Items close; edges are removed with a journal row; actors cannot be removed in v1.

### Schema

The schema is internal to the CLI (ADR 0002 narrows ADR 0001: herdr reads JSON only). Tables:

| Table | Columns |
|---|---|
| `meta` | key, value |
| `actors` | id (lowercase slug), kind `human \| agent`, display, created_at |
| `items` | id, kind `program \| project \| work_item`, form (work items: `spec_bound \| self_contained \| decision`; `ask` reserved), subtype (decisions: `research \| prototype \| grilling \| task`), title, parent_id, rank, urgent (0/1), owner (actor or null), status `open \| closed`, resolution, outcome, spec_state (projects: `draft \| accepted`), jira_url, created_at, updated_at, closed_at |
| `item_repos` | item_id, repo (absolute path or remote URL, stored as given) |
| `sections` | item_id, handle, body, updated_at; primary key (item_id, handle) |
| `edges` | src, type `blocks \| relates \| supersedes`, dst, created_at; primary key (src, type, dst). `A blocks B`: B waits on A. `A supersedes B`: A replaces B. `relates` is symmetric and stored once. |
| `handoffs` | id (`INTEGER PRIMARY KEY AUTOINCREMENT`), item_id, at, actor, session, first_line, body. Schema 3; see [Handoffs](#handoffs). |
| `claims` | id, item_id, session, actor, lease_minutes, taken_at, lease_until, ended_at, end `released \| expired` |
| `journal` | the feed row columns (see [Change feed](#change-feed)); `seq INTEGER PRIMARY KEY AUTOINCREMENT` |

- `journal` and `handoffs` have `BEFORE UPDATE` and `BEFORE DELETE` triggers that `RAISE(ABORT)`. Append-only is enforced by the database, not by convention.
- **Kind and form are immutable.** To change either, close the item as `superseded` by a new one. The point is that a spec-bound ticket can never be flipped to self-contained by an ordinary update to slip past its spec gate.
- Stage, evidence, addressee and asks are not in this schema; projects 2 and 3 add them.

### Ids

- An id is `<prefix>-<5 chars>`, for example `tr-k7m2q`. The chars are drawn with `secrets` from the lowercase Crockford base32 alphabet `0123456789abcdefghjkmnpqrstvwxyz` (about 33 M ids). On a collision, draw again; after three collisions in one creation, use 6 chars. Ids are non-sequential, so a later second writer cannot collide by counting.
- Input ids are matched case-insensitively, and `i l o u` are read as `1 1 0 v` (Crockford). There is no prefix-abbreviation matching.

### Kinds, nesting and order

- Kind rank: program > project > work item. A child's kind never ranks above its parent's. `new` and `move` check in this order and refuse on the first failure: an unknown parent (exit 3), a parent cycle (rule `cycle`), a kind violation (rule `nesting`), and an open item under a closed parent (rule `parent-closed`). That keeps the invariant that a closed item has no open children.
- **Depth** is 0 for a top-level item. **Root** is the top-level ancestor; a top-level item is its own root.
- **Rank** is a fractional-index string (base-62 digits, compared bytewise) that orders siblings. `new` and `move` take `--first`, `--last` (default), `--before <id>` or `--after <id>`. A key is generated between its neighbours, so a move rewrites only the moved item. If a generated key would exceed 32 chars, that parent's children are re-keyed evenly in the same transaction. Each one gets an `item.moved` row with `data.rekey: true`, which consumers may ignore. Top-level items are ranked among themselves.
- **Effective owner**: the item's own owner, else the nearest ancestor's, else null.

### Sections

- A section handle is a lowercase kebab slug (`acceptance-criteria`). The expected handles per kind and form are the Sections table of the Item data model:
  - program: `destination`, `notes`, `not-yet-specified`;
  - project: `problem`, `solution`, `user-stories`, `implementation-decisions`, `testing-decisions`, `acceptance-criteria`, `out-of-scope`;
  - spec-bound ticket: `what-to-build`, `acceptance-criteria`;
  - self-contained ticket: `current-behaviour`, `desired-behaviour`, `acceptance-criteria`;
  - decision: `question`, `resolution`.
- Extra handles are allowed. Core never requires a section; which must be filled when is Stage gates' call.
- `show` lists expected sections first, in table order (missing ones as empty), then extras in creation order. Generated parts (decisions so far, children rollups) are never stored.
- Bodies are markdown text, unparsed, up to 256 KiB each.

### Handoffs

Added 2026-10-11 (tr-3r0je), from Michael's "Handoffs" decision in the Tracker v1 Notes. Sessions checkpoint into **handoffs**: markdown or text records kept with an item, one row per checkpoint. A later session reads the latest to restart the work.

- A handoff is not a section. Core never requires one, and no gate reads them.
- `trk handoff add <id> [--file F] [--session S] [--as A] [--json]` appends one, reading the body from F or else stdin. It prints the item id, or `{"schema":1,"item":ID,"handoff":{…}}` with `--json`, the same shape as `show --json`. The writer is the resolved actor and session, as on every write; the session may be null.
- `trk handoff show <id> [--all] [--json]`:
  - with no flag it prints the latest body raw, like `section get`;
  - `--all` prints every handoff oldest first, each under a header line `## handoff <n>  <at>  <actor>[  session <s>]`, then a blank line and the body;
  - `--json` prints `{"schema":1,"item":ID,"handoff":{…}}`, or `"handoffs":[…]` under `--all`.
- A handoff record is `{id, item, at, actor, session, first_line, body}`. `id` increases across the store, not per item.
- **first_line** is the body's first non-blank line, stripped and cut to 200 chars. It is stored at write time, so reads never parse bodies.
- **Refusals**: an unknown item exits 3. A body with no non-blank line is a usage error. A body over 256 KiB (UTF-8 bytes, the section limit) is refused with rule `handoff-too-large`. `show` without `--all` on an item with no handoff exits 3; `--all` lists none.
- A handoff may be added to a closed item. Adding one does not change the item's `updated_at`.
- **Append-only**: `handoffs` has `BEFORE UPDATE` and `BEFORE DELETE` triggers that `RAISE(ABORT)`, like the journal.
- **The item JSON** gains `handoff`: the latest's `{id, at, actor, session, first_line}`, or null. It never carries the body. Human `show` prints `handoff: <at>  <actor>  <first line>  (trk handoff show <id>)` when there is one.
- **Feed**: each add writes one `handoff.added` row. `item` and the item fields describe the item, `actor` and `session` are the writer's, and `data` is `{id, first_line}`. The body never travels in the feed.
- **Schema 3**: `MIGRATIONS[2]` creates the empty table, its index and triggers in the migration transaction. It writes no journal rows.

### Status, close and reopen

- `trk close <id> --resolution <r> --outcome "<one sentence>"`. Resolution is one of `done`, `wont-do`, `superseded`, `out-of-scope`, `duplicate`. The outcome is required: one line, at most 300 chars, no newline.
- `trk close` refuses (rule `open-children`) while any child is open, and lists the open children. Children close first, bottom-up. (Asks: see [Asks and owners](02-asks-and-owners.md#parent-closes-with-an-ask-open); an open ask refuses the close with rule `open-asks`.) Core does not judge "destination holds" or "acceptance criteria met"; the closer does, and Stage gates may add checks.
- Closing an item ends its live claim in the same transaction, journaled as `claim.released` with `data.reason: closed`.
- `superseded` requires `--by <replacement>`. In the same transaction it adds `replacement supersedes id` if missing and moves every outgoing `blocks` edge of the item onto the replacement (an `edge.removed` and an `edge.added` row per edge, skipping any the replacement already has, and refusing if a move would make a cycle).
- `duplicate` requires `--of <id>` and adds a `relates` edge.
- `trk reopen <id>` clears resolution, outcome and closed_at. It refuses while the parent is closed. Edges moved by a supersede stay moved.

### Edges

- `trk link <a> blocks|relates|supersedes <b>` and `trk unlink <a> <type> <b>`. Self-links are refused. A `blocks` link that would close a cycle over `blocks` edges is refused (rule `cycle`). Edges may cross the tree, and the parent link is not an edge.
- A blocker counts as cleared once it is closed with any resolution.

### Spec state

- A new project starts with spec state `draft`. `trk spec accept <project>` and `trk spec draft <project>` set it, journaled as `spec.changed`. Both refuse on non-projects and on closed projects.
- Core puts no precondition on accepting. Agents judge acceptance; Stage gates may add one.

### Ready (the frontier)

`trk ready [--root <id>] [--owner <actor>] [--limit N] [--json]`

- **Baseline**: the item is open, has no live claim, no open `blocks` blocker and no open child ask (none exist until project 2, but the rule is coded now). The baseline does not look at ancestors' blockers.
- **Per kind**: programs never; projects while spec state is `draft`; decisions and self-contained tickets on the baseline; spec-bound tickets on the baseline plus a parent chain whose nearest project is `accepted` (a spec-bound ticket with no project ancestor is never ready, and `show` says why); asks never.
- **Order**: walk the whole forest depth-first, with siblings in rank order and top-level items in rank order. Children of items that are not themselves ready are still walked. Collect ready items, then put the urgent ones first. Each group keeps its walk order.
- `--root` limits the walk to one subtree. `--owner` keeps items whose effective owner is that actor.
- `ready` and `show` share one function that returns, for any item, `ready: bool` and `why_not: [rule…]`. The rules are `closed`, `claimed`, `blocked-by:<id>`, `open-ask:<id>`, `kind-never`, `spec-not-draft`, `spec-not-accepted` and `no-project`. The output matches `ready` exactly by construction.

### Actors

- `trk actor add <id> --kind human|agent [--display "..."]` and `trk actor edit <id> --display "..."` are journaled as `actor.added` / `actor.updated`, with `item` null.
- `trk actors --json` returns `{"schema":1,"actors":[{"id","kind","display"}]}`.
- `--owner` on `new` / `edit` must name a registered actor. `--no-owner` clears an owner so it inherits again. Changing an item's own owner journals `owner.changed` (data: old, new). Descendants whose effective owner changed as a result get no rows of their own, the same way a move implies its descendants moved.

### Claims

- `trk claim <id> --session S [--as A] [--lease 45m]` takes a lease of `lease_minutes` by default.
  - It refuses if the item is closed or an ask (rule `unclaimable`), or a program unless `--force` is given. A root's orchestrator may hold its program that way (ADR 0002 point 10); programs never come from the frontier, or if another session holds a live claim (rule `claimed`, exit 4).
  - If the same session already holds the claim, it renews it.
  - If the item is not ready for any other reason, it refuses with rule `not-ready` and lists the `why_not` rules, with or without `--force`. `--force` claims only a program, recorded as `data.forced: true`.
  - A claim never changes the owner.
- `trk claim --next [--root R] [--owner A] --session S` claims the first item `ready` would return, in the same transaction, and prints it. With nothing ready it exits 0 and prints nothing (or `{"claimed":null}` with `--json`).
- `trk claim renew --session S [--lease DUR]` extends every live claim of S to now + that claim's own `lease_minutes`, or + `--lease`, which also becomes the new stored length. It is built for a per-turn hook: no output, exit 0 even with no claims, and it writes only claims whose remaining lease is under half their stored lease length. So a busy agent journals `claim.renewed` about every half-hour, not every turn.
- `trk claim release --session S [<id>]` releases all of S's live claims, or one, journaled as `claim.released`. Any actor may run it; the plugin runs it as `herdr`.
- `trk sweep` ends every lease with `lease_until <= now` (`end=expired`) and journals `claim.expired` with `at` = `lease_until`. Every invocation runs it implicitly, and the explicit verb exists for the plugin.
- `trk claims [--session S] [--item ID] --json` returns `{"schema":1,"claims":[{"session","actor","item","lease_until","taken_at"}]}` for live claims only.
- A claim lapsed by `/clear` stays lapsed. The new session claims again.

### Change feed

This section implements ADR 0002 points 2–7. ADR 0002 is the authority, and this section adds only build details.

- `trk changes --since <seq> [--root <id>] [--limit N] --json`. `--since` is exclusive and `--since 0` replays everything. The default limit is 1000. Rows come in ascending seq. There is no `--follow`.
- The response is `{"schema":1,"epoch":E,"head":H,"next":M,"changes":[…]}`.
  - `epoch` names the journal the seqs belong to. `trk restore` rotates it, and a consumer keeps (epoch, seq): when the epoch changes or `head` drops below its cursor, it rebuilds from `--since 0` (ADR 0002 point 2). It is additive.
  - `head` is the store's latest seq.
  - `next` is the cursor to pass as the following `--since`: the seq of the last row the query scanned, which accounts for rows filtered out by `--root`. With an empty scan it is `head`.
  - `next` is an additive field (it does not bump `schema`). A consumer that ignores it and uses the last row's seq is still correct without `--root`.
- `--root R` keeps the rows whose `root` is R, plus `item.moved` rows whose old root (`data.old.root`) is R.
- **seq** is the journal's `AUTOINCREMENT` key: strictly increasing in commit order within an epoch, never reused within one, not gapless (a rolled-back transaction may burn one).
- **Row fields**: `seq`, `at` (UTC, `2026-10-10T21:52:03.123Z`), `change`, `item`, `kind`, `form`, `title`, `parent`, `root`, `depth` (all after the change), `actor`, `session`, `owner` (the effective owner after the change), `status_old`, `status_new` and `data` (an object, `{}` when empty). Ask rows add `addressee` (project 2). Nullable fields:
  - `actor.*` rows have a null `item`, and therefore null `kind`, `form`, `title`, `parent`, `root`, `depth` and `owner`. `--root` never returns them.
  - `form` is null except on work items, `parent` is null on top-level items, and `session` and `owner` may be null.
  - `status_old` and `status_new` are null except on `item.created` (old null), `item.closed` and `item.reopened`.
  - `contract/changes.schema.json` states exactly this.
- **On `claim.*` rows**: `session` is the claim's session, `data.claim_actor` is the claimant, and `actor` is whoever ran the command. That is `herdr` for a release by the plugin. For an implicit `claim.expired`, it is the claimant, since no one acted. `kind` and `form` use the CLI spellings `work-item`, `spec-bound`, `self-contained`, and the same spellings appear in all JSON.
- **Change kinds written by Core**, and their `data`:
  - `item.created`: subtype, rank, urgent, jira_url, repos, section handles.
  - `item.updated`: `fields` (names changed), `old` and `new` (scalar values; for sections, the name is `section:<handle>` with no values).
  - `item.closed`: resolution, outcome, plus `by` or `of`.
  - `item.reopened`: old resolution.
  - `item.moved`: old/new parent, root, depth and rank. One row per move, and a reorder within the same parent is also `item.moved`.
  - `owner.changed`: old, new.
  - `claim.taken`, `claim.renewed`, `claim.released`, `claim.expired`: claim_actor, lease_until, forced, reason.
  - `spec.changed`: old, new.
  - `edge.added`, `edge.removed`: type, src, dst. Written once, with `item` = src.
  - `actor.added`, `actor.updated`: id, kind, display.
  - `handoff.added`: id, first_line (see [Handoffs](#handoffs)). An additive kind under ADR 0002, added 2026-10-11.
- `stage.changed` and the `ask.*` kinds are reserved by ADR 0002 and written by projects 3 and 2. Consumers ignore unknown kinds. Adding fields never bumps `schema`; removing a field or changing its meaning does.
- **Leases**: the sweep and renewal rules under [Claims](#claims) are ADR 0002 point 6.
- **Wake hint**: `feed.head` holds the head seq as decimal text plus a newline, rewritten atomically after every commit that appended rows. Polling `changes` stays the contract.
- A JSON Schema for the response, `contract/changes.schema.json`, ships in the repo. herdr-extension's plugin tests validate against the same file.

### Other read verbs

- `trk show <id> [--json] [--no-sections]` returns every item field, depth, root, effective owner, repos, sections, edges in and out (with the other end's title and status), children (id, kind, form, title, status) in rank order, the live claim, the latest handoff's summary, `blocked` (derived), `in_progress` (live claim), `ready` and `why_not`.
- `trk tree [<id>] [--all] [--json]` prints an outline in rank order. By default it shows open items, and a closed child shows as one line with its outcome under `--all`. Each line has id, kind/form, title, a ready/claimed/blocked marker and the effective owner.
- The JSON item object is the same shape in `show`, `tree --json`, `ready --json` and `claim --json`.

### Writing verbs (summary)

| Verb | Does |
|---|---|
| `init` | create store, default actor, `herdr` role |
| `new <kind> "<title>" [--form] [--subtype] [--parent] [--first\|--last\|--before\|--after] [--owner] [--urgent] [--jira URL] [--repo R]… [--section handle=@file\|handle=-]… [--json]` | create; prints the id (or the item JSON) |
| `edit <id> [--title] [--owner\|--no-owner] [--urgent\|--no-urgent] [--jira\|--no-jira] [--add-repo] [--remove-repo]` | change scalar fields |
| `section set <id> <handle> [--file F]` (stdin by default) · `section rm <id> <handle>` · `section get <id> <handle>` | write, remove, read one section raw |
| `move <id> [--parent P\|--top] [--first\|--last\|--before\|--after]` | reparent and/or reorder |
| `close` · `reopen` | see [Status](#status-close-and-reopen) |
| `link` · `unlink` | edges |
| `spec accept\|draft <project>` | spec state |
| `claim` · `claim --next` · `claim renew` · `claim release` · `sweep` | claims |
| `actor add\|edit` | actors |
| `handoff add <id> [--file F]` (stdin by default) · `handoff show <id> [--all]` | append or read handoffs |
| `backup <path>` | online copy |
| `restore <path>` | put a backup back and rotate the epoch |

`kind` is spelled `program | project | work-item`; `--form` is `spec-bound | self-contained | decision`, and `--form ask` is refused until project 2. Work items require `--form`, and decisions require `--subtype`.

### Errors and exit codes

- Exit 0 means ok, 1 a refused rule, 2 a usage error, 3 not found, and 4 a conflict (claimed by another session, or a lock timeout).
- With `--json`, errors go to stderr as `{"error":{"code","rule","item","message"}}`. Without it, one human line goes to stderr. Refusals always name the rule and the offending item(s).

## Testing decisions

- pytest drives the CLI through its `main(argv)` entry point against a temp `$TRK_HOME`. A handful of end-to-end tests run the installed `trk` binary in a subprocess.
- A test-only `$TRK_NOW` (ISO time) overrides the clock, so lease, sweep and ordering tests are deterministic. It is ignored unless `$TRK_TEST=1`. Likewise `$TRK_BUSY_TIMEOUT_MS` shortens the busy timeout, so a test can make a write time out on the lock and exit 4.
- Golden scenarios cover `ready`: the nesting rule, a blocker closed with each resolution, a spec-bound ticket under draft vs accepted projects, urgent ordering across subtrees, and claimed vs lapsed claims. Each scenario checks that `show`'s `why_not` matches `ready` for every item in the store.
- Invariant tests: the journal refuses UPDATE and DELETE at the SQLite level; seq is strictly increasing; `feed.head` equals `head` after every write; every row validates against `contract/changes.schema.json`.
- A concurrency test runs 8 processes × 50 mixed writes on one store. No write is lost, there is no `database is locked` failure within the busy timeout, and 400 journal rows have unique, increasing seq.
- A performance check: `trk claim renew --session <unknown>` on a store of 5 000 items completes in under 150 ms wall clock on this machine.

## Acceptance criteria

End-to-end checks, each run against a fresh store unless stated:

1. `trk init --me michael` creates the store with actors `michael` (human) and `herdr` (agent). A second `init` exits 1.
2. `trk new project X --parent <work-item>` exits 1 with rule `nesting`. Moving program P under its own sub-program exits 1 with rule `cycle`. `new` under a closed parent exits 1 with rule `parent-closed`.
3. `link A blocks B`, `link B blocks C`, `link C blocks A`: the third exits 1 with rule `cycle`.
4. Under program P, created in the order J, D, E, U (each `--last`): project J (draft), decision D blocked by decision E, and urgent decision U. `ready --root P` returns `U, J, E` in that order. After `close E --resolution wont-do --outcome "…"`, D appears after J.
5. Session S1 claims D; S2's `claim D` exits 4. With `$TRK_NOW` past the lease, any command sweeps it, `changes` shows `claim.expired` with `at` = the lease end, and S2 can claim.
6. Continuing from AC 5:
   - `close` without `--outcome` exits 2.
   - Closing P while it has open children exits 1 with rule `open-children` and lists them.
   - Create decisions D2 and X, then `link D blocks X`, then `close D --resolution superseded --by D2 --outcome "…"`. Afterwards X's `why_not` is `blocked-by:D2`, D2 supersedes D, the `edge.*` rows show the move, and S2's claim on D ended with `claim.released` and `data.reason: closed`.
7. `changes --since 0 --json` replays every row from creation, ascending, validating against the schema. `--since <head>` returns no rows and `next` = head. `--root P` omits rows from another top-level item.
8. `sqlite3 store.db "delete from journal"` fails with the trigger's abort.
9. The concurrency test and the `claim renew` performance check pass.
10. herdr-extension confirms the plugin reads `changes`, `show`, `ready`, `actors` and `claims` from a live store without errors (the plugin side of ADR 0002).
11. **Dogfood**: the Tracker v1 program is re-entered by hand into the real store, and the `.scratch/tracker-v1/` markdown is marked retired.
    - The program's `destination`, `notes` and `not-yet-specified` sections come from `map.md`.
    - Its five projects are the spec tickets: a spec-writing ticket *is* its project while the spec is draft. Each project's state matches its spec doc.
    - The map's Out of scope entries are entered as decision items closed `out-of-scope`. Closed items are closed in their original close order, because a program's decisions so far are ordered by close order.
    - Every decision ticket is a `decision` work item with its subtype and `blocks` edges. Closed ones are closed `done` (or `out-of-scope`) with the map's gist as outcome, and their resolution text goes in the `resolution` section.
    - Afterwards, `trk ready --root <program>` lists the same items as the map's frontier (open, unblocked, unclaimed) at that moment.

## Out of scope

- **Asks and addressees**, the `ask.*` rows' semantics and ask-driven wake paths: project 2, Asks and owners. Core only codes the "open child ask" readiness rule and refuses `--form ask`.
- **Stages, evidence, gate checks on `spec accept` / close, and build-discipline refusals**: project 3.
- **Pages and publishing**: project 4. **Wrapper skills**: project 5.
- **Importing** herdr-projects or other live programs (fog); only this program is entered, and by hand.
- Multiple writers or machines, JIRA sync, a daemon or `--follow`, item deletion, archiving or pruning closed items, full-text search, and any web or TUI view.

## Acceptance

Accepted by the authoring agent (wC7:pA) on 2026-10-10, after an independent review against CONTEXT.md, ADR 0001/0002 and the Item data model. The review raised 14 findings; all 14 were applied, the main ones being: feed.head written under the write lock; session and actor semantics on `claim.*` rows; close ends live claims; rule `parent-closed`; programs claimable only with `--force`; nullable feed fields stated; concrete setups for AC 2, 4 and 6. Agents accept specs under this program's Notes.

Amended 2026-10-11 (tr-aa4ge): a not-ready item is never claimable (Michael's resolution of "Where the build discipline is enforced"). `--force` no longer overrides `why_not`; it only lets a root's orchestrator hold its program.

*Amended 2026-10-11 by [Spec: Stage gates + evidence + enforcement](03-stage-gates-and-enforcement.md#amendments-to-core)*: ticket creation needs its written sections and ≥1 criterion; `acceptance-criteria` and `destination` are generated from criterion rows; `close --resolution done` is gated (refused on tickets, which close `done` only by reaching Verified); `spec accept` needs a complete spec and a `judgment`; `spec draft` needs a reason.

Amended 2026-10-11 (tr-3r0je): [Handoffs](#handoffs) added. Schema 3 adds the append-only `handoffs` table, `trk handoff add|show`, the item JSON's `handoff` summary and the additive feed kind `handoff.added`.
