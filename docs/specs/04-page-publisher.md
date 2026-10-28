# Spec: Page publisher

Project 4 of the Tracker v1 program. Spec state: **accepted** (2026-10-11; see [Acceptance](#acceptance)).

Amended 2026-10-24 by Page backends: the publisher writes through a named page backend, `folder` built in.

Amended 2026-10-24 by Page adapters: the backend is now a [page adapter](#page-adapters), a program trk runs per call ([adapters.md](../adapters.md)). trk stores no secret; the adapter keeps its own credentials and reports their expiry. Where this spec says "backend", "token" or "401", read "adapter", "the adapter's credentials" and "exit 2".

Vocabulary is [CONTEXT.md](../../CONTEXT.md). This spec extends [Spec: Core store + CLI](01-core-store-and-cli.md) and [Spec: Asks and owners](02-asks-and-owners.md). It changes them only where it says so ([Amendments to Core](#amendments-to-core), [Amendments to Asks and owners](#amendments-to-asks-and-owners)). It reads data that project 3 (Stage gates + evidence + enforcement) adds, but it doesn't wait for it. Settled inputs:
- [Page templates](../../.scratch/tracker-v1/issues/08-page-templates.md): its Resolution's "The template" is this spec's [Page template](#page-template).
- [Artifact comments as answers to asks](../../.scratch/tracker-v1/issues/09-comments-as-answers.md): its Decisions and Spec defaults are this spec's [Comments as answers](#comments-as-answers).
- [Publisher auth, client and page privacy](../../.scratch/tracker-v1/issues/10-publisher-auth-and-privacy.md): token, client, team, debounce and flusher.
- [Stage gates](../../.scratch/tracker-v1/issues/05-stage-gates.md): the stage pipeline and criterion rows.
- [ADR 0002](../adr/0002-herdr-contract.md): the feed, as amended through 2026-10-11.
- The map's Notes on **Handoffs** (Michael, 2026-10-11).

## Problem

No one can see the tracker without a terminal. Michael and the rest of the team follow agent work through hand-kept artifact boards that agents rewrite by hand. Those boards reach v67–v375, drift from the truth, and cost an agent turn every time. An agent's open ask lives in the store and in herdr's routing, but a person can only answer it with `trk answer` in a shell. The page backend can host pages and take comments, and the tracker already journals every change, but nothing joins the two.

## Solution

The CLI renders one artifact page per item (asks excepted) from the store and keeps it current, with no agent in the path:

- Every page uses the **Board** template: header strip, tiles, a "Waiting on people" box of every open ask beneath the item, rollup tables of direct children, written sections folded, evidence and review. Every section and generated row carries a stable handle.
- The **publisher** is a feed consumer, like herdr. It keeps its own cursor into the journal and maps each new row to the pages it affects. It renders those pages, compares each handle with what it last published, and sends only what changed, as one patch per page after a quiet window of about 60 s per page. A page that renders the same is never written.
- A short-lived detached **flusher** does the sending. Any CLI write that dirtied a page spawns it, and it exits when nothing is waiting. A systemd user **timer** runs about every 60 s as a backstop and also runs the **comment pull**: a person's comment on an ask's box, on any page that shows it, is recorded as a reply to that ask, and the asker confirms it as the answer (amended 2026-10-13 by tr-3vgrb, decided in tr-v69zt).
- The CLI publishes through a [page backend](#page-backends): a folder of HTML files, or a hosted service with a dedicated access token labelled `tracker`. Every page is born shared on the `tracker` team. When a backend's token nears expiry or is rejected, the publisher opens a renewal ask.

## User stories

1. As Michael, I open the program's page and see every project with its spec state and tickets done, every open decision, and every ask anywhere below that is waiting on a person, without asking an agent.
2. As Kim, I see an ask waiting on me in the "Waiting on people" box of whichever page I'm reading and comment my answer on it. Within about two minutes my comment gets the reply "Reply received. <asker> will confirm it as the answer or follow up here." and stays open for me to resolve, and the box shows my reply as awaiting confirmation. When the asker confirms it, the ask leaves every box and my thread gets "Recorded as the answer…". If I asked a question back instead, the asker follows up and the ask stays open (amended 2026-10-13 by tr-3vgrb, decided in tr-v69zt).
3. As a colleague with no tracker account, I answer an ask by commenting. The tracker registers me as a human actor and records my answer.
4. As a second commenter on an ask that is already answered, I'm told "Already answered by Kim: …" and my comment stays open.
5. As the asker, I find the answer didn't settle it and run `trk reopen <ask> --reason "…"`. The ask is back in every waiting box, and the reason is posted under the answer on the page.
6. As a worker agent, I close, claim and advance items as usual and never think about pages. They catch up on their own, and a busy minute of writes to one ticket becomes one page version, not twenty.
7. As Michael, I click a ticket row on a project page to reach that ticket's page and follow its breadcrumb back up. A JIRA epic links the top-level item's own page.
8. As Michael, I'm asked to mint a new `tracker` token 14 days before the old one expires. Once I run `trk pages setup` with the new one, the pages catch up by themselves.
9. As anyone looking into a stale page, `trk pages` shows each page's slug, version, whether it is dirty and its last error, and `trk pages render <id>` shows locally what would be published.
10. As Michael on my phone, I open one page and see every open top-level item and where it stands, follow one down to a ticket, and climb back to the top from any page by its trail. On whichever page I am reading, one box says what is in progress underneath it (added 2026-10-26).
11. As Michael on a box with no publishing set up, I run `trk pages serve` in a tab, open `http://127.0.0.1:8795/` and browse the same pages from the store as it stands: the top-level page, every item's page and each Needs-you page, each one rendered as I ask for it. From my Mac I reach the box's own tracker through an ssh tunnel, and nothing is published, written or left running when I stop it (added 2026-10-27; Michael: "like publishing but local").

## Implementation decisions

### Modules and runtime

- Still stdlib-only Python (Core's runtime rule). New modules: `render` (templates, pure functions of a store snapshot), `pages` (affected-set mapping, diffing, page bookkeeping), `publisher` (the backend interface and errors), `folder` (the built-in backend), `pull` (comments as answers), `flusher`, `serve` (the local read-only server, added 2026-10-27).
- Every new module loads lazily, only from the verbs that need it. The hot path (`claim renew` on every turn) imports none of them. Its one new cost is the [spawn check](#flusher), which runs only after a commit that wrote rows. The check lives in a small stdlib module `spawn` that `store` imports eagerly (it loads `subprocess` only to start a flusher); `timer` (the systemd units) loads lazily like the rest. A `claim renew` that sweeps another session's lapsed lease journals `claim.expired`, which marks pages, so that renew does run the spawn check; a renew that only renews never does (amended 2026-10-11 by Background flusher and timer).
- A page render is a **pure function of the store snapshot** plus the URLs its links point at: the `pages` table by default (`render.published_urls`), or a map the caller passes (`render.render`, `top.render_page` and `needs_you.render_page` all take `urls`), which is how [the local server](#browsing-them-locally) renders the same templates against its own addresses. It uses no "now", no relative ages ("3h ago") and no random ids. Times are absolute UTC (`2026-10-11 14:03Z`). The same store renders byte-identical HTML, which is what makes "unchanged pages are skipped" work.
- `RENDER_VERSION`, a constant in `render`, is bumped by any change to the templates or CSS. On startup the publisher compares it with `meta.render_version`; on a mismatch it marks every page and stores the new value. So a CLI upgrade republishes everything once.

### Which items get a page

- Every program, project, ticket (spec-bound and self-contained) and decision gets its own page once publishing is set up. **Asks get no page.** An ask appears in every ancestor's waiting box under `ask-<id>`, and as a `child-<id>` row in its parent's tables.
- Any page-bearing item with no `pages` row counts as dirty and is due at once. That covers both setup over an existing store and every new item.
- Closed items keep their pages, and those pages keep updating. What happens to them in the long run is the map's **Lifecycle of closed items** fog.

### Needs-you pages

Added 2026-10-11 by Needs-you page (`tr-n77bw`; Michael: "need some way to see what needs me").

- **One page per person**, keyed `needs-you:<actor>` in `pages` (and `page_handles`), beside the item pages and owned by no root. People with a page: every **human** actor an open ask resolves to, the default owner (when human) from the start, and everyone who has had one, since a person's page is never deleted. Agents get none. It is published like every page (team, tag `trk:needs-you:<actor>`, title "Needs you: <display>") and rendered from the store, never edited by hand.
- **Waiting on you** (`waiting`): every open ask that waits on the person now, in `trk waiting --me <actor>` order — the asks whose role resolves to them, less any they have already replied to, which wait on their asker's confirmation (spec 02, Waiting on me, amended 2026-10-27 by UP-0036). Each box is the same `ask-<id>` element the program pages show, with a line naming its root program (linked) just above it, outside the box. With none open it shows "Nothing is waiting on you".
- **Blocked on you** (`blocked`): one row `blocked-<id>` per open page-bearing item that is blocked (an open `blocks` edge or an open child ask) by something the person holds: an open ask waiting on them (the same routing as the box above), or an open decision or ticket whose effective owner they are. Rows are in tree order and link the item's page, its root program's page and, for a decision or ticket blocker, that blocker's page.
- **Marking**: a row that marks any item's page also marks every person's page when it can change what one shows: `ask.*`, `item.closed`, `item.reopened`, `edge.*`, `item.moved`, `owner.changed`, and `item.updated` on an ask or changing a title, urgency or subtype. `role.set` and `actor.updated` mark every page already. An item's first publish marks every person's page due at once, so its links resolve in the same flush. A person's own first publish journals no `page.created` (that row's item is an item); `trk pages` lists its url. Person pages go after every item page in a flush pass.
- **Comments**: the pull lists a person's page while an open ask resolves to them, or while it showed one (as for any page). A root comment on an `ask-<id>` box there is disposed of exactly as on any page (each comment on an open ask is a reply, recorded once by comment id; one on an answered ask gets "Already answered…"), so a box shown on a program page and a person's page records each comment once (amended 2026-10-13 by tr-3vgrb, decided in tr-v69zt). A root comment on the person's `waiting` box takes the asks addressed to that person when it was written (the addressee of each ask's latest `ask.opened`, `ask.reopened` or `ask.readdressed` row by then) that were open then; one such ask takes it, and none or several get "Nothing is waiting here." or "Which ask?" as on any page.
- **Pull-before-drop** covers a person's page like any page. A pull pass's listing failure no longer overwrites a `pull-before-drop:` error a send left on a page; that error already names the failure and says the page is held.
- **Links to a person's page** (added 2026-10-11 by `tr-p9mpr`; Michael: "the needs you page doesn't look linked"): in every `ask-<id>` box, the person in "Waiting on" links their Needs-you page, and the top-level program page's `header` has a "Needs you" fact linking every published Needs-you page, by actor id. Each shows as plain text until that page has a url. A person's first publish marks every published page due at once, itself included (diffing skips the ones that don't name them), so the flush that creates it also links it. Handles are unchanged.
- `pages render`, `pages republish` and `pages flush --item` take `needs-you:<actor>` as well as an item id (exit 3 for an unknown actor).
- **Schema 6** rebuilds `pages` and `page_handles` without their foreign key to `items`, copying their rows; it journals nothing.

### The top-level page

Added 2026-10-26 (Michael: "i just need a single top level page showing all the root items in trk and where they are", and "i also need each page to have a simple way to show me what is in progres underneath of that"). Before it, a root item's page had no breadcrumb at all, because the trail was its ancestors and a root has none: every program page was a dead end.

- **One page per store**, keyed `top:` in `pages` (and `page_handles`), beside the item pages and the Needs-you pages and owned by no root. It is published like every page (team, tag `trk:top:`, title "All work") and rendered from the store, never edited by hand. It is never deleted, and it exists from the first flush, as an item's page does: with no `pages` row it is dirty and due at once.
- **Roots** (`roots`): one row per **open** top-level item in rank order (amended 2026-10-26, Michael: "can we have top level only show OPEN items"; it listed closed roots greyed before): title (linking its page), kind, effective owner, tickets done/total in its subtree, open asks in its subtree, how many items below it are in progress, and its derived flags. An empty table renders one "None" line, keeping its handle.
- **Its scope** is those roots and their subtrees, closed descendants included — read once per render, so no total can count a root the table leaves out. A closed root leaves the page and takes its subtree's numbers with it; it keeps its own page and its trail back here, reachable by URL and from whatever links it. Closing a root therefore lowers `tickets done/total`, which counts live work rather than the store's history.
- **Tiles** (`tiles`): open top-level items, open projects, tickets done/total, open decisions, asks waiting and items in progress, each counted over that scope.
- **In progress** (`in-progress`): the same box every item page carries, over that scope (see the template's item 4).
- **Header** (`header`): the "top level" pill, the title, how many top-level items are open, and the "Needs you" fact linking every published Needs-you page, as a root item's page has it. It shows **no render time**: an absolute one would change the page's bytes on every render, so every flush would rewrite it.
- **No ask boxes.** An ask is answered on its own item's page or on the addressee's Needs-you page, so this page carries no `ask-<id>` handle, the pull never lists it among the pages showing an ask, and a root comment left on it is **routed** to the store's `default_owner` (a page belonging to no item has no effective owner).
- **Marking**: it summarises every open root and everything in progress below them, so any journal row that marks a page marks it too — closing or reopening a root among them, which adds or removes a line. It is published **first** in a flush pass, before any item page, because every page's trail links it: a page published after it is born with that link, so no second pass puts one in. Its own first publish marks every published page due at once, itself included, which is what gives the trail on pages that already existed (on a fresh store none has a slug yet, so nothing is marked); an item's first publish marks it due as well, so its roots table stops being plain text in the same flush. Its own first publish journals no `page.created`, as a person's page does not: that row's item is an item. The diff still decides what is sent, so a write it does not show — a rename deep in the tree — leaves its version where it was.
- `pages render`, `pages republish` and `pages flush --item` take `top:` as well as an item id and `needs-you:<actor>`.
- **No schema change**: `pages` and `page_handles` lost their foreign key to `items` in schema 6, which is what lets a key that is not an item id have a row.

### Store additions

One migration, numbered at build time after whatever schema version is current then (Asks and owners is 2; Stage gates may take 3). Like Asks and owners' `MIGRATIONS[1]`, it receives the write `Transaction`, so its rows are journaled.

| Table | Change |
|---|---|
| `pages` | new: item_id (primary key; an item id, or `needs-you:<actor>` since schema 6, which dropped the reference to items), slug (null until published), url, version (last published), state `publishing \| live`, published_at, due_at (null when clean), first_dirty_at, last_error, last_error_at |
| `page_handles` | new: item_id, handle, shell_hash; primary key (item_id, handle). The per-handle hashes of the last published render, including the root shell `@root` (see [Diffing](#diffing-and-patching)). |
| `page_comments` | new: comment_id (primary key), slug, handle, ask_id (null when it named no ask), disposition `answer \| reply \| already-answered \| withdrawn \| which-ask \| use-cli \| ignored` (`answer` only on rows written before schema 7), seen_at, body (the comment's text, kept for a `reply`; schema 7 rebuilt the table to add it and `reply`, copying its rows with no body) (amended 2026-10-13 by tr-3vgrb, decided in tr-v69zt) |
| `page_replies` | new, the reply outbox: id, slug, comment_id, body, resolve (0/1; always 0, and never read: a reply never resolves a thread; closing resolves with its own call), state `pending \| sent \| failed`, created_at, sent_at |
| `actors` | add `service_user_id TEXT` (null for actors never linked), plus `CREATE UNIQUE INDEX … ON actors(service_user_id) WHERE service_user_id IS NOT NULL` (SQLite can't add a UNIQUE column) |
| `meta` | add `publish_cursor` (journal seq), `publish_team` (default `tracker`), `publish_base_url`, `publish_token_file`, `publish_token_expires`, `publish_home` (item id), `publish_renewal_ask` (open renewal ask id, or empty), `publish_token_rejected` (when a 401 set the hold on page writes; absent otherwise; added 2026-10-11 by Token expiry and renewal ask), `render_version`, `comment_cursor` (the cross-page comment activity log's cursor, `""` after a seed of an empty log that the backend gave no cursor, stored as the opaque string the backend gave; amended 2026-10-11 by Cross-page comment feed), `page_authors` (JSON: every author id the pull has seen, with the display name the backend gave, for `trk pages authors`; added 2026-10-11 by Comments answer asks) |

- The migration registers the agent actor **`publisher`** (journaled `actor.added`). It is the actor on every write the publisher makes by itself: auto-registrations, renewal asks, dispositions. It also seeds the function role **`publisher`** in Asks and owners' role table, meaning "holds the `tracker` token", held by the default owner and journaled `role.set`.
- Page bookkeeping (`pages`, `page_handles`, `page_comments`, `page_replies`, the cursors) is publisher state and **not journaled**: a row per publish would flood the feed. One exception is journaled: **`page.created`** (data: slug, url), written once when an item's page is first published, so that consumers can find it. Answers, registrations and asks caused by the pull are journaled through their normal rows.
- **No network call ever happens inside a SQLite transaction.** Bookkeeping that follows a call (a new version or slug, the hashes, a disposition, a reply's state) is written afterwards, in a short `BEGIN IMMEDIATE` of its own.

### Setup, token and privacy

`trk pages setup --adapter "<command>" --home <item> [--team tracker]` (amended 2026-10-24 by Page adapters; it replaced `--backend`, `--dir`, `--base-url`, `--token-file` and `--expires`).

- **trk stores no secret** and never reads one. The adapter keeps its credentials (a file of mode 0600 named on its command line, its own config, a keychain) and reads them on every run. The command line is stored as `publish_adapter` and journaled, so it may name a path to a secret and never the secret itself.
- Setup runs the adapter's `describe` (name, protocol, capabilities, expiry) and its live `check`. It then stores the command line, team, expiry (`publish_expires`, absent when the adapter reports none) and home item, journaling `config.changed` per changed key, and keeps the `describe` answer and a new setup id as publisher bookkeeping. Re-running setup is how a held adapter or expiring credentials are cleared (see [Token expiry](#token-expiry)). The first setup also sets `publish_cursor` to the journal head, because every existing item is already dirty by having no `pages` row. A later setup keeps the cursor, so no row still waiting to be marked is skipped. A store set up before adapters publishes nothing until `trk pages setup --adapter` runs.
- `--home` names the open item that the publisher's own asks sit under (for this program, the Tracker v1 program). A closed or missing home is refused with rule `home-closed`, or exit 3 when missing.
- A `tool_not_allowed` refusal names the missing grant in `last_error`.
- **Every page is published with `team: <publish_team>`** (Publisher auth, Q3), which shares it with everyone on the team from birth. The client refuses to publish a new page while the team is unset. It never sends `team` on `update`, since a shared page keeps its team.
- One-time setup by Michael, outside the tracker, before the first publish: for an adapter with credentials, create the team `tracker` and give the adapter its credentials as its README says.
- Publishing stays off until setup has run. Until then, writes spawn nothing, `trk pages` says "not set up", and `flush`, `pull` and `tick` exit 1 with rule `publishing-not-set-up`.

### Page adapters

(Amended 2026-10-24 by Page adapters, replacing Page backends.) The publisher writes through a **page adapter**: any program, run as `<command> <verb>` with one JSON object on stdin and one on stdout ([adapters.md](../adapters.md) is the protocol, `trk/adapter.py` trk's client and the Python adapter kit). `trk-pages-folder` (`trk/folder.py`) is installed with trk: each page is an HTML file in a directory, with no credentials and no comments. `python -m trk.conformance -- <command>` checks any adapter.

- **Verbs**: `describe` and `check` (setup), `publish {title, html, team, tag: "trk:<item id>"}`, `update {slug, base_version, edits | html, title?}`, `sections {slug, handles}` after a conflict, `find {tag}` to recover a page by its tag. An adapter with capability `comments` also has `comments {slug}` and `reply {slug, comment_id, body, resolve, idempotency_key}` (`resolve: true` only on a closing reply), and with `activity` the cross-page `activity {cursor, limit}`. The pull asks `describe` for the capabilities each run. An adapter without comments is never pulled, and pull-before-drop is skipped.
- **Exit codes**: 0 ok; 2 held (credentials, permission or configuration: ends the run, holds page writes and opens the renewal ask, see [Token expiry](#token-expiry)); 3 unavailable, may have taken effect; 4 refused, with `{error, ...}` on stdout. `conflict` takes the [Conflicts](#conflicts) path. Every other refusal goes into the page's `last_error` and is never retried in the same run. Any other exit, or a malformed answer, after a write counts as possibly applied. Every error message starts with the call's name.
- **Retries** belong to the adapter: one over a network retries reads and `update` (made safe by `base_version`, which the adapter must enforce), and never blindly retries `publish` or `reply`.
  - **Non-idempotent calls are never blindly retried.** Before `publish`, the page row is set to `state: publishing`. After an ambiguous failure, or on any later run that finds a `publishing` row, the publisher looks for the page tagged `trk:<item id>` with `find` and adopts it (slug, version) before publishing again.
  - For `reply`, an ambiguous failure leaves the outbox row `pending`. Before resending, the pull re-lists that comment's thread and marks the row `sent` if a reply with the same body is already there. The `idempotency_key` (`trk-reply-<outbox row id>`) lets an adapter whose host deduplicates post it at most once all the same.
- Tests run an in-process fake adapter through the same protocol (see [Testing decisions](#testing-decisions)).

### Page template

The Board layout on every page kind (Page templates, The template). Static HTML, inline CSS, no script, no external resources (a strict content security policy), light and dark tokens, usable at phone width. Folding is native `<details>`. Every element below carries `data-comment-id="<handle>"`.

1. **Header strip** (`header`): breadcrumb — "All work" linking [the top-level page](#the-top-level-page), then every ancestor, each linking its page (amended 2026-10-26: the trail always starts at the top-level page, so a root item's page shows that one crumb where it used to show none) — kind pill (with form and subtype for work items), title, derived flags (blocked, in progress, urgent, and closed with its resolution), effective owner, live claim (actor only: the lease time is never shown, so `claim.renewed` changes nothing), JIRA link, id and `updated_at`. Inside it, a **`handoff`** line once handoffs exist (see [Handoffs](#handoffs)).
2. **Tiles** (`tiles`):
   - Program: projects, specs accepted, tickets done/total in the subtree, open decisions, asks waiting in the subtree, and fog patches (top-level list items in `not-yet-specified`).
   - Project: a stage funnel (`stage-funnel`, tickets per stage) and tickets done/total.
   - Ticket: a stage pipeline (`stage`), one cell per stage (`stage-framed`, `stage-specd`, `stage-built`, `stage-merged`, `stage-verified`), each showing the result and short sha of its latest counting evidence.
   - Decision: subtype, owner, and the outcome once closed.

   Stage cells render only once Stage gates' stage column exists. Until then a ticket's tiles show its status.
3. **Waiting on people** (`waiting`): every open ask in the item's subtree, the item's own child asks included. In `trk waiting` order (highest urgency score first), each box showing the same reason line as an "Urgency" fact, with the render time inside the cache note, e.g. `waiting 52m (cache warm, 8m left, as of 14:05Z)`; the render reads the clock once, so the line and order are as of that time, and nothing republishes a page just because minutes passed (amended 2026-10-22 by tr-qwwsc). Each box (`ask-<id>`) shows the ask's title as its heading, the whole question rendered as markdown like an item's sections (amended 2026-10-13 by `tr-s2rfp`: the 240-character excerpt cut questions off; feed rows keep the excerpt), role and resolved addressee, asker, opened time, the item it sits on (linked) and "Answer by commenting on this box". With none open, it shows a green "Nothing is waiting on people".
4. **In progress** (`in-progress`), added 2026-10-26: one row `progress-<id>` per open item anywhere in the subtree that work has started on — an item of any kind with a live claim, or a ticket at Built, Merged or Reviewed, whose work exists but is unverified — most advanced first (claimed before unclaimed, then by stage reached, then by id). Each row shows the item (linking its page), kind, stage, who (the claim's actor, else the effective owner), since (the claim's `taken_at`, else `updated_at`) and its flags. With nothing in progress it shows "Nothing below this is claimed or built". A page with **nothing below it at all**, as every ticket has, renders no box and so no handle; the top-level page renders it over the open roots it lists and their subtrees. A claim counts while the store holds it open, never by comparing its lease to the clock: a lapsed lease is ended by `trk sweep`, and that `claim.expired` row marks the page like any other, so until a sweep runs this box shows a lapsed claim exactly as the header's existing "in progress" flag does.
5. **Rollup tables, direct children only**. Each row is `child-<id>` and links the child's page. **Each child appears in exactly one table**, the first that matches:
   - Program: project → `projects` (title, owner, spec state, tickets-done meter, open asks in its subtree, flags; closed rows greyed); open decision → `open-decisions` (title, subtype, owner, open asks, flags); decision closed `done` → `decisions-so-far` (close order: title, outcome); any other child closed `out-of-scope` → `out-of-scope`; everything else (asks, tickets, programs, decisions closed otherwise) → `other-children`.
   - Project: ticket → `tickets` (Ticket | Owner | PR | Stage | Open asks | flags; closed rows greyed; self-contained tickets marked); everything else (asks, child projects, decisions) → `other-children`.
   - Ticket and decision: every child → `children` (asks and any other work items, with outcome when closed).
   - Closed rows show their outcome. An empty table renders as one "None" line, keeping its handle.
6. **Written sections, folded**: one `<details>` per section, with handle = the section handle, in the data model's order, then extras in creation order. The gate-relevant one is open by default: `destination` on a program, `acceptance-criteria` on a project, `what-to-build` or `desired-behaviour` on a ticket, `question` on a decision.
   - Markdown renders through a small built-in converter: headings, lists, emphasis, code, links and tables. Raw HTML in a section is escaped, never passed through. Only `http`, `https` and `mailto` link schemes become links; any other scheme stays text.
   - `decisions-so-far` and `out-of-scope` on a program fold the same way.
   - **Criterion lists**: once Stage gates' criterion rows exist, `acceptance-criteria` (tickets, projects) and `destination` (programs) render from them. Each criterion gets one row, `ac-<n>`, showing its text, version and evidence status. A retired criterion stays as a struck-through row, so its handle never drops. Until then these render as written sections under the same handle, so the handle never changes.
7. **Evidence** (`evidence`), once Stage gates' evidence table exists: one row per evidence row, `evidence-<id>`, showing gate (a stage, `accept` or `close`; amended 2026-10-11 by Stage gates + evidence + enforcement), kind, criterion, result, sha, actor and time. Retracted rows are struck through.
8. **Review** (`review`): the live decision ledger, one row per entry, `ledger-<n>`. Its row shape belongs to Decision ledger in the tracker (`tr-ngyp0`, resolved 2026-10-11): kind, decided, alternatives, why, origin, latest verdict, settlement, linked ask/ticket/ADR, plus a GREEN/YELLOW/RED lane chip in the ticket tiles. The handle and placement (ticket pages, after evidence) are reserved here. It renders only once ledger rows exist.

**Links**: a link to an item whose page isn't published yet renders as its title in plain text. It becomes a link when that item's `page.created` marks the page (see [Affected pages](#affected-pages)).

**Handle rules**:
- A handle is unique within a page, and the same thing always gets the same handle on every page (`ask-<id>` above all).
- A handle is never renamed. It disappears only when the thing it names leaves that page, for example when an ask closes or an item moves away. [Pull-before-drop](#comments-as-answers) protects ask threads when that happens.
- **Reserved names**: `header`, `handoff`, `tiles`, `stage`, `stage-funnel`, `waiting`, `projects`, `open-decisions`, `decisions-so-far`, `out-of-scope`, `other-children`, `tickets`, `children`, `evidence`, `review`, `in-progress`, `roots`, and every name starting `child-`, `ask-`, `ac-`, `evidence-`, `ledger-`, `stage-`, `progress-` or `s-` (`in-progress`, `roots` and `progress-` added 2026-10-26).
  - A written section whose handle is reserved on its page renders under `s-<handle>`. The `s-` prefix is reserved, so this can't collide.
  - `out-of-scope` is a written section on a project and a generated list on a program, so it is reserved only on program pages.
  - Core's section rules are unchanged: only the page handle differs.
- Plain layout elements have no handle. A person's comment on one is routed to the item's owner (see [Comments as answers](#comments-as-answers)).

**Size**: a page over 2 MiB is never sent. It records `last_error: too-large` and stays dirty. Direct-children rollups keep real pages far below the limit (the prototype's 15 views came to about 100 KB).

### Handoffs

From the map's Notes (Michael, 2026-10-11): sessions checkpoint into handoffs, which are records kept with an item and retrievable later to restart work. Their storage and verbs are a Core follow-up.

- A page never shows a handoff's body.
- The header carries one `handoff` line for the item's latest handoff: when it was written, by whom, its first line, and the command that retrieves it (`trk handoff show <id>`, or whatever verb the Core follow-up names). It is not a hyperlink in v1, because a handoff has no page of its own.
- The line renders only once handoff storage exists, and it changes only when a newer handoff is recorded. The Core follow-up's handoff row marks the item's page.

### Affected pages

The publisher reads the journal from `publish_cursor` and marks the pages each row affects. Over-marking is harmless, because unchanged renders are skipped.

| Row | Pages marked |
|---|---|
| `item.created`, `item.closed`, `item.reopened`, `item.updated`, `spec.changed`, `stage.changed`, evidence and criterion rows (project 3), `ledger.*` rows (added 2026-10-11 by [Spec: Stage gates + evidence + enforcement](03-stage-gates-and-enforcement.md#amendments-to-page-publisher)), `claim.taken`, `claim.released`, `claim.expired` | the item and every ancestor |
| `item.updated` changing `title`, `owner.changed` | as above, plus every descendant (breadcrumbs, inherited owner) |
| `item.moved` | the item, every old and new ancestor, and every descendant |
| `ask.*` | the ask's parent and every ancestor (waiting boxes, child row) |
| `edge.added`, `edge.removed` | both ends and their ancestors |
| `page.created` | the item itself, its parent and every descendant (their links to it, and its own: an ask box's "On" names its parent); **due at once**, with no quiet window |
| `actor.updated`, `role.set` | every page. These rows are rare, and diffing skips the unchanged pages. |
| `claim.renewed`, `actor.added`, `config.changed`, `page.created` for asks (never written), unknown kinds | none |

**Timing**
- Marking sets `due_at = max(due_at, row.at + 60 s)`, and sets `first_dirty_at = row.at` only when it is null.
- A page is **due** at the earlier of `due_at` and `first_dirty_at + 5 min`. So a page that never goes quiet still updates every five minutes.
- A page whose last attempt failed is due no sooner than `last_error_at + 5 min`, whatever the other two say.
- A successful send clears `due_at`, `first_dirty_at` and `last_error`.
- `--now` treats every dirty page as due, or only the pages named with `--item`, failed ones included.

**Recording marks**
- The cursor advance and the marks are written in one transaction, so after a crash the rows are marked again rather than lost.
- Marking runs at the start of every flush and pull, and again before each sleep of a waiting flusher. The rows themselves were already committed by whichever CLI wrote them.

### Diffing and patching

- A page's render is a tree of handled elements.
  - The **shell** of a handle is its outer HTML with every nested handled element replaced by a placeholder naming its handle.
  - The **root shell** `@root` is the whole document (`<head>`, `<style>`, layout) with every top-level handle replaced the same way.
  - `page_handles` stores each shell's hash from the last successful publish.
- If `@root` changed, the **whole body** is sent. Otherwise a handle **needs sending** when its shell hash changed or it is new.
  - The patch replaces the **outermost** handles that need sending, and each replace carries its nested handles with it. A handle whose own shell is unchanged is recursed into, never sent.
  - So one ticket closing patches one `child-<id>` row, plus `tiles`, and `waiting` if that changed, not the whole table.
  - A handle that vanished is covered by its parent's shell changing, because the placeholder list differs.
- More than 50 edits also means the whole body is sent. Whole bodies go through `update {body}` with `base_version`.
- When the item's title changed, the same `update` call also sets `title`.
- Each page gets one write per flush, all or nothing, so each flush makes at most one new version per page.

### First publish

- A new page is published with its full render, in depth order (ancestors first), so breadcrumbs link up wherever the ancestor already exists.
- Each first publish journals `page.created`. That row marks the page itself, the parent and the descendants due at once, because their plain-text links to the new page can now become links. A page that doesn't link itself renders the same and is not written again.
- **A flush keeps going until a pass creates no new page and leaves no page due at once.** So the run that creates a subtree's pages also turns their cross-links into links. A parent therefore gets two versions at birth (the publish, then the link patch). A second `flush --now` right after writes nothing.

### Conflicts

- Every write carries `base_version = pages.version`. A `conflict` refusal (with `currentVersion` and `changedSections`) means something other than the tracker wrote the page.
- The publisher then fetches the changed handles with `sections {handles}`, adopts `currentVersion`, and re-sends every handle whose server source differs from its render. If a changed section isn't one of its handles, or any handle is missing on the server, it sends the whole body on `currentVersion` instead.
- It retries once per flush. A second conflict leaves the page dirty with `last_error: conflict`.
- It never fetches and rewrites blindly. Only a conflict leads to a read. A conflict whose `changedSections` is empty (the other writer changed nothing inside a handle) sends the whole body, since only that puts back what lies outside the handles (amended 2026-10-11 by Edit conflicts and republish).

### Flusher

- **`<store>/pages.lock`** (an `flock`) serializes all publisher network work: flush, pull, and the outbox. It is held per unit of work (one page's publish, or one pull pass), never while sleeping. It is always taken before any SQLite transaction, never inside one, and it stays held through the unit's own short bookkeeping transactions so that no second flusher sends the same page meanwhile; a SQLite transaction is never open across a network call (amended 2026-10-11 by First publish).
- **Spawn check**, after every committed write: if publishing is set up and the transaction wrote any row that can mark pages (see the table), the CLI checks for a live flusher through `<store>/flusher.pid` (the pid is alive and its command line is `trk pages flush --wait`, read from `/proc/<pid>/cmdline` or, where there is no /proc, from `ps` — one fork per check off Linux, and a dead pid is ruled out by a signal first so only a live one is looked up. Amended 2026-10-27: off Linux a live pid used to be trusted without reading its command line, so a pid the OS had recycled onto an unrelated process suppressed every spawn until someone removed the file, leaving the timer to publish. A live pid whose command line cannot be read at all — another user's process, no `ps`, a `ps` that hung — is still trusted). If there is none, it starts `trk pages flush --wait` detached (`start_new_session`, with stdio to `<store>/flusher.log`) and returns without waiting.
- **`trk pages flush [--wait] [--now] [--item <id>]…`**:
  - Without `--wait`, it marks, publishes what is due now (including [first publish](#first-publish) passes), and exits.
  - With `--wait`, it writes `flusher.pid` and loops: mark, publish every due page in depth order, then sleep until the next `due_at` among pages that haven't failed. It **exits when no page is dirty except failed ones**, which it leaves to the timer.
  - A 401 also ends the run. Before exiting, it removes the pid file and compares the journal head with `publish_cursor` once more; if the head is ahead, it loops again. This closes the race with a write that committed while the flusher was finishing.
- A failing page keeps its marks and `last_error`. One failing page never stops the others.
- There is no daemon: the flusher exits when idle, and if one dies, the timer catches up.
- The spawner records the child's pid in `flusher.pid` as soon as it starts it, and the flusher registers itself there too; registering and unregistering are serialized by `<store>/flusher.lock`, and the pid file is written aside and renamed into place, so the spawn check never reads it half-written. After a 401 the flusher exits without the head-vs-cursor second round, since nothing can be sent until setup has a new token. One that finds another live flusher registered exits at once, so two flushers started by racing writes leave one — the same way out of the window between a spawn's `Popen` and its child's exec, during which the recorded pid still reports the spawner's own command line and so reads as no flusher (noted 2026-10-27; waiting for the exec would make every spawning write pay for it); when it loops again after the head-vs-cursor recheck it registers again the same way. Before each sleep it marks again, so writes that committed during a pass (whose spawn check found it live) are counted. `--wait` does not combine with `--now`. The flusher runs as `<python> -m trk pages flush --wait`, the same interpreter and package as the writer that started it, and `flusher.log` moves to `flusher.log.1` past 1 MiB (amended 2026-10-11 by Background flusher and timer).

### Timer and the pull

- **`trk pages tick`** runs the pull, then `flush` without `--wait`. It runs in the foreground and takes `pages.lock` per unit of work like everything else, so it interleaves safely with a running flusher.
- **`trk pages timer install|remove|status`** writes and enables, or removes, a systemd user unit pair `trk-pages.{service,timer}` (`OnUnitActiveSec=60s`, `ExecStart=<trk path> pages tick`, with `$TRK_HOME` passed through). Installing it changes the user's systemd setup, so its first install on Michael's machine is an ask to Michael, as Core 11's hooks were.
- `tick` can be run by hand and in tests. None of this depends on herdr.
- `tick` reads the token file like the flusher, and one client serves its pull and its flush. `tick --json` is flush's report plus `pull`, the pull's report (amended 2026-10-11 by Comments answer asks). The timer unit also sets `OnActiveSec=60s`, since `OnUnitActiveSec` alone never fires before the backend has first run; the units go to `$XDG_CONFIG_HOME/systemd/user` (default `~/.config/systemd/user`) (amended 2026-10-11 by Background flusher and timer).

### Comments as answers

This section carries Artifact comments as answers to asks: Michael's decisions and the agent's spec defaults as recorded there.

**Reading comments**
- When the backend offers no activity log, the pull calls `list_comments {slug}` for every page that shows an open ask (its waiting box isn't empty), and for every page that showed an ask in its last publish. When it offers one, it makes one cross-page activity call from `comment_cursor` instead.
- **The activity log** (amended 2026-10-11 by Cross-page comment feed; it is called the activity log so that "feed" keeps meaning Core's change feed). When `offers_activity` is true (asked once per run), the pull calls `activity` from `comment_cursor` with `limit` 500, paging while `hasMore`, and stores the last cursor it got only after that pass's dispositions commit, so a crash re-reads rather than skips.
  - Only `comment` events on tracker pages (a slug in `pages`) are root comments; replies use their `rootCommentId`. Only pages the per-page read may list count: a page whose first publish may not have finished is left out, as it is there. While any page's first publish is in doubt (`publishing`), a comment event on a slug no live page has may be on that page, so the stored cursor stays at the start of the log page holding the first such event; the next pass reads it again, and once a flush adopts the page its comments are read (`page_comments` skips the rest) (amended 2026-10-11 by Cross-page comment feed, for its ledger-13). `reply` events (the tracker's own replies among them) and `status` events are never read: a status event's author fields describe the root comment's author, so it would look like a person's comment on the box. A `comment` event whose `authorKind` isn't `human` is `ignored`, as an assistant's is (in both modes; a per-page comment with no `authorKind`, from a backend without author ids, still counts as a person's), and the rest of the disposition table is the same in both modes. Events on other pages the token's owner owns are passed over and leave no row.
  - With no cursor stored, the pull drains the log from a null cursor to seed it and acts on none of it: the log replays history from before setup. An `invalid_cursor` refusal is recorded and re-seeds the same way rather than replaying. A seed whose drain fails part way stores nothing, so the next pass seeds again. A seed that finishes is stored even when the backend gives no cursor (an empty log): `comment_cursor` is then `""`, and the next pass reads from the oldest event, so the first comments are new, not history.
  - A seeding pass also runs the per-page read, and so does a pass whose `offers_activity` or activity call fails (a `tool_not_allowed`, a 5xx after its retries). The per-page read is idempotent by `page_comments`, so a comment written before the seed or during the failure is still read once. A failure part way keeps the events read before it and stores the cursor they reached.
  - An activity-log or `offers_activity` failure goes on the `last_error` of every page the per-page read would list, and in `errors` (one entry with a null item when no page shows an ask); the next such call that works clears it. A tool error's line is `activity: <code> (<detail>)`, so a missing grant reads `tool_not_allowed (activity)`.
  - A reply queued by the pass that sends it goes out without a listing first; only a reply an earlier pass left `pending` has its thread listed before it is sent again (see the outbox). So a tick with the activity log costs `offers_activity`, one activity call per 500 events and one call per reply, however many pages show asks.
  - `pages pull --json` (and `tick --json`'s `pull`) carries `activity`: null when the backend offers none, else `{seeded, events}`.
- Every root comment is read once per comment id. `page_comments` makes the pull idempotent: a comment that already has a disposition is never acted on again. The check and the insert happen in the same `BEGIN IMMEDIATE` as the store change the disposition causes, and in the same transaction as its outbox reply.
- Only root comments anchored on an `ask-<id>` handle or on `waiting` can answer an ask. Any other root comment from a person (`authorKind: human`, or none in a per-page read from a backend without author ids) with some text is **routed** (amended 2026-10-20 by tr-yd424, replacing the pointer at the ask boxes of 2026-10-11): the transaction journals `comment.routed` (below), keeps the comment's text in `page_comments.body`, and queues one unresolved reply, "Sent to <owner display name>; they'll reply here." The owner is the item's effective owner; on a Needs-you page it is the page's person. The thread stays open. The disposition is `routed`, with no ask, so the reply never holds a drop. An `ask-<id>` handle naming no ask is routed the same way. A comment with no text, one whose thread is already resolved or already has a reply by an assistant (the tracker's own replies can't be told from other assistants'; as after a store restore), and a root comment the wire marks `authorKind: assistant` (the tracker's own replies among them), is `ignored`: no row, no reply. The per-page read lists only pages that show an open ask, so a comment on any other page is read only where the backend offers the activity log; that log covers every page. Every root comment comes from a person, because the backend only lets assistants reply. The tracker's own replies are never read as answers.
- The new root comments of one pass are disposed of oldest first by `createdAt`, across every page read, so an ask's replies are recorded in the order they were written wherever they are (amended 2026-10-13 by tr-3vgrb, decided in tr-v69zt). A root comment the wire marks `authorKind: assistant` is `ignored`, and so is an empty comment. A comment the wire gives no readable `createdAt` counts as written at the pull. Every comment that is read (on `waiting`, an ask's box or routed) links its author, registering one if needed, whatever its disposition; an `ignored` comment registers no one (amended 2026-10-11 by Comments answer asks).
- A listing that fails records its error on that page's `last_error` and leaves the page out of the pass; the next listing that works clears it. A pull's errors never set `last_error_at`, the flush's failure pause, so a page whose comments can't be read still publishes when it falls due, and `flush --wait` skips only pages whose send failed. `pages pull --json` carries the token check's fields, `comments` (one entry per disposition made: comment, slug, handle, ask, disposition), `replies` (`sent`, `failed` and `pending` comment ids) and `errors` (per page) (amended 2026-10-11 by Comments answer asks).

**The commenter**
- A commenter maps to an actor by the backend's opaque author id (`service_user_id`), never by display name. A backend whose comments carry no `authorId` can't answer asks by comment: the pull points people at `trk answer`.
- `trk actor link <actor> --service-user <id>` links a registered actor to an author id. It is journaled as `actor.updated` with `fields: ["service_user"]`, and re-linking moves the id from any actor that held it. `trk pages authors [--json]` lists every author id the pull has seen, with display name and linked actor, so Michael can link Kim's and his own id once.
- An author id that isn't linked is registered as a `human` actor and linked to it, journaled `actor.added` by `publisher`. Its id is a slug of the display name, de-duplicated with `-2`, `-3`; its display is the display name as given. A later `actor link` can move the id onto the right actor. The auto-registered actor stays, since actors are never removed in v1.

**The reply and the answer** (with author ids; amended 2026-10-13 by tr-3vgrb, decided in tr-v69zt)
- A person's root comment on an open ask's `ask-<id>` box, on any page, created after the ask's latest `ask.opened` or `ask.reopened` row, is a **reply**, not yet the answer: a person may be asking a question back ("I need to see the whole question…"). In one transaction, the publisher:
  - journals **`ask.replied`** with actor = the commenter and data `slug`, `comment_id` and `excerpt` (the comment as plain text, as `answer` excerpts are). The ask stays open, keeps blocking its parent and stays in `trk waiting`;
  - records the disposition `reply` with the comment's full text in `page_comments.body`;
  - queues the outbox reply, `resolve: false`: "Reply received. <asker display> will confirm it as the answer or follow up here." No reply the tracker sends resolves a thread, and it never resolves a thread of an open ask: the person resolves those when they have read them (amended 2026-10-11 by Reply to an answering comment without resolving its thread). The one case where the tracker resolves is [Closing threads](#comments-as-answers) below, once the ask is closed and its box has left the page (Pub 17, revised 2026-10-19 by tr-hr642).
- A later comment on a replied, still-open ask records another reply the same way. The ask's item JSON carries `replies: [{comment, actor, at, excerpt, body}]` (`body` is the full text of a `trk reply`, null for a page comment; amended 2026-10-24 by tr-bvxs5) (those since its latest opening, oldest first), `show` prints a `replied:` line naming the latest, and its box on every page (program pages and Needs-you pages alike) shows the latest reply's excerpt, its commenter and time, and "awaiting confirmation" under the question.
- The commenter must be one who could answer: answerer rules are Asks and owners' (the resolved actor or any human), so a person's comment always qualifies.
- **The asker confirms**: `trk answer <ask> --from-comment <comment id> --outcome "<gist>"` closes the ask. The comment must be a reply recorded on this ask (rule `no-reply` otherwise), and only the asker, the parent's effective owner or a human may run it (rule `ask-permission`). It sets the `answer` section to the comment's full text, closes the ask `done` and journals `ask.answered` with actor = the commenter (`proxy` as usual), `via: "page"`, the reply's `slug` and `comment_id`, and `confirmed_by` (who ran it). The same transaction queues "Recorded as the answer to <ask title>, by <name> (for <addressee display>). Ask closed." on the comment's thread, unresolved. `--from-comment` takes no `--answer`; one of the two is required.
- `trk answer <ask> --answer …` without `--from-comment` (a person answering from the CLI) closes the ask directly, as before.
- **A reply with no page** (tr-zb4rn, 2026-10-24; Michael's ruling of 2026-10-23: "a reply is only a proposed answer, it may or may not answer the question"): `trk reply <ask> --body @F|-` journals the same `ask.replied` row as the writer, with comment id `reply-<journal seq>` and the full text in `data.body` (the only place it is kept). The writer may be the asker, the addressee or any human (rule `ask-permission`), and the text is held to `answer`'s body limit. `contract/changes.schema.json` requires a non-empty `slug` on `ask.replied`, so the row carries the placeholder `slug: "cli"`; the `reply-` id is what marks it page-less. The reply appears in `replies` like any other. `trk answer <ask> --from-comment reply-<n>` confirms it (asker, owner or a human, as above) and journals `ask.answered` with actor = the reply's writer, `via: "cli"`, `comment_id` and `confirmed_by`. The asker's own reply, from either source, is listed but refused as the answer (rule `own-reply`). Closing an ask queues no outbox note for a page-less reply.
- **Every close tells the replies it leaves**: whenever an ask closes other than by confirming a reply (a CLI answer, a `human-review` answer, `pages setup` answering the renewal ask, a withdrawal, its parent's close, or `close` as `duplicate`/`superseded`), the same transaction queues one note, unresolved, on each reply thread recorded since the ask's latest opening: "Ask closed: answered by <name> via the CLI." (or "through a recorded review"), "Ask withdrawn: <reason>." ("Ask withdrawn: parent closed (<resolution>)."), "Ask closed as a duplicate of <id>: <outcome>." or "Ask closed, superseded by <id>: <outcome>.". Confirming one reply tells the other threads "Ask closed: <name>'s reply was recorded as the answer." (amended 2026-10-15 by tr-3vgrb ledger-6.)
- **Later comments** on an answered ask, on any page or after a CLI answer, get "Already answered by <name>: <outcome>", left open (`already-answered`). They never reopen the ask.
- **Comments on a withdrawn ask**, including (in a store written before 2026-10-22, tr-jwbg9) one withdrawn automatically when its parent closed, get "Withdrawn: <outcome after 'withdrawn: '>", left open (`withdrawn`). For a parent close, that reads "Withdrawn: parent closed (done)".
- **A comment on `waiting`** counts as a reply to the one open ask in that box only when exactly one is open. The box is taken as it stood when the comment was written: the asks whose latest opening came before the comment and that were not closed until after it. When that one ask has closed since, the comment gets the reply a comment on its own box would ("Already answered…" or "Withdrawn…") (amended 2026-10-11 by Comments answer asks). With two or more, it gets "Which ask? Comment on its box instead." With none, it gets "Nothing is waiting here." Either way the comment stays open (`which-ask`).
- **A comment from before an open ask's latest opening** is no answer. If the ask was answered before (it has since been reopened), the comment was written while that answer stood and gets "Already answered by <name>: <that outcome>", left open (`already-answered`); otherwise it is `ignored` (amended 2026-10-11 by Comments answer asks).
- A comment that can't be recorded as a reply (a linked agent other than the addressee may not answer, or the answer section would refuse the text) gets "<the refusal>: answer with `trk answer <id>`." and the comment stays open (`use-cli`) (amended 2026-10-11 by Comments answer asks).
- **Without author ids** (a backend that gives none): an open ask's comment gets "Answering by comment isn't enabled yet: answer with `trk answer <id>`." and stays open (`use-cli`). Later comments on an answered or withdrawn ask are handled as above.

**The outbox**
- Every reply is queued in `page_replies` in the same transaction as its disposition, so a crash between the store and the page is retried, never lost.
- The pull sends `pending` rows under `pages.lock`, with `resolve: false`, a row an older build queued with `resolve: 1` included, except a closing reply (below), which resolves its thread in the same call. No other reply resolves its thread. A person resolving a thread later is no new comment and changes nothing in the store: status events are never read, and a comment is disposed of once by id (amended 2026-10-11 by Reply to an answering comment without resolving its thread).
- Before sending a `pending` row, the pull looks at its thread, as listed in this pass or listed now when the page wasn't (it no longer shows an ask). A reply already there with the same body marks the row `sent` without sending. A refusal that isn't ambiguous marks the row `failed`; an ambiguous failure leaves it `pending` for the next pull. A `failed` row is never sent again (amended 2026-10-11 by Comments answer asks).

**Closing threads** (Pub 17, revised 2026-10-19 by tr-hr642; Michael, comment 5f95dcc5 on tr-fn54k: "since we durably record answers elsewhere, we can remove comments for answered questions that are removed from the page (or mark them resolved)"). An answer is recorded in the store (the ask's `answer` section and its `ask.answered` row), so the page thread that carried the question is only clutter once the box is gone: one person's page held about 40 outdated threads that buried the open asks.
- **When.** A thread is closed when it is a root comment anchored on an `ask-<id>` handle (`page_comments.handle`), the ask is closed (answered, withdrawn, a duplicate or superseded, or closed with its parent), and the box has left the page: the page is published and the `page_handles` of its last send holds no such handle. A box still on the page is never closed, so closing waits for the flush that drops it (pull-before-drop passes that run during the flush may do it for a page sent earlier in the same flush). An open ask's thread, a comment on `waiting` (a box shared by every ask) and a comment on anything else (a section, a row, the page) are never closed. A reopened ask is open again and keeps its thread.
- **What.** One closing reply, "Closed: <outcome>. Recorded on <ask title> (<ask id>)", sent with `resolve: true` so the thread resolves in the same `reply` call. `<outcome>` is the ask's `outcome` as stored, trailing full stop dropped, so a withdrawal reads "Closed: withdrawn: <reason>. Recorded on …". Every other reply is sent with `resolve: false`. There is no separate resolve call: the backend interface has none, and a hosted backend's token need not be granted one (amended 2026-10-20 by tr-hr642, ledger-6).
- **Outbox and crash safety.** In one transaction the pull inserts the closing reply into `page_replies` and a marker into the `meta` key `page_closings` (JSON `{comment id: {reply: <outbox row id>, state, handle}}`; `handle` is the thread's `ask-<id>` box, kept because a thread `tidy` reads from the page may have no `page_comments` row). The marker is what makes it idempotent: a comment with a marker is never queued again, whatever its state, and the reply row is sent by the outbox's existing rules (a reply already on the thread with the same body marks it `sent`; an ambiguous failure leaves it `pending`; a refusal marks it `failed`, never sent again by itself, `pages reply resend` revives it). A marker is `queued` until its reply is sent, then `resolved`. A row is a closing reply only by its marker, never by its `resolve` column, so a row an older build queued with `resolve: 1` stays a plain reply. The result of the send decides: `resolved: true` and no `note` marks the marker `resolved`; `resolved: false` or a `note` is final: the reply is `sent`, the marker becomes `gone`, the thread is left open and the error is reported once. `not_found` (the thread is gone, or the token's owner may not resolve it) is final too: the reply is `failed`, the marker `gone`. When an ambiguous send is found on the thread by the next pull, the marker is `resolved` if the thread reads as resolved, else `gone` with the error reported. Any other failure goes on the page's `last_error` and in `errors`, and the next pull retries the send.
- **Reopened before sent.** Before it sends a `queued` closing reply, the pull checks again that the ask is closed and its box is off the page. If not (the ask was reopened, or the box is back), the never-sent row is deleted and the marker dropped, so a later close queues a fresh closing reply (amended 2026-10-20 by tr-hr642, ledger-7).
- **Last word.** The pass queues closings after it has sent its other replies, and never on a thread that still has a `pending` reply, so the closing reply follows every earlier one. A reopened ask returns under the same box, but the old threads stay resolved (the backend has no un-resolve), so a reopen's "Reopened by…" note lands in a resolved thread; new comments on the returned box start new threads.
- **No hold.** A closing reply never holds a page back in pull-before-drop: it exists only once its box has left, so no unread comment hangs on it.
- **Only new closes.** The first pass of a build with this step stamps `meta.closings_from` and closes nothing; a pull then closes only asks whose `closed_at` is at or after the stamp. Asks closed earlier are `trk pages tidy`'s. So merging this never replies on, or resolves, a page's old threads before someone asks (amended 2026-10-19 by tr-hr642, ledger-3).
- **`trk pages tidy <page-key> [--dry-run] [--json]`** (`<page-key>` is an item id or `needs-you:<actor>`) closes, once, the threads on that page whose ask is already closed and whose box has left it, whenever the ask closed. It lists the page's threads (`list_comments`, resolved ones included), so a thread no pull disposed of is covered. It skips, with a reason in `skipped`: an ask still open (`ask-open`), a box still on the page (`box-on-page`), a handle naming no ask (`no-such-ask`), a thread already resolved (`resolved`), one already closing (`already-closing`) and one whose earlier reply is still pending (`reply-pending`, in dry-run too), and `not-queued` for a thread another pull queued first. Threads outside boxes are not listed. It then sends the outbox under `pages.lock`, as a pull does. `--dry-run` queues and sends nothing, and lists the threads it would close in `closing`. A page with no published page is refused with rule `no-page`, an unknown page key is exit 3. The report carries `page`, `slug`, `closing`, `skipped`, `replies`, `resolved`, `errors` and the token check's fields.
- **No new journal kind and no contract change.** Closing replies, markers and resolves are publisher bookkeeping; nothing journaled changes and no row in `contract/` does. The store has no new table: `page_closings` and `closings_from` are `meta` keys.

**Pull-before-drop**
- Before the flusher sends a page whose render drops an `ask-<id>` handle, it runs a full pull pass first. That way no comment written before the drop is left unread, on any page, even when an ask moves.
- The pass runs under the `pages.lock` the page's unit of work already holds, without the token check (the flush made it), and before every such page, so a box leaving D, J and P is pulled three times; a comment written between two of those sends is still read. The page is rendered again after it, since the pass may have answered an ask. `pages republish` does the same (amended 2026-10-11 by Reopen and withdrawal on the page).
- When the pass can't read the dropping page's own comments (the activity log failed or isn't offered, and listing the page failed too), that page is not sent: it fails with `last_error: pull-before-drop: <the error>`, so the failure pause applies, and it is sent once a read works. An activity log read whole covers every page. Failures listing other pages don't hold it back, because those pages still show the box or showed it (amended 2026-10-11 by Reopen and withdrawal on the page).
- A send that drops an `ask-<id>` handle puts the page in `pages_reread` (a `meta` key, publisher bookkeeping), and the next pull lists it once more, so a comment written between the pass's listing and the send is read too; the first listing of the page that works takes it off, and so does a pass that reads the activity log whole, since the log holds that comment. (amended 2026-10-11 by Reopen and withdrawal on the page).
- A reply the pass could not send and resolve also holds the drop back, as a failed listing does: while a reply on a comment on a dropped box is `pending`, or was failed by a pass during the running flush (an ambiguous failure or a refusal), the page fails with `last_error: pull-before-drop: …` and the failure pause, and every other page that would drop that box in the same flush is held too. The next flush retries the reply first. A reply marked `failed` is never sent again by itself, so once the flush that failed it is over it holds no page back: that flush's error says the reply was refused for good and that the next flush drops the box anyway, and the next flush does, which clears the page's `last_error` (amended 2026-10-11 by Reopen and withdrawal on the page).
- **Failed replies stay visible.** Every outbox reply that failed for good (a refusal) is listed by `trk pages --errors` with its outbox id, page, comment, ask, body and reason, and `pages --json` carries them as `failed_replies`, whether or not the page has dropped the box since. The reason lives in the `meta` key `reply_failures` (publisher bookkeeping). A person settles each one with `trk pages reply resend <id>` (back to `pending`; the next pull sends it as it stands, even when an earlier attempt left the same body on the thread) or `trk pages reply dismiss <id> --reason "<one line>"` (it leaves the list; the row stays `failed`). Either refuses an id that is not a listed failed reply (rule `no-failed-reply`) (amended 2026-10-11 by Reopen and withdrawal on the page).
- A comment's `status` plays no part in its disposition: `outdated` (a re-render dropped its handle) and `open` again (a re-render put it back) are not new comments, and each root comment is disposed of once by id; the activity log's `status` events for them are never read, like its other `status` events. A comment no pass read before it went `outdated` is still disposed of as usual (amended 2026-10-11 by Reopen and withdrawal on the page).

**On the page afterwards**
- The ask leaves every waiting box and shows as a closed row with its outcome in its parent's table, through the normal affected-pages path.
- A reopened ask (see [the amendments](#amendments-to-asks-and-owners)) comes back into every waiting box under the same `ask-<id>`.

### Token expiry

- Every flush and pull checks `publish_expires`, the expiry the adapter reported at setup (none, no check). At 14 days or less before expiry, or on any exit 2, the publisher opens one ask, unless `publish_renewal_ask` names one still open (amended 2026-10-24 by Page adapters):
  - it sits under `publish_home`, is opened by actor `publisher` and is addressed `--to publisher`;
  - its title is "Fix the page adapter", and its question names the expiry date and the setup command (amended 2026-10-24 by Page adapters; it was "Mint a new tracker token");
  - its id is stored in `publish_renewal_ask`.
- A 401 holds every page write until setup succeeds with a new token. Pages stay dirty, and the store is unaffected.
  - The hold is `publish_token_rejected` in `meta` (publisher bookkeeping, not journaled). While it is set, a flush makes no call at all, even if the old token would work again: it still checks that the ask is open, and reports `held: true`.
  - Any setup whose token check passes lifts the hold in its own transaction, whether or not it answers the ask: the check has just shown the token works (amended 2026-10-11 by Token expiry and renewal ask).
  - `pages republish` is a page write too: while the hold is set it is refused (`republish-failed`) before any call, and a 401 to it sets the hold and opens the ask as a flush's does (amended 2026-10-11 by Token expiry and renewal ask, for Edit conflicts' ledger-9).
  - An exit 2 sets the hold only when no setup has run since the run's client was made (each setup stores a new id). A run that began before a setup and meets its refusal after it sets no hold and opens no ask, so the old setup's refusal never undoes the new one (amended 2026-10-24 by Page adapters; it compared the token file's contents).
- A successful setup with a new token answers the open renewal ask in the same transaction (`ask.answered`, `via: "cli"`, answer "The page adapter checks out; its credentials expire <date>." or "…; it gives no expiry.", outcome "Adapter fixed"; amended 2026-10-24 by Page adapters). The actor is whoever ran setup, under Asks and owners' answer permission. If that actor may not answer (an agent other than the holder), setup still succeeds and leaves the ask open, and says so. The next tick catches every page up. To make that true of a tick (which, unlike `flush --now`, honours the failure pause), the setup that lifts the hold also clears `last_error` on every page the 401 failed (amended 2026-10-11 by Background flusher and timer).
  - "A new token" is a setup whose check passed with an adapter-reported expiry more than 14 days away, or none. The secret is never stored, so setup can't compare it with the old one. A setup that leaves the expiry inside the window leaves the ask open and says so (amended 2026-10-11 by Token expiry and renewal ask).
  - `setup --json` carries `renewal_ask`: null when none is open, else `{id, answered}`, plus `reason` (`expires-soon` or `ask-permission`) when it stays open.
- The ask's opening never stops the run that found it due. A refusal (the home closed since setup, say) is reported as `renewal_ask_error` and tried again next run. An ask that was withdrawn or closed some other way is no longer open, so the next run in the window opens a new one. `flush --json` carries `held`, `renewal_ask` (the open ask's id, or null) and, on a refusal, `renewal_ask_error` (amended 2026-10-11 by Token expiry and renewal ask).

### Reads and verbs

| Verb | Does |
|---|---|
| `pages setup --adapter --home [--team]` | configure publishing, check the adapter, answer an open renewal ask |
| `pages [--dirty] [--errors] [--json]` | list pages: item, kind, title, slug, url, version, published_at, dirty and due_at, last_error; `--errors` also lists the replies that failed for good, and `--json` carries them as `failed_replies` (amended 2026-10-11 by Reopen and withdrawal on the page) |
| `pages render <id> [--out F]` | render one page locally, without network (`-` for stdout); `<id>` may be `needs-you:<actor>` |
| `pages serve [--port N]` | serve every page read-only on `127.0.0.1:<port>`, rendered per request (added 2026-10-27) |
| `pages flush [--wait] [--now] [--item ID]…` | publish due pages |
| `pages pull` | run the comment pull and the outbox |
| `pages tidy <id> [--dry-run]` | close the threads of already-closed asks on one page; `<id>` may be `needs-you:<actor>` (added 2026-10-19 by tr-hr642) |
| `pages tick` | pull, then flush (the timer's command) |
| `pages timer install\|remove\|status` | the systemd user timer |
| `pages authors [--json]` | author ids seen on comments and their linked actors |
| `pages reply resend\|dismiss <reply-id> [--reason]` | settle a reply that failed for good: send it again, or dismiss it with a reason (added 2026-10-11 by Reopen and withdrawal on the page) |
| `pages republish <id>` | send the whole body on the current version, ignoring the stored hashes (repair); it is sent on the stored version and lands on `currentVersion` through a conflict, so it reads nothing (amended 2026-10-11 by Edit conflicts and republish) |
| `actor link <actor> --service-user <id>` | link an actor to a page commenter's author id |

- JSON outputs carry `"schema":1`. Exit codes and the error JSON are Core's.
- A network failure in `flush`, `pull` or `tick` exits 0, with the failure recorded on the pages. These verbs run unattended, and `pages --errors` is how a person sees what went wrong. `setup` exits 1 on a failed adapter check.
- New rule names: `publishing-not-set-up`, `token-file-mode`, `home-closed`, and `no-page` (`pages render` on an ask; added 2026-10-11 by Render a page locally), `token-check-failed` (setup's live check failed) and `not-built` (`flush`, `pull` or `tick` after setup, until their tickets land; both added 2026-10-11 by Page backends, fake service and setup). Since First publish, `flush` runs; since Background flusher and timer, `flush --wait`, `tick` and `timer` run; since Comments answer asks, `pull` runs too, and no verb is `not-built` any more. Background flusher and timer added `systemctl` (a `systemctl --user` step failed), `no-trk` (no trk executable for the unit to run) and `test-units` (the default systemd user directory under `$TRK_TEST=1`, as `test-store` refuses the default store). `republish-failed`: `pages republish` is run by a person, so like `setup` it exits 1 when its send fails, after recording `last_error` on the page; an item with no published page is refused with `no-page` (both added 2026-10-11 by Edit conflicts and republish). Page backends (2026-10-24) added `no-backend` (setup or a verb names a backend that isn't installed), `backend-check-failed` (the check of a backend without a token failed; one with a token still fails `token-check-failed`) and `no-comments` (`pages tidy` on a backend without comments). Page adapters (2026-10-24) replaced `no-backend`, `backend-check-failed`, `token-check-failed` and `token-file-mode` with `adapter-check-failed` (setup's `describe` or `check` failed, or the adapter could not run). [Browsing them locally](#browsing-them-locally) (2026-10-27) added `port-in-use` (`pages serve` cannot bind the port it was given).

### Browsing them locally

Added 2026-10-27. Michael: "can we ship trk with simple web server that uses a lcoalhost port to browse visually?" — "like publishing but local".

`trk pages serve [--port N]` (default port 8795) serves the pages this store would publish, from the store as it stands at the moment of each request. It is the publisher's templates without the publisher.

- **It renders on request, it does not publish.** A GET renders the page then and there (`render.render`, `top.render_page`, `needs_you.render_page`), so a served page cannot be stale, and there is no flush, no `pages` row, no cursor and no page bookkeeping behind it. Nothing is diffed or patched: the whole page is rendered every time, which costs nothing locally and is what the diffing machinery exists to avoid over a network.
- **It writes nothing.** Each request opens the store the way a read verb opens it (`store.open_store(sweep=False)`, so an older schema still migrates once, as `trk show` would) and then sets `PRAGMA query_only` on the connection, so SQLite refuses any write on it whatever a page asks for. Lapsed leases are not swept, so a lapsed lease reads as live here exactly as it does on a published page until `trk sweep` ends it.
- **It is independent of publishing, and does not compete with it.** A store publishes through one adapter at a time, so pointing the folder adapter at a directory takes the place of a real adapter; this takes the place of nothing. It runs on a store where `pages setup` has never run, and on a publishing store it changes nothing about what is published.
- **Links come from the server, not from the `pages` table.** `serve.urls` builds `{page key: URL}` in the shape `render.published_urls` returns — `/` for the top-level page, `/<item id>` for every page-bearing item (closed ones too), `/needs-you/<actor>` for each person with a page — and the render uses it, so a served page never carries an adapter's URL and a store that has published nothing still has working links.
- **The origin a page links from** is the `Host` the browser asked for when that names a loopback address (`localhost`, `127.0.0.1`, `::1`), so a tunnel forwarded on another port links itself correctly; any other `Host` is never echoed into a page and the links fall back to the server's own address.
- **It binds a loopback address only** (`127.0.0.1`), so it opens no port on the machine's network, and it runs in the foreground: Ctrl-C stops it, nothing installs it as a service, and it leaves nothing behind. A page on another box is read over an ssh tunnel: `ssh -N -L 8795:127.0.0.1:8795 <host>`.
- **Paths it does not answer** get a plain error page carrying the store's own message and a link back to the top-level page: 404 for an unknown item or actor and for an ask (rule `no-page`: an ask is answered on its item's page), 503 when there is no store yet. A port already in use is refused with rule `port-in-use`, not a traceback.

### Amendments to Core

- **Publisher bookkeeping lives outside the journal.** Core's write path journals every state change. `pages`, `page_handles`, `page_comments`, `page_replies`, the closing markers (`meta.page_closings`) and the publisher cursors are a cache of what was sent to the page backend, not item state, and are exempt. Each is written in its own short `BEGIN IMMEDIATE`, and `feed.head` changes only when rows are journaled.
- **A second lock file**: `<store>/pages.lock` serializes publisher network work only. Item writes still use the SQLite write lock alone.
- **Item JSON** (`show`, `tree --json`, `ready --json`, `claim --json`, `waiting --json`) gains `page: {url, version, published_at, dirty}`, or null before the first publish and for asks.
- **Status on ask rows**: `ask.reopened` carries `status_old`/`status_new` `closed`/`open`. That adds it to the list of rows that carry status in Core's change feed rules (as Asks and owners did for its ask rows).
- **Spawn check**: every write that commits rows now runs the spawn check (see [Flusher](#flusher)). It costs one pid-file read and is skipped when publishing isn't set up.
- `contract/changes.schema.json` adds `page.created` (item set, data slug and url), `ask.reopened`, `data.via`/`slug`/`comment_id` on `ask.answered`, and `service_user` as a possible `actor.updated` field. None of this bumps `schema`.

### Amendments to Asks and owners

Artifact comments as answers to asks (Michael, 2026-10-11) changed three things the accepted Asks and owners spec says. This spec carries them, and that spec points here.

- **Reopening an answered ask** replaces `ask-final` for answered asks. `trk reopen <ask> --reason "<one line>"` is allowed only when the ask is closed `done` (answered) and its parent is open. The asker, the parent's effective owner or any human may run it; anyone else gets rule `ask-permission`. A withdrawn ask still refuses with rule `ask-final`.
  - The reopen clears resolution and outcome and keeps the `answer` section. It journals **`ask.reopened`** instead of `item.reopened`, with the same fields as `ask.opened` (the addressee resolved now) plus `data.reason` and `data.reopened_by` (ADR 0002 point 5, as amended).
  - The parent leaves the frontier again (`open-ask`).
  - If the latest `ask.answered` has `via: "page"`, the same transaction queues an outbox reply on that comment, without resolving it: "Reopened by <name>: <reason>". Otherwise the reason lives only in the journal row. <name> is the reopener's display name. The thread is the person's: when they have resolved it, the backend does not reopen it on a reply, so the reply stays on it as a record, and the reopened ask is answered by a fresh root comment on the same `ask-<id>` box (amended 2026-10-11 by Reopen and withdrawal on the page).
  - The next answer overwrites `answer`. Earlier answers stay in the journal's excerpts and in the page threads.
- **`data.via`** on `ask.answered`: `"cli"`, or `"page"` with `slug` and `comment_id` (and `confirmed_by`, who ran `trk answer --from-comment`).
- **`comment.routed`** and **`comment.acknowledged`** (added 2026-10-20 by tr-yd424): a person's page comment outside an ask box, routed to the item's owner. `comment.routed`: item the page's item (null on a Needs-you page), actor the commenter, `owner` the effective owner, `addressee` the actor it is routed to (the owner, or the Needs-you page's person); data `slug`, `handle` (null for the page itself), `comment_id`, `excerpt` (plain text, at most 240 characters) and, on a Needs-you page only, `page` (`needs-you:<actor>`). `comment.acknowledged`: `trk comment ack`; actor who acknowledged it; data `comment_id` and, when given, `ask`. Neither marks a page. `trk show <item> --json` lists the item's routed comments with no acknowledgement under `comments` (`comment`, `author`, `handle`, `at`, `body`); `trk comment reply <comment> --body @file|-` queues a reply in the thread through the outbox, leaving it open; `trk comment ack <comment> [--ask <id>]` takes it off the list. Both refuse a comment that was not routed (`no-routed-comment`), and a reply or ack refuses one already acknowledged (`comment-acknowledged`). Routing it to the owner's session or pane is the plugin's to add.
- **`ask.replied`** (amended 2026-10-13 by tr-3vgrb, decided in tr-v69zt): a person's page comment on an open ask, recorded with the ask left open (see [Comments as answers](#comments-as-answers)); `trk answer --from-comment` turns one into the answer.
- **Automatic registration** of a page commenter as a `human` actor, and `actor link` (above). The actors JSON stays the same: `service_user_id` is internal.

## Testing decisions

- Same harness as Core and Asks and owners: pytest through `main(argv)`, a temp `$TRK_HOME`, `$TRK_NOW` under `$TRK_TEST=1`, and every journal row validated against `contract/changes.schema.json`.
- **A fake page backend**, in process behind the client interface, with the semantics the publisher relies on:
  - slugs, versions and tags;
  - `base_version` conflicts as JSON tool errors with `changedSections`;
  - all-or-nothing patches by `data-comment-id` (duplicate or unknown handles refused), the 50-edit and 2 MiB limits, team at publish;
  - comments with handles, `createdAt`, author id and kind (switchable off, to model the time from a backend without author ids), and replies, with `resolve` succeeding only on the owner's pages;
  - with `activity` on, `offers_activity` true and its log of `comment`, `reply` and `status` events over the owner's artifacts, an opaque cursor (none for an empty log read from the start), `hasMore` paging and `invalid_cursor` (added 2026-10-11 by Cross-page comment feed).

  Injectable faults: a 502, a 502 after the origin accepted a publish (the ambiguous case), a 401, `tool_not_allowed` and a conflict. Every flush, pull and conflict test runs against the fake.
- **Golden renders**: one fixture store built through the CLI, rendered for each page kind and compared byte for byte with checked-in HTML. The same tests check that:
  - rendering twice gives identical bytes;
  - no `src=` attribute and no `<link>` point outside the page;
  - every `href` is http(s), mailto or an in-page anchor;
  - every `data-comment-id` is unique on its page;
  - each child appears in exactly one table;
  - an ask's `ask-<id>` is identical on every ancestor page.
- **Handle stability**: over a scripted series of writes (close a ticket, open and answer an ask, rename, move, reopen), every handle in the old render either appears in the new render or names a thing that left the page.
- **Diff minimality**: closing one ticket under a project with ten tickets sends exactly that ticket's `child-<id>`, `tiles`/`stage-funnel` and any changed `waiting` handles to the project page, and nothing to sibling ticket pages. A `RENDER_VERSION` bump republishes every page once.
- **Debounce**: twenty writes to one ticket within 60 s of `$TRK_NOW` produce one write to its page. A page written every 30 s for 6 minutes is still written at the 5-minute cap. A `claim renew` marks nothing.
- **Comments**: a table test over the dispositions. Cases:
  - a reply, two replies in the order written, and the asker confirming one as the answer;
  - already-answered after a CLI answer;
  - a withdrawn ask;
  - a `waiting` comment with zero, one and two asks open;
  - a non-ask handle, which is routed to the owner (also on a Needs-you page), with show listing it until it is acknowledged, a reply in its thread and an agent's comment ignored;
  - an unknown author, who is registered;
  - a linked author;
  - the time from a backend without author ids;
  - a crash injected between disposition and reply, then recovered;
  - two pulls racing, which send exactly one reply;
  - pull-before-drop.

  Every case runs twice, listing page by page and reading the activity log, except the time from a backend without author ids, which no backend with the activity log has (amended 2026-10-11 by Cross-page comment feed).
- **The local server** (`tests/test_serve.py`, added 2026-10-27): a server bound to a free loopback port on a daemon thread, asked over HTTP. It serves the top-level page at `/`, an item at `/<id>` (trailing slash too) whose first crumb is this server's `/`, a person at `/needs-you/<actor>`, and a closed item's page; it serves a store that has never published, with its links working; a published store's pages never carry the adapter's host; an ask, an unknown item, an unknown actor and an unknown path are 404 pages that link home; `read_only()`'s connection refuses an INSERT; the base a page links from follows a loopback `Host` and falls back for any other; a port in use is refused `port-in-use`; and the verb's default port is `serve.DEFAULT_PORT`.
- **Live smoke** (acceptance only, not in CI): against the dev page backend with a test token and a scratch team.

## Acceptance criteria

End-to-end checks against the fake page backend, unless stated otherwise. Each check starts from a fresh store: `trk init --me michael`, `trk actor add kim --kind human` and `trk roles set product kim` (`worker` was registered by hand here until `trk init` started registering it, 2026-10-27), then program P, project J (draft) under P, decision D (grilling) under J and spec-bound ticket T under J. If Stage gates has landed, P is created with one destination criterion and T with `what-to-build` and one criterion, so that their creation gates hold.

1. Before setup, `trk pages flush` exits 1 with rule `publishing-not-set-up`, and writes spawn no flusher. `trk pages setup … --home P` with a token file of mode 0644 exits 1 with rule `token-file-mode`. A valid setup journals `config.changed` rows, and the token secret appears nowhere in `store.db` or in any output.
2. One `trk pages flush --now` publishes four pages top-down (P, J, D, T), each with `team: tracker` and tag `trk:<id>`, and journals four `page.created` rows. In the same run, P and J are patched so that their rows link to their children's pages. Every breadcrumb and row link resolves to another page's url, and `show T --json` carries `page.url`. A second `flush --now` writes nothing.
3. `trk pages render J --out -` is byte-identical across two runs, holds the Board elements in order, has no handle twice, and contains no external resource.
4. Session S1 claims D. As `worker`, `trk ask D --to product "Q?" --question -`. After a flush, the same `ask-<id>` box (role `product`, addressee `kim`) appears in the waiting boxes of D, J and P, and as a `child-<id>` row in D's `children`. T's page is not written.
5. Twenty `trk section set T what-to-build` writes within 60 s of `$TRK_NOW` produce one `update` on T's page, carrying only the changed handles. With `$TRK_NOW` past half of S1's lease, `trk claim renew --session S1` journals `claim.renewed` and marks no page.
6. `trk actor link kim --service-user u-kim`. Two root comments on `ask-<id>` before a tick: first by `u-kim` on P's page, then by a new author `u-new` (display "Lee Park") on J's page. `trk pages tick` then (amended 2026-10-13 by tr-3vgrb, decided in tr-v69zt):
   - journals two `ask.replied` rows (actors `kim` and `lee-park`), leaves the ask open with `open-ask:<ask>` in `show D`'s `why_not`, and replies "Reply received. worker will confirm it as the answer or follow up here." to both comments, leaving them open;
   - registers `lee-park` (human, by `publisher`);
   - after `trk answer <ask> --from-comment <Kim's comment> --outcome "…" --as worker`, the ask is closed `done`; its `ask.answered` row has actor `kim`, `proxy: false`, `via: "page"`, P's slug, the comment id and `confirmed_by: worker`, and the next tick replies "Recorded as the answer…" to Kim's comment, leaving it open;
   - removes the box from D, J and P on the next flush, and `show D` no longer has `open-ask:<ask>` in `why_not`.
7. As `worker`, open ask A2 under J and ask A3 under T, both to `product`. A root comment on J's `waiting` box gets "Which ask?…" and stays open, because J's box holds A2 and A3. After `trk withdraw A3 --reason "not needed" --as worker`, a comment on T's `waiting` box gets "Nothing is waiting here." and stays open.
8. `trk reopen <the ask from AC 6> --reason "need units"` as `worker` (the asker) journals `ask.reopened` with `data.reason`, with status `closed` → `open`. `show D` has `open-ask:<ask>` in `why_not` again, the box returns to D, J and P under the same handle, and the next tick posts "Reopened by worker: need units" on Kim's thread. `trk reopen A3` (withdrawn) exits 1 with rule `ask-final`.
9. A root comment by `u-kim` on the reopened ask's box on D's page; then `trk close D --resolution wont-do --outcome "…"` before any tick. (Amended 2026-10-22, tr-jwbg9: the close is now refused with `open-asks`; `trk withdraw` the ask first, then close.) The next tick replies "Withdrawn: parent closed (wont-do)" to the comment, left open, **before** any page drops that `ask-<id>` (pull-before-drop).
10. Fault injection:
    - A stale `base_version` on J's page: the publisher fetches the changed handles, re-sends on `currentVersion`, and the page ends with the expected render.
    - A 502 after the origin accepted a new page's publish: the retry finds the page by its `trk:<id>` tag, and no second page exists.
    - A 401: one "Mint a new tracker token" ask opens under P, addressed to role `publisher`, and all page writes are held. A second 401 opens no second ask. `trk pages setup` with a new token, as `michael`, answers that ask, and the next tick publishes every dirty page.
11. Live, against the dev page backend with a test token and a scratch team (with author ids): P, J and T publish shared on the team. A comment typed in the web UI on an `ask-<id>` box is recorded as the answer within two timer periods, and the reply shows on the open thread.
12. Dogfood: the Tracker v1 program's own pages are published from the live store, and Michael confirms that the program page replaces the hand-kept board.

## Out of scope

- **Page comments outside an ask's box** (sections, rows, stage table): ignored in v1, apart from the pointer at the ask boxes (map fog: page comments as feedback).
- **Archiving, pruning or unpublishing** the pages of closed items (map fog: lifecycle of closed items). A token can't delete artifacts anyway.
- **Images and assets** on pages, and **audience views** beyond the waiting box (map fog).
- **A local live UI or a hosted web app** (map: out of scope).
- **Stage, criterion, evidence and ledger data**: project 3 and Decision ledger own them. This spec only renders them when they are present.
- **Handoff storage and verbs**: a Core follow-up. This spec only reserves the header line.
- **The two page backend changes themselves**: built in the page backend from this spec's handoff.
- Pages for asks, multiple teams or per-page visibility, and renaming the CLI.

## Acceptance

Accepted by the authoring agent (wC7:pX) on 2026-10-11, after an independent review against Page templates, Artifact comments as answers to asks, Publisher auth, Stage gates, the live wire, the Core and Asks and owners specs, ADR 0002 and CONTEXT.md. The review raised 25 findings, and all were applied. The main ones:

- First publish now runs until the cross-links resolve.
- One lock covers all publisher network work and is never held while sleeping. Dispositions and replies go through an idempotent outbox.
- New pages are recovered by tag after an ambiguous 502, so there are never duplicates.
- `actor link` maps known people to their author ids.
- The flusher exits when only failed pages remain.
- A root-shell hash and `RENDER_VERSION` catch layout and template changes.
- Each child appears in exactly one rollup table.
- `owner.changed` and `page.created` mark descendants.
- The pull's behaviour without author ids is stated.
- An Amendments to Core section was added.
- Bodies go inline, and conflicts are parsed as tool errors.
- Acceptance criteria 1 and 5–9 were re-staged so that their setups produce what they assert.

Agents accept specs under this program's Notes.
