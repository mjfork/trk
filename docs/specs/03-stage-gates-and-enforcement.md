# Spec: Stage gates + evidence + enforcement

Project 3 of the Tracker v1 program (`tr-pbrvj`). Spec state: **accepted** (2026-10-11; see [Acceptance](#acceptance)).

Vocabulary is [CONTEXT.md](../../CONTEXT.md). This spec extends [Spec: Core store + CLI](01-core-store-and-cli.md) and [Spec: Asks and owners](02-asks-and-owners.md), and changes them only where it says so ([Amendments to Core](#amendments-to-core), [Amendments to Asks and owners](#amendments-to-asks-and-owners)). [Spec: Page publisher](04-page-publisher.md) already renders the data added here when it is present; its one change is [Amendments to Page publisher](#amendments-to-page-publisher).

**Builds on Asks and owners.** The `engineering` role, `trk answer`, `config set`, and migrations that receive the write `Transaction` all come from Asks and owners, which is still being built. This project's tickets that touch asks, settings or the migration wait on the matching Asks and owners tickets (wired by the tickets decision).

Settled inputs, all closed decisions in the `trk` store under `tr-yk91p`:

- **Stage gates** (`tr-00pd4`): its Resolution is this spec's stages, gates, criterion rows, evidence rows, `advance` / `reopen-stage` / skip, the preconditions on `spec accept` and `close`, and `ready`'s stage and missing list.
- **Where the build discipline is enforced** (`tr-zmmq3`): enforcement is the tracker's refusals only, with no escape hatch and no bypass record. Its one change to built code, `claim --force` no longer overriding `not-ready`, is Core ticket `tr-aa4ge` (being built now). This spec relies on it and does not specify it again.
- **Decision ledger in the tracker** (`tr-ngyp0`): its Resolution, Spec defaults and Consequences are this spec's [Ledger](#ledger), [Blast radius](#blast-radius) and [Merged gate additions](#merged-gate-additions). It **amends Stage gates' "no review gate"**: independent review gates Merged for tickets in the RED and YELLOW lanes.
- [ADR 0001](../adr/0001-own-store-not-beads.md) (stage gates require evidence: the reason for our own store) and [ADR 0002](../adr/0002-herdr-contract.md) (`stage.changed` reserved; consumers ignore unknown kinds).

## Problem

The program's discipline, "no build without a ticket, no ticket without a spec", and the stage pipeline every ticket is meant to go through (Framed → Spec'd → Built → Merged → Verified) exist only as prose. The store knows whether a ticket is open or closed, but not how far it got or why anyone believes it works. A ticket can be closed `done` with no PR, no passing test and no check against its acceptance criteria; a project can be accepted with half its spec empty; a program can close `done` without anyone showing that its destination holds. Acceptance criteria are free text in a section, so nothing can say which criterion is proved and which is not, and an edit to a criterion silently leaves old proof standing.

Review is the same. An agent building a ticket makes forks the diff never shows (why this schema, why that default), and they are lost at the next compaction. Nobody independent judges them before merge, a builder can "review" its own work, and a test made to pass by regenerating its golden file looks the same as a real fix. A risky file and a typo fix go through the same (missing) gate.

## Solution

The tracker stores each ticket's **stage** and moves it only through **gates** it checks from data. Every check reads append-only **evidence** rows and versioned **acceptance criterion** rows, and refuses with a machine-readable **missing list** when the gate does not hold. Nothing is written on a refusal, a refusal never opens an ask, and there is no override.

- A ticket is **born Framed** (creation is refused unless its written sections and at least one criterion are given) and auto-advances through every gate checkable from data alone, so a spec-bound ticket under an accepted spec is born Spec'd.
- `advance` moves one stage forward: Spec'd (spec accepted, or judged self-contained), Built (a PR at a head sha and a passing test run at that sha), Merged (a merge sha, a blast-radius rating, and for RED/YELLOW tickets an independent review of the ledger), Verified (every criterion passing at a sha containing the merge). Reaching Verified closes the ticket `done`; no other path closes a ticket `done`. Merged alone may be skipped, with a reason, for tickets that change no repo.
- `reopen-stage` moves a ticket back with a reason; gates above that stage then count only evidence recorded after the move.
- Acceptance criteria (tickets, projects) and destination statements (programs) become rows with stable `ac-<n>` handles. The `acceptance-criteria` and `destination` sections are generated from them, with each criterion's evidence status.
- `spec accept` needs a complete spec, at least one criterion and a `judgment` row naming the acceptor. A project or program closes `done` only with passing evidence for every criterion; a decision only with its `resolution` written.
- While building, the builder logs its correctness-relevant forks as **ledger** rows. For a ticket whose **blast radius** reaches YELLOW or RED, an auditor session completes the ledger, an independent reviewer session puts a **verdict** on every row, and every RISKY or WRONG row is settled without the builder before Merged; RED also needs a person's GitHub PR approval. A confirmed **reward-hacking flag** is never overruled. The tracker enforces independence by session: a session that built the ticket is refused when it tries to judge it.
- `ready` stays stage-blind, but every item row now carries its stage and its next gate's missing list.

## User stories

1. As a worker agent, I create a spec-bound ticket with its `what-to-build` section and its acceptance criteria in one `trk new`, and it is born Spec'd because its project's spec is accepted.
2. As a worker agent, I am refused when I create a ticket with no acceptance criterion or an empty `what-to-build`, and the refusal says exactly which is missing.
3. As a worker agent, I create a self-contained bug report, judge it self-contained with `trk advance <id> --judgment "repro and expected behaviour are complete"`, and claim it: two commands before the claim, because urgent work has a short legal path.
4. As a worker agent, after opening my PR I record `pr` evidence with the head sha and a passing `test-run` at that sha, then `trk advance` to Built.
5. As a worker agent, I run `trk advance` too early and get a non-zero exit, nothing written, and JSON listing each missing piece per gate and per criterion, which I can act on without reading the spec.
6. As a worker agent, I see `trk show <ticket>` and `trk ready` report my ticket's stage and what its next gate still lacks, from the same check `advance` runs.
7. As a worker agent, I record evidence of any fixed kind, with an optional criterion, sha, verdict and a required one-line note, and never edit or delete a row afterwards.
8. As a worker agent who recorded the wrong evidence, I retract it with a `retract` row that names it and says why, and the gate stops counting it.
9. As a worker agent whose test flaked, I record a new passing run, and the latest counting row for that criterion decides.
10. As a worker agent, I edit a criterion's text, and its version goes up and its earlier evidence stops counting, so stale proof never satisfies a changed promise.
11. As a worker agent, I retire a criterion that no longer applies; its handle stays, struck through, and the gate stops asking for it.
12. As a worker agent, I add a criterion after Built, and Verified then also needs passing evidence for it.
13. As a worker agent whose ticket failed verification, I `reopen-stage` it to Built with a reason, and the Merged and Verified gates then ignore everything recorded before the move, while it all stays visible as history.
14. As a worker agent on a ticket that changes no repo, I skip Merged with a one-line reason and go straight on to Verified, checked against the built result.
15. As a worker agent, I reach Verified with an outcome, and the ticket closes `done` in the same step.
16. As a worker agent, I am refused when I `trk close <ticket> --resolution done` directly, and told to advance to Verified; closing `wont-do`, `superseded`, `out-of-scope` or `duplicate` still works at any stage.
17. As a verifier agent, I run `trk ready --stage merged` to find merged tickets waiting for verification.
18. As a builder agent, I log a fork as a ledger row (what I decided, the alternatives, why, what I would have asked) while I build, so it survives compaction and a reviewer can judge it.
19. As a builder agent, I am refused a ledger row with no alternative or no why, so a row always says what was weighed.
20. As a builder agent facing a fork that contradicts an ADR or the accepted spec, changes unspecified user-visible behaviour or is hard to reverse, I open an ask instead of logging a row, and my ticket pauses (the ask is Asks and owners' mechanism; the rule for when is the Workflow skills integration's builder direction).
21. As an auditor agent in a fresh session, I add the rows the builder missed (origin `auditor`), mark noise, mark a guess repeated from another ticket's ledger, and record an `audit` row at the head sha.
22. As a reviewer agent in a fresh session, I put SOUND, RISKY or WRONG on every ledger row, and CONFIRMED or REFUTED on every reward-hacking flag.
23. As a builder agent, I am refused when I try to write a verdict or an auditor row on my own ticket, from my session or under my named agent actor.
24. As a fixer, after fixing a WRONG row I settle it `fixed` at the fix sha, and the reviewer's later SOUND verdict at that sha or later clears it.
25. As a reviewer, I defer a RISKY row, and the tracker files a self-contained ticket for it at once, born Framed, related to the source ticket.
26. As Michael, I accept a RISKY row or overrule a WRONG row by answering an `engineering` ask under the ticket, and only then may the settlement be recorded.
27. As Michael, I know a confirmed reward-hacking flag can only ever be fixed, by me or anyone else.
28. As a worker agent, I run `trk blast-radius <ticket> --sha <head>`, and the tracker rates every changed file RED, YELLOW or GREEN from the repo's rules file and the ADRs' scopes, and records the ticket's lane.
29. As a worker agent on an all-GREEN ticket, I merge with no review at all.
30. As Michael, I am asked to read a RED ticket's PR, and my GitHub approval, recorded as `human-review` evidence at the head sha, answers the ask and satisfies the gate.
31. As the auditor, when I mark a guess that repeats one in another ticket's ledger, the tracker files (or links) a "Ticket template: add a field for <topic>" ticket under the workflow skills project, with no ask.
32. As an agent accepting a project's spec, I am refused while any expected spec section is empty, there is no criterion, or no `judgment` row for acceptance has been recorded since the last return to draft; the judgment's actor is recorded as the acceptor.
33. As an agent returning an accepted spec to draft with a reason, I see the project's Spec'd tickets fall back to Framed and leave the frontier, while tickets already Built keep their stage.
34. As an agent closing a project `done`, I am refused until every criterion has passing evidence: an end-to-end run, or a `judgment` citing the tickets that delivered it.
35. As an agent closing a program `done`, I am refused until every destination statement has passing evidence: a run, or a `judgment` citing the delivering projects.
36. As an agent closing a decision `done`, I am refused while its `resolution` section is empty.
37. As the herdr plugin and the page publisher, I read `stage.changed`, `criterion.*`, `evidence.added` and `ledger.*` rows from `changes`, and ignore the ones I don't need.
38. As Michael, I read a ticket page's stage pipeline, criterion list, evidence table and review ledger and know why each stage was reached, without asking an agent.
39. As any writer, I am refused when I `section set` a generated section (`acceptance-criteria`, `destination`) and pointed at the criterion verbs.
40. As Michael, after the upgrade the live store's tickets have stages and its written criteria have become rows, with the original text kept, and nothing in the journal was rewritten.

## Implementation decisions

### Modules and the gate function

- New modules, stdlib only, loaded lazily from the verbs that need them (the `claim renew` hot path imports none): `criteria`, `evidence`, `stages` (gates and stage moves), `ledger`, `blast` (rules file, ADR scopes), `gitrepo` (every git call: rev-parse, ancestry, diff).
- **One gate function.** `gate(store, item, gate_name) -> missing[]` is the only place a gate is evaluated. `advance`, `skip`, `spec accept`, `close`, `show`, `ready` and `criteria` all call it, so what `show` says is missing is, by construction, exactly what `advance` refuses on (the same pattern as Core's shared `ready`/`why_not`).
- Every new write is one `BEGIN IMMEDIATE` transaction with its journal rows, as in Core.

### Gates and their names

A **gate** is named by what it lets happen. These names appear on evidence rows, in missing lists and in JSON:

| Gate | Applies to | Lets happen |
|---|---|---|
| `framed` | tickets | creation |
| `specd`, `built`, `merged`, `verified` | tickets | entering that stage |
| `accept` | projects | `spec accept` |
| `close` | projects, programs, decisions | closing `done` |

Asks keep Asks and owners' rule (`answer` required by `trk answer`) and get no gate of their own.

### Stages (tickets only)

- `items.stage`: `framed | specd | built | merged | verified`, set on spec-bound and self-contained tickets, null on every other item. Stored, never derived. `items.merged_skipped` (0/1) marks a ticket whose Merged gate was met by a `skip` row.
- Stage moves only through `new` (auto-advance), `advance`, `skip`, `reopen-stage`, `spec accept`/`spec draft` (see [Spec state](#spec-state)) and `reopen` of a Verified ticket. Each move writes one `stage.changed` row per stage crossed (data: `old`, `new`, `via` = `create | advance | skip | reopen-stage | spec-accept | spec-draft | reopen | migration`, `reason` when given). Auto-advance through two stages writes two rows, so every stage has a row that entered it.

| Enter | Gate holds when | Skippable |
|---|---|---|
| Framed | written sections non-empty (spec-bound: `what-to-build`; self-contained: `current-behaviour` and `desired-behaviour`) and ≥1 live criterion | no |
| Spec'd | spec-bound: nearest project ancestor's spec is `accepted` (from data); self-contained: a counting `judgment` row for gate `specd` | no |
| Built | a counting `pr` row with a sha (the **head sha**), and a counting passing `test-run` for gate `built` whose sha equals the head sha | no |
| Merged | the Built gate still holds at the current head sha; a counting `merge` row (the **merge sha**); a counting `blast-radius` row at the head sha; and for YELLOW and RED lanes, the [Merged gate additions](#merged-gate-additions) | yes, by `skip` |
| Verified | every live criterion's latest counting row for gate `verified` is a pass of kind `webtester-run`, `human-accept` or `test-run`, at a sha that contains the merge sha (or the head sha when Merged was skipped) | no |

- **The head sha** is the sha of the latest counting `pr` row; **the merge sha** is the sha of the latest counting `merge` row. A fix pushed during review is recorded as a new `pr` row at the new head plus a passing `test-run` at it: the ticket stays at Built (no backward move), and Merged re-checks Built at the new head. Rows pinned to the head (`blast-radius`, `human-review`, Built's `test-run`) must be at the current head. `audit` rows and ledger verdicts carry over when their sha is the current head or an ancestor of it, so a fix needs no new audit, only the re-check verdict on the fixed row, a new `blast-radius` row at the new head, and for RED a fresh `human-review` at the new head.
- **The repo** of a ticket is the first of its repos (in the order added) that is a local git checkout, else the same for its nearest ancestor with repos. All git calls go through the `gitrepo` module.
- **Sha comparison.** Shas are stored as given (lowercase, 7–40 hex). When the repo is a checkout, every comparison resolves both sides with `git rev-parse --verify <sha>^{commit}`; otherwise two shas are equal when one is a prefix of the other and the shorter has ≥7 chars.
- **"Contains"** is git ancestry: `git -C <repo> merge-base --is-ancestor <ancestor> <sha>`. Exit 0 counts; exit 1 is `wrong-sha`; exit 128 (unknown commit) or no checkout at all is `sha-unverifiable`. Equal shas count without git. Ancestry is used by Verified (merge sha), by carry-over of `audit` rows and verdicts, and by the `fixed` re-check, so `show`, `ready`, `criteria` and `evidence` of a ticket at Built or later may call git. Results are cached per (repo, ancestor, descendant) for the life of one CLI invocation.
- **No-repo-change tickets.** Built is unskippable, so such a ticket records a `pr` row with `--ref none` and the sha of the repo's commit the work was tested against, and a passing `test-run` (or equivalent check) at that sha. Only such a ticket may skip Merged (see `skip` below).
- **Creation**: `new` for a ticket checks `framed` and refuses (rule `gate-unmet`, with the missing list) when it fails. It journals `item.created`, then one `criterion.added` per criterion, then the `stage.changed` rows. Then it auto-advances through `specd` when that gate holds from data alone (spec-bound tickets under an accepted project). Judgment gates, Built, Merged and Verified are always explicit.
- **`trk advance <ticket> [--judgment "<note>"] [--outcome "<one sentence>"]`** moves exactly one stage forward, checking that stage's gate. `--judgment` is allowed only for a self-contained ticket at Framed: it writes the `judgment` row for `specd` and advances, in one transaction. Advancing into Verified requires `--outcome` (usage error, exit 2, otherwise) and closes the ticket `done` with it in the same transaction, journaling `stage.changed`, then Asks and owners' `ask.withdrawn` rows for any open child asks, then Core's `item.closed`, edge and `claim.released` rows. Advancing a closed ticket is refused (Core's rule `closed`).
- **`trk skip <ticket> merged --reason "<one line>"`** is allowed only at Built, and only when the head `pr` row has ref `none`, no counting `merge` row exists and no counting `blast-radius` row is YELLOW or RED; otherwise rule `not-skippable` (a skip must never route around review). It writes a `skip` evidence row for gate `merged` (note = reason), sets `merged_skipped`, and moves to Merged with `via: skip`. Any other stage name is refused (rule `not-skippable`).
- **`trk reopen-stage <ticket> <stage> --reason "<one line>"`** moves an open ticket back to a named earlier stage (Framed through Merged). Moving to the current or a later stage is refused (rule `not-backward`). It clears `merged_skipped` when the target is Built or earlier. Evidence above the target stage stays as history but, for every gate above the target, only rows recorded after this move count. Ledger rows, verdicts and settlements are not cut off (the latest verdict already decides, and carry-over is by sha); a new `audit` row is needed because `audit` is evidence for `merged`. `ledger add` is allowed again once the ticket is back below Merged. `spec draft`'s move to Framed sets no cut-off.
- **Closing a ticket.** `close --resolution done` on a ticket is refused (rule `done-needs-verified`); advancing into Verified is the only way. The other resolutions close at any stage with no gate (outcome still required, as in Core). The stage is kept as it was.
- **Reopening a ticket closed at Verified** (Core `reopen`) requires `--to-stage <stage> --reason "<one line>"`, a stage before Verified. It journals `item.reopened` then `stage.changed` (`via: reopen`) and applies the same counting cut-off as `reopen-stage`. Reopening a ticket closed at any other stage leaves the stage alone and needs neither flag.
- A failing evidence row never moves a ticket. It already blocks the gates that weigh it, and the agent decides between a re-run and `reopen-stage`.

### Acceptance criteria

- Table `criteria`: item_id, n (handle `ac-<n>`, per item, starting at 1, never reused), text (one line, at most 500 chars), rank (fractional index, as Core's sibling rank), version (starts at 1), retired (0/1), retired_reason, created_at, updated_at. Every version's text is kept in table `criterion_versions` (item_id, n, version, text, actor, created_at), so evidence always names text that can still be read.
- Criteria belong to tickets and projects (acceptance criteria) and to programs (destination statements). Decisions and asks have none (rule `no-criteria`).
- **Verbs:**
  - `trk criterion add <item> "<text>" [--first|--last|--before ac-<n>|--after ac-<n>]` prints the handle. Allowed on any open item that takes criteria, at any stage; added past Built, it simply needs its own evidence before Verified.
  - `trk criterion edit <item> ac-<n> "<text>"` bumps the version. Evidence recorded against an earlier version stops counting.
  - `trk criterion retire <item> ac-<n> --reason "<one line>"` keeps the row, struck through, and drops it from every gate. Retiring the last live criterion is refused (rule `last-criterion`), since Framed needs one. A retired criterion is never un-retired; add a new one.
  - `trk criteria <item> [--json]` lists every criterion with its handle, text, version, retired flag and evidence status (see [Weighing](#weighing)).
  - All criterion writes are refused on closed items (Core's rule `closed`).
- **At creation**, `new` takes `--criterion "<text>"` (repeatable, in order). Tickets need ≥1 (the `framed` gate) and programs need ≥1 (destination statements, Stage gates). Projects may start with none; `accept` needs one.
- **Generated sections.** On tickets and projects, `acceptance-criteria` is generated from the rows; on programs, `destination` is. `section set` and `section rm` on them are refused (rule `generated-section`, pointing at `trk criterion`). `section get` and `show` return the generated text: one line per criterion in rank order, `- [x] ac-2 (v2) text — pass (test-run e7k2m9, 3f2a1c0)` or `- [ ] ac-3 text — no evidence`, with retired ones as `- ~~ac-4 text~~ (retired: reason)`. `show --json` adds a `criteria` array alongside.
- Feed: `criterion.added` (data: handle, text, version 1, rank), `criterion.edited` (handle, old_version, version, text), `criterion.retired` (handle, reason). Text in feed data is the plain-text excerpt rule of Asks and owners (240 chars).

### Evidence

- Table `evidence`, append-only (`BEFORE UPDATE` / `BEFORE DELETE` triggers raise, as on `journal`): id, item_id, gate (see [Gates](#gates-and-their-names)), criterion_n (nullable), criterion_version (set from the criterion's current version when a criterion is given), kind, ref, sha, verdict `pass | fail` (nullable), note (one line, required, at most 300 chars), detail (JSON, nullable, only for kinds that carry structure), actor, session, seq (the journal seq of its `evidence.added` row), created_at.
- **Ids** are 6 chars of Core's lowercase Crockford alphabet, drawn with `secrets`, unique in `evidence` (redrawn on a collision, as Core's item ids); non-sequential, so a later second writer cannot collide by counting. Page handles are `evidence-<id>` (Page publisher). Input matching is Core's (case-insensitive, `i l o u` read as `1 1 0 v`).
- **Kinds, fixed in v1** (adding one is a code change, so gates can trust them): `judgment`, `pr`, `test-run`, `webtester-run`, `human-accept`, `merge`, `review`, `skip`, `plan`, `doc`, `retract`, plus the Decision ledger's `blast-radius`, `human-review` and `audit`.

| Kind | Gates it may name | Needs | Verdict |
|---|---|---|---|
| `judgment` | `specd`, `accept`, `close` (with a criterion) | note; for `accept` the acceptor is the row's actor | `pass` required |
| `pr` | `built` | ref (PR URL, branch name, or `none` for a no-repo-change ticket), sha | none |
| `test-run` | `built`, `verified`, `close` | ref (run id, log path or URL), sha (optional for `close`) | required |
| `webtester-run` | `verified`, `close` | ref (test id), sha for `verified` | required |
| `human-accept` | `verified`, `close` | ref, sha for `verified`; criterion | required |
| `merge` | `merged` | sha (the merge sha), ref (PR URL) | none |
| `blast-radius` | `merged` | sha (the head sha), detail (required) | none |
| `audit` | `merged` | sha (the head sha) | none |
| `human-review` | `merged` | ref (the GitHub review URL, or `direct:<actor>@<sha>` for a person's direct review, see the tr-753br amendment), sha (the head sha) | `pass` = approved, `fail` = changes requested |
| `review` | any | ref | optional |
| `skip` | `merged` | note (the reason) | none |
| `plan`, `doc` | any | ref | none |
| `retract` | the retracted row's gate | ref = the evidence id it withdraws, note = why | none |

  A row that breaks this table is a usage error (exit 2) naming the field.
- **Verbs:**
  - `trk evidence add <item> --kind K --gate G [--criterion ac-<n>] [--ref R] [--sha S] [--verdict pass|fail] --note "<one line>"` prints the id. `--sha` is stored lowercase and must be 7 to 40 hex chars. Evidence may be added to an open item at any stage, and to a closed one only as `review` or `doc` (or by `evidence retract`). The tracker-managed kinds `skip`, `blast-radius`, `audit` and `retract` are refused here (usage error, exit 2, naming `trk skip`, `trk blast-radius`, `trk ledger audit` or `trk evidence retract`), so each is written only with its checks.
  - `trk evidence retract <item> <evidence-id> --note "<why>"` writes a `retract` row. Retracting a retraction, a `skip` row, or a row of another item, is refused (rule `bad-retract`). A retraction never moves a stage or a spec state: what it withdrew stops counting for every later gate check (so a retracted `merge` row leaves no merge sha and Verified then lists `merge`), while moves already made stay as history.
  - `trk evidence <item> [--gate G] [--json]` lists rows in seq order, each with `counts: bool` and, when false, why (`retracted`, `old-version`, `before-reopen`, `wrong-sha`, `sha-unverifiable`, or `not-weighed` for the kinds in no group). A `verified` row is `sha-unverifiable` while there is no merge sha to contain (amended 2026-10-11, tr-hajqn). A `judgment` for `accept` recorded before the project's latest return to draft is `before-draft` (amended 2026-10-11, tr-ke8fa).
  - `skip`, `advance --judgment`, `blast-radius`, `review record` and `ledger audit` write their rows through the same function.
- Feed: one `evidence.added` row per row (data: id, gate, kind, criterion, criterion_version, sha, verdict, ref, note excerpt). A retraction is an `evidence.added` row with kind `retract`.

### Weighing

Rows are weighed in **groups**, per gate:

| Gate | Groups (each decided separately) |
|---|---|
| `specd`, `accept` | `judgment` |
| `built` | `pr` (gives the head sha); `test-run` |
| `merged` | `merge`; `blast-radius`; `audit`; `human-review` |
| `verified` | per criterion: {`test-run`, `webtester-run`, `human-accept`} |
| `close` | per criterion: {`test-run`, `webtester-run`, `human-accept`, `judgment`} |

`review`, `plan` and `doc` are in no group and never count. Within a group (for one gate and, where per criterion, one criterion), a row **counts** when all hold:

1. it names that gate (and that criterion at its **current version**);
2. it was recorded after the ticket's latest backward stage move to a stage below this gate (`reopen-stage` or a Verified reopen; seq comparison with the `stage.changed` row); for `accept`, after the project's latest `spec.changed` to draft (amended 2026-10-11, tr-ke8fa);
3. its sha is valid for the gate (Built's `test-run`: equals the head sha; Verified: contains the merge sha, or the head sha when Merged was skipped; `blast-radius` and `human-review`: equal the head sha; `audit`: the head sha or an ancestor of it);
4. no `retract` row names it.

Among counting rows of a group, the **latest** (highest seq) decides: a re-run pass clears an earlier flaky fail, and a later fail blocks an earlier pass. A criterion's **evidence status** is `pass`, `fail` or `none` from its latest counting `verified` row (tickets) or `close` row (projects, programs).

### Missing lists and refusals

- An unmet gate refuses with exit 1, rule `gate-unmet`, nothing written. The error JSON is Core's plus `gate` and `missing`:

```json
{"error":{"code":1,"rule":"gate-unmet","item":"tr-k7m2q","message":"verified: 2 missing",
  "gate":"verified",
  "missing":[{"need":"criterion-pass","criterion":"ac-2","version":2,"latest":{"id":"e7k2m9","verdict":"fail"}},
             {"need":"criterion-pass","criterion":"ac-3","version":1,"latest":null}]}}
```

- `need` values: `section` (with `section`), `criterion` (none live), `criterion-pass` (with `criterion`, `version`, `latest`), `judgment`, `spec-accepted` (with `project`), `pr`, `test-run-pass`, `merge`, `blast-radius`, `audit`, `ledger-verdict`, `ledger-settled`, `reward-hack-fixed`, `human-review`, `sha-unverifiable`, `open-children` (with the child ids), `resolution`. Ledger needs carry `ledger: "ledger-<n>"`. The text form prints one line per entry.
- Never auto-opens an ask (the RED-read ask is the one exception, see [Blast radius](#blast-radius)). When only a person can supply a missing piece (a `human-accept`, an `engineering` settlement), the agent opens the ask itself.
- **No escape hatch** (Where the build discipline is enforced): no flag skips a gate, Merged's `skip` is a gate rule limited to no-repo-change tickets, and nothing journals a bypass because none exists.

### Spec state

Core's `spec accept` and `spec draft` gain preconditions and effects:

- **`spec accept <project>`** checks gate `accept`: every expected project section non-empty (`problem`, `solution`, `user-stories`, `implementation-decisions`, `testing-decisions`, `out-of-scope`; `acceptance-criteria` is generated, so the check is ≥1 live criterion instead), and a counting `judgment` row for gate `accept` recorded after the project's latest return to draft. The row's actor is the acceptor; any registered actor may accept, including the author. `spec.changed` data gains `acceptor` and `judgment` (the evidence id).
  - In the same transaction it auto-advances every open spec-bound ticket of the project (nearest project ancestor) at Framed to Spec'd, one `stage.changed` row each (`via: spec-accept`).
- **`spec draft <project> --reason "<one line>"`**: the reason is now required (usage error otherwise) and goes in `spec.changed` data. In the same transaction every open spec-bound ticket of the project at Spec'd moves back to Framed (`via: spec-draft`, no evidence cut-off). Tickets at Built or later keep their stage. Core's `spec-not-accepted` readiness rule already keeps every spec-bound ticket of a draft project off the frontier, unchanged; a re-acceptance needs a fresh `judgment` and returns them.
- Edits to an accepted spec's sections and criteria stay allowed and journaled (Stage gates: no frozen specs).

### Closing done

`close --resolution done` checks gate `close` for the cases below. Core's `open-children` refusal still runs first, unchanged (existing tests rely on it); `gate.missing` reports the same condition as need `open-children` for reads.

- **Projects**: every child closed, ≥1 live criterion (need `criterion`; a migrated project whose criteria lived only in a doc must add them), and, for every live project criterion, a latest counting `close` row that passes: a run (`test-run`, `webtester-run`, `human-accept`) or a `judgment` whose note cites the delivering tickets. The project's spec must also be accepted: a draft project is refused with need `spec-accepted` (with `project`, the project itself), listed after `open-children` and before the criterion needs, since the build discipline allows no build without an accepted spec (amended 2026-10-11, tr-afrvc). A draft project still closes `wont-do`, `superseded`, `out-of-scope` or `duplicate`.
- **Programs**: the same (≥1 live statement), for every live destination statement (a run, or a `judgment` citing the delivering projects).
- **Decisions**: the `resolution` section is non-empty.
- **Tickets**: refused (`done-needs-verified`), as above.

Other resolutions close as in Core with no gate.

### Ledger

From Decision ledger in the tracker, Spec defaults. A ledger is its own append-only tables, not evidence rows, because rows gather verdicts and settlements over time. Tickets only (rule `ledger-tickets-only`).

| Table | Columns |
|---|---|
| `ledger` | item_id, n (handle `ledger-<n>`, per ticket, never reused), kind `fork \| contradiction \| reward-hack`, decided, alternatives (JSON list, ≥1 required for `fork`), why, would_have_asked, conflicts_with (required for `contradiction`: an ADR path or `spec:<section>`), files (JSON list, optional), origin `builder \| auditor`, noise (0/1, auditor only), repeat_of (`<ticket>:<n>`, auditor only), topic (required with repeat_of), refers_to (an earlier n this row corrects), actor, session, sha, created_at |
| `ledger_verdicts` | item_id, n, verdict `sound \| risky \| wrong \| confirmed \| refuted`, note (required), sha, actor, session, seq, created_at |
| `ledger_settlements` | item_id, n, settlement `fixed \| deferred \| accepted \| overruled`, sha (for `fixed`), ask (for `accepted`/`overruled`, and `fixed` on a contradiction), ticket (for `deferred`), adr (when a contradiction's code wins and the ADR is amended or superseded), note, actor, session, seq, created_at |
| `ledger_promotions` | item_id, n, adr (path), actor, session, seq, created_at |

All four have the append-only triggers. Rows are never edited: a correction is a new row with `refers_to`. Both rows still need a verdict (the reviewer may judge the corrected one SOUND as superseded) and, when RISKY or WRONG, a settlement. Promotion to an ADR is a `ledger_promotions` row recording the path; only a human actor may promote, and the proposal is an `engineering` ask tied to the row.

**Verbs** (the working names from the decision):

- `trk ledger add <ticket> --kind fork|contradiction|reward-hack --decided "…" [--alternative "…"]… --why "…" [--would-have-asked "…"] [--conflicts-with …] [--file PATH]… [--sha S] [--origin builder|auditor] [--noise] [--repeat-of <ticket>:<n> --topic "…"] [--refers-to <n>]`. Prints `ledger-<n>`. `--origin` defaults to `builder`. Allowed while the ticket is open and before Merged.
- `trk ledger list <ticket> [--json]`: rows with latest verdict, settlement, promotion and linked asks.
- `trk ledger verdict <ticket> <n> sound|risky|wrong --note "…" [--sha S]`; for a `reward-hack` row the verdict is `confirmed|refuted`. Other combinations are usage errors.
- `trk ledger settle <ticket> <n> fixed --sha S [--ask <id>] | deferred | accepted --ask <id> [--adr PATH] | overruled --ask <id> [--note "…"]`. `--ask` is required for `accepted`, `overruled`, and `fixed` on a `contradiction` row.
- **Asks about a row.** `trk ask <ticket> --to engineering --ledger <n> "…"` (see [Amendments to Asks and owners](#amendments-to-asks-and-owners)) ties an ask to one ledger row, so the row links its ask while the ask is still open and the ask's box links back. `ledger list` and `show` carry each row's linked asks.
- `trk ledger audit <ticket> --note "…"` records the `audit` evidence row at the head sha.
- `trk ledger promote <ticket> <n> --adr PATH`.
- *Amended 2026-10-11 (tr-b0y7v, as built).* `ledger add` on a ticket at Merged or later is refused under rule `ledger-closed` (on a closed ticket, Core's `closed`). Every text field is one line of at most 500 chars. `ledger promote` from an agent is refused under rule `human-only`. A verdict without `--sha` is recorded at the head sha. `ledger audit` with no head sha is refused `gate-unmet` with need `pr`. `<n>` is read as `<n>` or `ledger-<n>`. A ticket's item JSON carries its rows as `ledger_rows` (the `ledger list` rows; empty on other items), since an ask's `ledger` is its tied row's handle. `ask --ledger` takes any role; only settlements require `engineering`. Verdicts, audits and settlements are refused on a closed ticket (`closed`); promotion is not. An auditor marks a builder row as noise with its own row, `--noise --refers-to <n>`. A claim counts as held until its release, its expiry row or its lease end, whichever is first.
- Feed: `ledger.added`, `ledger.verdict`, `ledger.settled`, `ledger.promoted` (data: n, kind, and the row's fields as excerpts). Consumers ignore unknown kinds; no `schema` bump.

**Builder and independence.** A session is a **builder session** of a ticket if it wrote a `builder` ledger row on it, recorded its `pr` or `test-run` evidence, or held a claim on it during any period when its stage was before Built (claim intervals against `stage.changed` rows). An actor is a **builder actor** if it acted as any builder session did (a `builder` row, `pr` or `test-run` evidence, a pre-Built claim) and is an agent other than the **shared agent**: store setting `shared-agent` (default `worker`), the actor many sessions write as, for whom only the session check applies. Then:

- `ledger verdict`, `ledger add --origin auditor` and `ledger audit` are refused (rule `not-independent`) from a builder session or builder actor, and require a session (`--session` / `$TRK_SESSION`; rule `session-required`), since independence is checked by session.
- `accepted`, `overruled` and a contradiction's `fixed` need `--ask` naming an ask that is a child of the ticket, tied to **this** row (`--ledger <n>`), addressed to `engineering`, closed `done` (answered), and answered by a human actor; and the writer may not be a builder session or actor. Otherwise rule `settle-needs-answer`. One ask settles one row; the RED-read ask is never tied to a row.
- `fixed` records the fix sha. It counts as settled only once a later verdict on that row (higher seq), from an independent session, at a sha equal to or containing the fix sha, is SOUND (or REFUTED for a flag). A later RISKY or WRONG verdict reopens the row.
- `deferred` makes the tracker create, in the same transaction, a self-contained ticket: title `Deferred: <decided excerpt>`, `current-behaviour` = the row (kind, decided, why, files), `desired-behaviour` = the latest verdict's note, one criterion `ledger-<n> of <ticket> resolved as the reviewer describes` (a draft for its builder to edit), parent = the reviewed ticket's nearest project (else its root), a `relates` edge to the source ticket. It is born Framed. The settlement stores its id.
- A `reward-hack` row whose latest verdict is CONFIRMED accepts only `fixed` (rule `reward-hack-final` for `accepted`, `overruled`, `deferred`).
- A `contradiction` row is settled through Michael's ask either way: `fixed` (the ADR or spec wins and the code changed) needs the ask as well as the re-check; `accepted --adr PATH` (the code wins, the ADR is amended or superseded, linked).
- *Amended 2026-10-22 (tr-w40vn).* Four confirm-fixed asks waited on Michael at once for contradictions an auditor had found and re-verdicted sound at the fix. Michael, 2026-10-22: "the reviewer here should sign offf - they found the mistkae?" So `settle <ticket> <n> fixed --sha S` on a `contradiction` row needs **no ask** when all of these hold; otherwise the `--ask` route above applies unchanged, and fork and `reward-hack` rows are unchanged.
  1. The row's origin is `auditor`. A contradiction a builder logged still goes through its ask.
  2. The writer is the actor `auditor`, from a session that is not a builder session (`ledger settle` already needs a session for this path; rule `session-required`, and `not-independent` for a builder).
  3. The row's latest verdict from an independent session (highest seq; builder sessions and actors excluded, as for the Merged gate) was recorded by the actor `auditor`, is `sound`, and its sha is `S` or a descendant of `S`. Git's answer about that ancestry is read as `settle` reads it elsewhere: unknown is refused `sha-unverifiable`.
  Any condition missing is refused under `settle-needs-answer` (exit 1, nothing written), and the message names the first missing condition and the `--ask` route. Any session that writes as `auditor` and did not build the ticket may settle; it need not be the session that raised the row or wrote the verdict.
  The `ledger.settled` feed row for this path carries `ask: null` and two new keys, `verdict_seq` and `verdict_sha`: the seq and sha of the verdict that was relied on (both `null` for every other settlement; no schema bump, the keys are additive). The settlement is read, for the Merged gate, as made just before that verdict, so the cited SOUND verdict is its re-check and the row is settled without a further verdict. A later RISKY or WRONG verdict reopens the row as it does any fix. A settle with an ask is read as before.

**Repeated guesses.** `ledger add --origin auditor --repeat-of <t>:<n> --topic "…"` checks the target row exists. In the same transaction the tracker looks for an open self-contained ticket titled `Ticket template: add a field for <topic>` under the configured template parent; if there is one it adds `relates` edges to both tickets, and otherwise it creates one (born Framed: current-behaviour cites both rows, desired-behaviour asks for the field, one criterion) with those edges. The template parent is store setting `template-parent` (`trk config set template-parent <id>`, alongside Asks and owners' `default-owner` and this spec's `shared-agent`); unset, it is the reviewed ticket's root. The dogfood store sets it to Workflow skills integration (`tr-xh18g`).

*Amended 2026-10-11 (tr-a7hvr, as built).* The search covers the template parent's direct children only, and a closed ticket with the title is never reused (a new one is filed). `config set template-parent` takes an open item that is not an ask (unknown exits 3; `parent-closed`, `ask-leaf`); `trk config` lists it as null while unset. A template parent closed after it was set refuses the auditor row `parent-closed` with nothing written. The filed ticket is created as the auditor, with no owner of its own; its rows follow the `ledger.added` row in the same transaction (`item.created`, `criterion.added`, `stage.changed`, then one `edge.added` per new edge).

### Blast radius

- **Rules file**: `.trk/blast-radius` at the repo root, one rule per line, `RED <glob>` or `YELLOW <glob>`, `#` comments. Globs match repo-relative paths, `**` crossing directories. A file's colour is the highest of every matching rule; unmatched files are GREEN. A repo with no rules file at the rated sha is refused (rule `no-blast-rules`, exit 1, no lane recorded) naming the repo and the file to add; a rules file with only comments rates every path GREEN. *(Amended 2026-10-20, tr-pc3yw.)*
- **ADR scope**: every `docs/adr/*.md` with a line `Scope: <glob>, <glob>…` makes matching files at least YELLOW, and the classification records which ADR applied (the reviewer checks the change against it).
- **`trk blast-radius <ticket> [--sha S] [--base REF] [--repo PATH]`**: `--sha` defaults to the head sha. It runs `git diff --name-only <base>...<sha>` in the ticket's repo; `--base` defaults to the merge-base with `origin/HEAD`, else `main`, else `master`. It records one `blast-radius` evidence row at that sha, with detail `{"lane":"red|yellow|green","files":[{"path","colour","rule","adr"}]}`, and prints the lane. The ticket's **lane** is the lane in the detail of its latest counting `blast-radius` row; `show`'s JSON carries it (`lane`), and pages render it as the chip.
- *Amended 2026-10-11 (tr-y05c7).* Rated after the merge, the default base diffed nothing and rated the ticket GREEN, so a builder who rated late skipped review. The default base is now the ticket's pre-merge point when it can be found: the first parent of the latest counting `merge` row's sha, when that commit is a merge commit whose first parent does not contain the rated sha; otherwise the merge-base with `origin/HEAD`, else `main`, else `master` as before. A default base that shows no changed file is refused under rule `empty-diff` (exit 1, nothing written), naming `--base`: after a fast-forward merge the pre-merge point cannot be told from git, so the builder passes `--base <the commit the ticket branched from>`. An explicit `--base` may still rate an empty diff GREEN.
- *Amended 2026-10-21 (tr-xqea9).* Three RED review asks were opened for files the PRs never touched, each from a diff against a stale base: `git diff <old>...<head>` listed everything develop had gained since (1317 files for #1842, including develop's own migrations). Two routes led there. (a) The latest counting `merge` row was another PR's: `tr-tk1tp` had a `merge` row for PR 1687, whose merge commit's first parent (`182b69276`, 16 Oct) the next PR's rating took as its pre-merge point. The pre-merge point above now applies only when that merge commit contains the rated sha (it is this head's merge); otherwise it is ignored. (b) The builder passed `--base <the commit the branch was cut from>`, as `docs/agents.md` said, after the branch had merged the base branch. When the latest counting `pr` row's ref is a GitHub pull request (a number, `#number` or a `github.com/…/pull/N` URL), `blast-radius` asks `gh pr view <ref> --json baseRefName` and `gh api --paginate repos/<owner>/<repo>/pulls/<N>/files` for the PR's own files, all of them (`gh pr view --json files` stops at 100). The executable is `gh`, or `$TRK_GH`. Then: (1) the default base, after the pre-merge point and before `origin/HEAD`, is the PR's base branch fetched from `origin` now (`git fetch origin +refs/heads/<base>:refs/remotes/origin/<base>`), never the checkout's cached copy; the diff `base...sha` is then against the merge-base with that tip, and the row's note names `origin/<base> <sha>`. A failed fetch is refused under `sha-unverifiable`. (2) A rating, with the default or an explicit `--base`, is refused under rule `stale-base` (exit 1, nothing written, no ask opened) when a file it changes (a rename counted by its new name, as GitHub counts it) is absent from the PR's files; the message names the first of them. The check applies only when the PR's head (`headRefOid`) is the rated sha or a descendant of it; for any other head (an older head of a ticket with several PRs, a force-push) the PR's file list says nothing of what is rated, so the check is skipped with a note while the fresh base still applies. (3) `gh` installed but unable to read the PR is refused under `sha-unverifiable`; `gh` not installed, or a ref that is not a pull request (a branch, `none`), rates as before and, for a missing `gh`, prints a note that the PR was not checked. (4) A PR stacked on another open PR by merge has the parent's changes in its own file list, so the check above cannot tell them from its own; trk does not infer a parent, and the builder passes `--base <parent PR head sha>` and names the parent PR in the `pr` row's note, and does not merge the child before the parent PR has merged, or before the parent's ticket holds the review of the parent's files; trk does not check the order (`docs/agents.md` step 7).
- **The RED-read ask** (an interpretation; see [Further notes](#further-notes)). When the lane is RED, the same transaction opens, unless one is already open, an ask under the ticket to `engineering`: asker = the writer of the `blast-radius` row, title `Read RED changes in <PR ref>`, question listing the PR and its RED files. The ticket records it as its RED-read ask (`items.red_ask`). If `engineering` resolves to the writer, no ask is opened (Asks' `self-addressed` rule; the writer is the person who must approve) and `blast-radius` prints a note; the `human-review` gate applies either way.
- **`trk review record <ticket> --ref <GitHub review URL> --sha <head> approved|changes-requested --note "…"`** (or, with no `--ref`, a person's direct review: see the 2026-10-24 amendment, tr-753br) writes the `human-review` row (verdict `pass` / `fail`); `evidence add` also accepts kind `human-review`. A `pass` at the head sha answers the open RED-read ask in the same transaction, as a proxy answer: actor the writer, outcome `Approved on GitHub: <ref>`, `ask.answered` with `proxy: true` and `data.via: "evidence"` with the evidence id. This is a named exception to Asks and owners' answer permission (an agent may record it), because the approval itself happened on GitHub. `fail` leaves the ask open. Until Evidence ingestion automates pickup, an agent records the approval with the review URL.
- *Amended 2026-10-20 (tr-3nykp).* A RED rule may name the role that reviews its files: `RED:<role> <glob>`, for example `RED:product **/accepted-rulings.json`. A bare `RED <glob>` means `RED:engineering`. The role must be in the role table or be the relative role `parent`; otherwise the rules file is a usage error naming the line. Each file in the detail gains `"roles"`: the roles named by its highest-colour matching rules (every named role applies when several RED rules match, so a file can need two roles), empty for a file that is not RED; a stored RED file without the key is `engineering`'s. The RED-read ask above is opened once for each role that reviews a RED file, to that role, listing that role's files (so `items.red_ask` holds the latest one opened, and open RED-read asks are found by their title and role); a role's holder who already has a counting `human-review` pass at the head gets none. A `human-review` pass answers each open RED-read ask whose role resolves to the recording actor, and no other.
- *Amended 2026-10-20 (tr-6e23r).* A rule `SENSITIVE <glob>` marks a directory whose file names must not be stored (customer or model names). Files matching it are rated by the colour rules as usual, so a RED file there still makes the lane RED, but the detail names none of them: it gains `"sensitive": [{"dir": "<glob>", "red": n, "yellow": n, "green": n, "roles": {"<role>": n}}]`, one entry per glob with changed files (a path matching several globs counts under the first; `roles` counts the RED files each role reviews), and `files` lists only the other paths. The row's note and the RED-read asks count them without naming them. A rules file with no `SENSITIVE` line stores the detail as before.
- *Amended 2026-10-11 (tr-bqx0y).* A repo with no GitHub remote, such as this tracker's own, has no review URL. There the person approves locally, after reading the diff at the head sha, and records it themselves: `trk review record <ticket> --ref local:<actor>@<head> --sha <head> approved --note "…" --as <actor>`. The ref is free text, so nothing in the tracker changes. The proxy answer's outcome still reads `Approved on GitHub: <ref>`; the `local:` ref is what tells the two apart. This repo's rules file, `.trk/blast-radius`, was approved by Michael on 2026-10-11. Superseded for the person's own approval by the next amendment, which needs no hand-typed ref.
- *Amended 2026-10-24 (tr-753br).* A RED approval no longer starts on GitHub. `trk review record <ticket> approved|changes-requested --sha <head> --note "…" --as <person>`, with no `--ref`, writes a `human-review` row (verdict `pass` / `fail`) whose ref is `direct:<actor>@<head>`, with the full head sha as the row's sha (the console, or the phone answer-card server, passes the session as usual, for example `console` or `phone:<node>`). It is refused unless: (1) the writer is a human actor (rule `approval-needs-person`); (2) `--sha` names the ticket's head, the latest counting `pr` row's sha, compared as this spec compares shas (the same commit, short or full; one that cannot be told from the head is refused too; rule `not-head`); `--sha` is required and never defaults to the head, so a delayed console command or phone confirmation cannot approve a head the person never saw; (3) the head has a counting RED `blast-radius` row and an open RED-read ask addressed to the writer (rule `no-red-read`), so an approval cannot precede the RED context it approves. A `pass` answers that ask, as in the paragraph above, with outcome `Approved by <actor> at <short sha>`; `ask.answered` still says `proxy: true` and `via: "evidence"`, because the changes schema fixes `proxy` to true for `via: evidence` and a change to it would be a contract edit. A ref starting `direct:` (any case) is reserved: `review record --ref direct:…` and `evidence add --kind human-review --ref direct:…` are refused (rule `reserved-ref`), so the prefix reliably marks a direct review. With any other `--ref`, behaviour is unchanged: a GitHub review URL, or a `local:` ref, recorded by an agent as the named proxy exception. **Known limit.** trk cannot authenticate a person. `--as` and `--session` are asserted identity, so a direct approval is as trustworthy as the session that writes it, as for every `--as michael` answer today (`docs/agents.md`, Actors). A repo that needs more has to put an attested channel in front of `review record`. Out of scope: signed attestations (a principal, ticket, sha, decision, nonce and expiry bound by the console or phone service), closing the open `evidence add --kind human-review --ref <other>` path, and a GitHub repo's own branch protection, which still wants a GitHub review to merge there.

### Merged gate additions

On top of Built at the head, the `merge` and the `blast-radius` rows, Merged needs, for the **YELLOW and RED** lanes:

1. a counting `audit` row (at the head sha or an ancestor of it), from an independent session;
2. every ledger row has a latest verdict from an independent session, at the head sha or an ancestor of it (`ledger-verdict` missing otherwise);
3. every WRONG row is `fixed` (re-checked SOUND) or `overruled`; every RISKY row is `fixed`, `deferred` or `accepted`;
4. every CONFIRMED `reward-hack` row is `fixed` (re-checked REFUTED);
5. every `contradiction` row is settled (`fixed` or `accepted`) through an answered `engineering` ask tied to it, or, for a `fixed` row an auditor raised, by that auditor's own settle (*Amended 2026-10-22, tr-w40vn*, below);
6. **RED only**: for each role that reviews a RED file (*Amended 2026-10-20, tr-3nykp*; `engineering` for a bare `RED` rule), a counting `human-review` pass at the head sha recorded by the actor that role resolves to. `missing` has one `human-review` entry per role lacking it, with `role` and `actor`; a review by anyone else does not count for the role.

GREEN tickets need none of these; that lane merges unreviewed. A skipped Merged needs no blast-radius row and no review. A refused `advance` lists each unmet row by `ledger-<n>`.

*Amended 2026-10-11 (tr-wf46m, as built).* Independence is judged when the gate is checked, so an `audit` or a verdict stops counting once its session or actor becomes a builder of the ticket. A row's deciding verdict is its latest independent verdict at the head sha or an ancestor of it; when a later independent verdict's sha cannot be placed by git, the row's need is `sha-unverifiable` (with `ledger` and `sha`). RISKY and WRONG are settled only by a settlement recorded after that verdict (RISKY: `deferred` or `accepted`, a contradiction only `accepted`; WRONG: `overruled`), so any later RISKY or WRONG verdict reopens a settled row, not only a `fixed` one. SOUND and REFUTED stand (a reviewer may come round in debate), except that a `fixed` recorded since the latest adverse verdict before them must have its sha contained in theirs (else it is not the fix's re-check), and a REFUTED after a CONFIRMED needs such a fix: a confirmed flag clears only by `fixed` and its re-check. A contradiction judged SOUND also needs `accepted` or `overruled` (the code wins, through the ask) or a `fixed` so re-checked; one judged WRONG is `overruled` when the code wins. Each row yields at most one need. `settle` takes `--sha` only with `fixed`, `--adr` only with `accepted`, and no `--ask` with `deferred`; a settlement other than `fixed` must fit the row's latest verdict (`deferred` and `accepted` a RISKY row, `overruled` a WRONG one, and `accepted` also a SOUND contradiction) and is a usage error otherwise, so a person's answer is never spent on a settlement the gate ignores. `reward-hack-final` is checked first, then the fit, then `not-independent` for a builder deferring (the builder never settles its own row but by `fixed`), then `settle-needs-answer`. The deferred ticket's rows (`item.created`, `criterion.added`, `stage.changed`, `edge.added`) come before the `ledger.settled` row that names it.

### Ready, show and the item JSON

- Every item JSON (`show`, `tree --json`, `ready --json`, `claim --json`, `waiting --json`) gains `stage` (null off tickets), `merged_skipped`, `lane` (tickets, null until a `blast-radius` row) and `gate`: `{"next": <gate name or null>, "missing": [...]}`. `next` is the ticket's next stage, `accept` for a draft project, `close` for an accepted project, a program or a decision, and null for closed items and asks. `missing` is the same list `advance`/`accept`/`close` would refuse with.
- `ready` stays stage-blind: no new exclusions. `--stage <stage>` keeps tickets at that stage. Self-contained tickets at Framed stay claimable.
- `show` text adds a `stage:` line with the pipeline, the lane, and `next: <gate> — <n> missing` followed by the missing lines; `tree` adds the stage after the form (`[built]`).
- Git calls make `ready` and `show` slower only for tickets at Built or later, at most a few `git` processes each, cached per invocation. `ready` computes `gate` only for the rows it prints.

### Amendments to Core

- **`claim --force`**: no longer overrides `not-ready`; delivered by Core ticket `tr-aa4ge`, not here.
- **`new`**: tickets require their written sections at creation (`--section`), ≥1 `--criterion`, and are refused `gate-unmet` otherwise; programs require ≥1 `--criterion`. `--section acceptance-criteria=…` and `--section destination=…` are refused (`generated-section`, pointing at `--criterion`). `item.created` data gains `stage` and `criteria` (handles).
- **The existing suite**: fixtures that create tickets without criteria, or close tickets `done` directly, stop working. The first ticket of this project updates them (a test helper that creates a gated ticket and walks it to Verified) and keeps the suite green.
- **Spec 01** carries a dated amendment note pointing at this section (added with this spec), as `tr-aa4ge` added one for `claim --force`.
- **`close`**: `done` checks gate `close` (projects, programs, decisions) and is refused on tickets (`done-needs-verified`).
- **`reopen`**: a ticket closed at Verified needs `--to-stage` and `--reason`.
- **`spec accept`/`spec draft`**: as in [Spec state](#spec-state).
- **`section set`/`section rm`**: refused on generated sections.
- **Sections table**: `acceptance-criteria` and `destination` stay expected handles but become generated; Core's "Core never requires a section" is replaced by the gates here.
- **`config set`**: adds keys `template-parent` and `shared-agent`.
- New rules: `gate-unmet`, `not-skippable`, `not-backward`, `done-needs-verified`, `no-criteria`, `last-criterion`, `generated-section`, `bad-retract`, `ledger-tickets-only`, `not-independent`, `session-required`, `settle-needs-answer`, `reward-hack-final`, `empty-diff` (amended 2026-10-11, tr-y05c7). Writes to closed items reuse Core's `closed`. Exit codes and the error JSON are Core's (plus `gate`/`missing`).
- `contract/changes.schema.json` adds `stage.changed`, `criterion.added|edited|retired`, `evidence.added`, `ledger.added|verdict|settled|promoted` and their data shapes, and the new `item.created`/`spec.changed`/`ask.opened`/`ask.answered` data keys. None of this bumps `schema`; ADR 0002 point 5 gets a one-line amendment listing the new kinds.

### Amendments to Asks and owners

- **`trk ask … --ledger <n>`** ties the ask to ledger row `n` of its parent ticket (the parent must be a ticket with that row; otherwise usage error). Stored in a new table `ledger_asks` (item_id, n, ask_id); `ask.opened` data gains `ledger` (`ledger-<n>`, or null). The ask's item JSON gains `ledger`.
- **Proxy answer by evidence**: a `human-review` pass answers the ticket's RED-read ask whoever records it (see [Blast radius](#blast-radius)); `ask.answered` `data.via` gains the value `evidence`, with `evidence` (the id).
- A ticket stores its RED-read ask in `items.red_ask` (null otherwise).

### Amendments to Page publisher

- Its Affected pages table gains `ledger.*` rows (mark the ticket and every ancestor), alongside `stage.changed` and the evidence and criterion rows. Without it the `review` section would never update, since unknown kinds mark nothing.
- Its evidence rows show the row's **gate** (which may be `accept` or `close`, not only a stage) where it says "stage".

### Migration

One migration, numbered at build time after whatever schema version is current then (Asks and owners is 2; Page publisher takes one). Like Asks and owners', it receives the write `Transaction`, so its rows are journaled; the journal's existing rows are never touched. The actor on its rows is the store's default actor, with `data.via: "migration"`.

1. Add `items.stage`, `items.merged_skipped`, `items.red_ask`; create `ledger_asks`, `criteria`, `criterion_versions`, `evidence`, `ledger`, `ledger_verdicts`, `ledger_settlements`, `ledger_promotions` with their triggers.
2. **Criteria from written sections.** For every item with a non-empty `acceptance-criteria` (tickets, projects) or `destination` (programs) section: each top-level list item (`- `, `* `, `1. `, with or without `[ ]`/`[x]`) becomes one criterion in order, its text collapsed to one line; a section with no list items becomes one criterion of its whole text, cut to 500 chars. The section is then renamed to the extra handle `acceptance-criteria-imported` / `destination-imported`, so nothing written is lost; the rename is journaled as `item.updated` with fields `section:<old>` and `section:<new>`. One `criterion.added` row each. This is a one-time import, not prose parsing as a model (which Stage gates rejected).
3. **Stages.** Every open ticket gets stage `framed`, then auto-advances through `specd` when its gate holds from data (`stage.changed`, `via: migration`). Tickets that fail the Framed gate still get `framed` (the gate checks creation, not existing items), never auto-advance, and their `gate.missing` shows what to fill. Closed tickets get stage null: they closed before gates existed, and pages show their status.
4. Existing claims, edges and asks are untouched.

## Testing decisions

- **One seam: the CLI.** Same harness as Core and Asks and owners: pytest through `main(argv)` against a temp `$TRK_HOME`, `$TRK_NOW` under `$TRK_TEST=1`, every journal row validated against `contract/changes.schema.json` by the autouse fixture. Tests assert on exit codes, printed JSON (including missing lists) and feed rows, never on tables or module internals. Prior art: `tests/test_ready.py` (golden scenarios with `why_not` cross-checks), `tests/test_claims.py`, `tests/test_spec_state.py`, `tests/test_acceptance.py`.
- **Real git, no mocks.** Ancestry and blast-radius tests build a throwaway git repo in `tmp_path` (commits, a branch, a merge), register it with `--repo`, and pass its real shas. The suite already uses `git` (`tests/test_hooks.py`).
- **Gate table test**: for each stage, a ticket missing exactly one piece at a time is refused with exactly that `need`, and `show`'s `gate.missing` equals the refusal's `missing` for every item in the store (the `why_not` pattern, by construction).
- **Weighing tests**: flaky fail then pass (passes); pass then fail (blocks); retracted pass (blocks); evidence on an edited criterion's old version (blocks); evidence before a `reopen-stage` (blocks above the target stage, counts at or below it); Verified at a sha not containing the merge sha (`wrong-sha`); an unknown sha and a repo path that is not a checkout (`sha-unverifiable`); short and full shas of the same commit compare equal.
- **Independence matrix**: for verdict, auditor row and audit, each of the builder session (by ledger row, by `pr`, by `test-run`, by a pre-Built claim), the builder named agent under another session, the shared agent under another session, and a fresh session, asserting allowed or `not-independent`; settlements with and without a qualifying answered `engineering` ask tied to the row, and with an ask tied to another row.
- **Lane tests**: rules file with RED and YELLOW globs, an ADR with a `Scope:` line, no rules file (refused with `no-blast-rules`), a comment-only rules file (every file GREEN); the RED ask opened once, not opened when self-addressed, answered by a `human-review` pass, left open by a fail; a fix commit's new head needing a fresh `blast-radius` while the audit carries over.
- **Migration test**: a fixture store at the prior schema with a ticket whose `acceptance-criteria` is a checklist, a project whose criteria are prose, a program with a destination list, an open spec-bound ticket under an accepted project and a closed ticket. After opening under the new CLI: rows created as described, `-imported` sections hold the old text, stages set, the journal's old rows unchanged.
- **Append-only**: `UPDATE`/`DELETE` on `evidence`, `ledger`, `ledger_verdicts`, `ledger_settlements`, `ledger_promotions` abort at the SQLite level.

## Acceptance criteria

End-to-end checks. **Setup** for each, unless stated: a fresh store (`trk init --me michael`; agents `worker`, `builder` and `reviewer` added with `trk actor add … --kind agent`); a throwaway git repo R with `main` and a feature branch whose head is `<head>`; program P created with one `--criterion`; project J under P with `--repo R`, every spec section written, one criterion, a `judgment` for `accept`, and `spec accept J` run. "A ticket at Built" means a spec-bound ticket under J (born Spec'd) with `pr` at `<head>` and a passing `test-run` at `<head>` recorded by session W as `builder`, then `advance`. Sessions W (builder) and V (reviewer) are distinct uuids.

1. Under a second project K (draft): `trk new work-item "T" --form spec-bound --parent K` with no section and no criterion exits 1, rule `gate-unmet`, missing `section` (`what-to-build`) and `criterion`, and no row is written. With `--section what-to-build=-` and `--criterion "c1"` it succeeds at `framed`, journaling `item.created`, `criterion.added` and one `stage.changed` (`via: create`). `spec accept K` exits 1 listing every empty section and `judgment`; once they are filled and a `judgment` is recorded, it succeeds and T moves to `specd` (`via: spec-accept`). Then `spec draft K --reason "…"` moves T back to `framed`, and T's `why_not` has `spec-not-accepted`.
2. A self-contained ticket S under J is born `framed`; `trk advance S --judgment "complete repro"` moves it to `specd` and writes one `judgment` row; `trk claim S --session W` succeeds.
3. A ticket T1 under J (born `specd`): `advance` exits 1 missing `pr` and `test-run-pass`. With `pr` at `<head>` and a passing `test-run` at another commit, it still refuses (`test-run-pass`); with a passing `test-run` at `<head>`, `advance` moves T1 to `built`. `ready --root P --json` and `show T1 --json` report `gate.next` `merged` with a `missing` list equal to what a refused `advance` prints.
4. T1 continued, R has a comment-only `.trk/blast-radius` (with no rules file at all `blast-radius T1` is refused with `no-blast-rules`): `blast-radius T1` records lane green. `merge` evidence at merge commit `<m>` (on `main`, containing `<head>`), then `advance` moves T1 to `merged`. A passing `test-run` for gate `verified`, `ac-1`, at a commit not containing `<m>` is listed by `evidence T1` as `wrong-sha`; a failing one at `<m>` makes `advance --outcome "…"` exit 1 (`criterion-pass`, latest fail); a later passing one at `<m>` lets `advance --outcome "…"` move T1 to `verified` and close it `done` in one transaction (`stage.changed` then `item.closed`).
5. `trk close <open ticket> --resolution done --outcome "…"` exits 1, rule `done-needs-verified`; `--resolution wont-do` on a ticket at `framed` succeeds.
6. Ticket T2 under J: `criterion edit T2 ac-1 "…"` bumps the version, and a `verified` pass recorded at v1 is listed `old-version`. `criterion add` then `criterion retire T2 ac-2 --reason "…"` shows ac-2 retired in `criteria T2`; retiring the last live criterion exits 1 (`last-criterion`); `section set T2 acceptance-criteria` exits 1 (`generated-section`); `evidence add T2 --kind skip …` exits 2.
7. Ticket T3 under J taken to `merged` as in AC 4, with a `verified` pass at `<m>`; `reopen-stage T3 built --reason "…"` moves it back, and `evidence T3` lists that pass and the `merge` row as `before-reopen`, while Built's `pr` and `test-run` still count.
8. Ticket T4 under J with `pr --ref none` at `<head>` and a passing `test-run` there, at `built`: `skip T4 merged --reason "no repo change"` moves it to `merged` with `merged_skipped`, and a `verified` pass at `<head>` then lets it reach `verified`. A ticket at Built with a real PR ref gets `not-skippable` from `skip`, and `skip <t> built` exits 1 (`not-skippable`).
9. Rules file `YELLOW src/**` in R, ticket T5 at Built changing `src/a.py`: `blast-radius T5` records lane yellow. As `builder` in W, `ledger add T5 --kind fork …` succeeds as `ledger-1`; `ledger verdict T5 1 sound` exits 1 (`not-independent`) from W, and from a third session as `builder`. From V as `reviewer`: `ledger add --origin auditor` (`ledger-2`), `ledger audit T5`, verdict `wrong` on ledger-1 and `sound` on ledger-2. With `merge` evidence, `advance` exits 1 listing `ledger-1` as `ledger-settled`. As `builder` in W: a fix commit `<f>` with a new `pr` row and passing `test-run` at `<f>`, `settle T5 1 fixed --sha <f>`, and `blast-radius` at `<f>`. From V: verdict `sound` on ledger-1 at `<f>`. With `merge` evidence at a commit containing `<f>`, `advance` moves T5 to `merged` (the audit at `<head>` carried over as an ancestor of `<f>`).
10. Ticket T6 at Built in the YELLOW lane, with a `reward-hack` row CONFIRMED from V: `settle … overruled --ask <A>` exits 1 (`reward-hack-final`), where A is an answered `engineering` ask tied to that row; only `fixed` plus a REFUTED re-check from V clears it. A RISKY row with `settle … accepted --ask <B>`, B tied to it but still open, exits 1 (`settle-needs-answer`). `settle … deferred` on another RISKY row creates a self-contained ticket at `framed` under J with a `relates` edge to T6.
11. Rules file `RED contract/**`, ticket T7 at Built changing `contract/x.json`, `blast-radius` written as `worker`: one `engineering` ask is opened under T7 (asker `worker`); a second `blast-radius` run opens no other. `review record T7 … changes-requested` leaves it open and Merged (with audit, verdicts and merge) refused with `human-review`; `review record T7 … approved` at the head sha answers it (`proxy: true`, `via: evidence`) and Merged succeeds.
12. With `config set template-parent <W>` (a project under P): an auditor row on T5 with `--repeat-of <T6>:1 --topic "units"` creates `Ticket template: add a field for units` under W with `relates` edges to T5 and T6; a second repeat with the same topic from another ticket adds an edge to the existing ticket instead of filing another.
13. Project L under P, accepted, with one ticket closed `wont-do`: `close L --resolution done` exits 1 (`gate-unmet`, `criterion-pass` for its criterion); after a passing `judgment` for gate `close` on that criterion, it succeeds. A decision with an empty `resolution` fails `close --resolution done` (`resolution`); with it written, it succeeds. A draft project with a passing `close` judgment on every criterion fails `close --resolution done` (`spec-accepted`) until its spec is accepted, and closes `wont-do` while draft (amended 2026-10-11, tr-afrvc).
14. `ready --root P --stage merged` keeps only tickets at `merged`, and every `ready --json` row's `gate.missing` equals `show`'s for that item.
15. `changes --since 0` replays every new kind, all validating against the updated schema; `sqlite3 store.db "delete from evidence"` fails with the trigger's abort.
16. **Dogfood**: a backup of the live store opens under this build: Tracker v1's open tickets have stages, its written criteria are rows with the old text under `-imported` handles, and `trk show tr-yk91p` lists its destination as criteria. Then the live store is upgraded, `template-parent` is set to Workflow skills integration (`tr-xh18g`), and this project's own last ticket reaches Verified through its gates.

## Out of scope

- **`claim --force` and the not-ready refusal**: Core ticket `tr-aa4ge`.
- **Evidence ingestion**: recording webtester runs, PRs, CI output, Codex reviews, GitHub approvals and blast-radius runs automatically (map fog). Every row is recorded by a CLI call here.
- **The builder's and auditor's directions**: when to log a fork versus open an ask, the trivia rule, running the auditor and reviewer in fresh sessions, the 5-round debate and its stalemate ask, proposing ADR promotions. Workflow skills integration (`tr-xh18g`) owns them; the tracker only stores rows and enforces independence and the gates.
- **Rendering**: stage pipeline, criterion lists, evidence table, review ledger and lane chip are Page publisher's, already specified there.
- Git hooks, PR or CI checks, editor hooks and the herdr plugin as enforcement points (Where the build discipline is enforced). Any escape hatch or bypass record.
- Authenticating actors or sessions (identity stays self-declared; independence catches mistakes, not impostors), checking GitHub for real approvals, defect-escape rate per lane, a "caused by" edge, and stage-aware queues.

## Further notes

- **Interpretation, return to draft.** Stage gates says a project's spec-bound tickets "still at Framed leave the frontier; tickets past Spec'd keep their stage". This spec reads that as: Spec'd tickets fall back to Framed (their gate no longer holds), Built and later keep their stage, and Core's existing `spec-not-accepted` rule keeps all of them off the frontier, so `ready` stays stage-blind as Stage gates requires. Re-acceptance auto-advances the Framed ones again.
- **Interpretation, "contains".** Stage gates' "a sha containing the merge sha" needs git ancestry, so the tracker runs `git merge-base --is-ancestor` against the ticket's local checkout. The same ancestry check carries audits and verdicts over a fix commit. All git calls live in one module.
- **Interpretation, the RED-read ask.** Stage gates rejected auto-opened asks for unmet gates, and Decision ledger names "the ticket's RED-read ask" without saying who opens it. This spec has `blast-radius` open it, because the RED read is a gate whose answer is a person's approval and the ask is how a person learns it is needed. It is the only ask the tracker opens; an unmet gate still never opens one.
- **Interpretation, review after a fix.** Decision ledger pins `human-review` and `blast-radius` to "the head sha being merged". A fix pushed during review moves the head, so those two are redone at the new head, while the audit and the verdicts on unchanged rows carry over by ancestry; only the fixed row needs a fresh verdict.
- **Generated sections in AGENTS.md.** AGENTS.md tells agents to `section set … acceptance-criteria`; that becomes `trk criterion add` and `trk new … --criterion`. Updating it is part of this project's last ticket.
- Suggested slice order for the tickets: stage column + criteria rows + generated sections + migration + the existing suite's fixtures → evidence + weighing + `advance`/`skip`/`reopen-stage` + missing lists in `show`/`ready` → `spec accept`/`draft` and `close` gates → blast radius + lanes + RED ask + `review record` → ledger tables, verbs and independence (needs Asks and owners' `ask` for `--ledger`) → Merged gate additions, deferral and repeated guesses → dogfood upgrade and AGENTS.md.

## Acceptance

Accepted by the authoring agent (wC7:p12) on 2026-10-11, after an independent review against Stage gates, Where the build discipline is enforced, Decision ledger in the tracker, Core ticket `tr-aa4ge`, the Core, Asks and owners and Page publisher specs, the built code, ADR 0001/0002 and CONTEXT.md. The review raised 31 findings, and all were applied. The main ones:
- A fix pushed during review moves the head sha: Built is re-checked at the new head, `blast-radius` and `human-review` are redone there, and the audit and verdicts carry over by ancestry.
- Acceptance criteria were re-staged with one ticket each, a named `builder` agent and verdicts on every row, so that their setups produce what they assert.
- Settling asks are tied to one ledger row (`ask --ledger <n>`), and a contradiction's `fixed` takes its ask.
- The RED-read ask names its asker, skips a self-addressed ask, and is answered by `human-review` as a named proxy exception. It is flagged as an interpretation.
- `evidence add` refuses the tracker-managed kinds. `skip` is limited to no-repo-change tickets, so it can't route around review. Retractions never move a stage.
- Weighing groups are defined, `judgment` no longer counts for Verified, and sha comparison and git exit codes are pinned down.
- `close done` on a project or program needs at least one live criterion. Core's `open-children` refusal still runs first.
- The builder actor covers every builder act, and the shared agent is a setting.
- The dependency on Asks and owners, the Page publisher's `ledger.*` marks, the migration's journaling and the existing suite's fixtures are now stated.

Agents accept specs under this program's Notes.
