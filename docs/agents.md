# Working with several agent sessions

This page explains the workflow trk is built for: one person, a driver session that plans, and worker sessions that build and review. It shows how the sessions share work through trk. Read the [README](../README.md) first. The skills in `skills/` hold the same rules in a form an agent loads; this page is for the person who sets it up.

## The cast

| Who | What they do | How they write to trk |
|---|---|---|
| **You** | Decide what matters. Answer asks. Approve risky changes. | `--as <your actor>` (the default actor) |
| **Driver** | One session that keeps a program moving: picks the next work, starts workers, checks what comes back. It does not build. | `--as driver` |
| **Worker** | A fresh session that builds one ticket, or specs one project. | `--as worker` |
| **Auditor, reviewer** | A separate session that checks a worker's output. | `--as auditor` |

`trk init` registers only `herdr` and `publisher` besides you. Register the agents you use:

```bash
trk actor add worker --kind agent
trk actor add driver --kind agent
trk actor add auditor --kind agent
trk actors
```

Always pass `--as`. Without it, a write is recorded under the default actor, which is you. That would make an agent's work look like yours and would let it pass gates meant for a person.

trk now enforces this. A write with neither `--as` nor `$TRK_ACTOR`, run inside an agent session, is refused (exit 2, rule `actor-required`) and writes nothing; the message names `--as` and `trk actors`. A process counts as an agent session when `$CLAUDECODE` or `$TRK_SESSION` is set. Reads are never refused, and `trk claim renew`, which the session hooks run, is exempt because it only extends leases. Your own terminal sets neither variable, so the default actor applies there as before. This is Michael's ruling tr-shdmq, 2026-10-22: "Refuse the write when it comes from an agent session; your own terminal keeps working as you."

## Independence

Some gates need a review from someone who did not build the work. trk decides who built it by Claude Code session id. A subagent shares its parent's session, so it never counts as independent. A reviewer must be a separate Claude Code session. Start one in another terminal, tab or pane.

## One ticket, start to finish

1. **Find work.** `trk ready --root <program> --json` lists the frontier: what nothing blocks, urgent items first, then in tree order. `trk show <id> --json` says why an item is not ready (`why_not`) and what its next gate lacks.
2. **Take it.** `trk claim --next --root <program> --as worker` takes the next ready item without racing another session. `trk claim <id>` takes a named one. Exit code 4 means another session holds it, so pick another. The hooks renew the claim every turn.
   - When you work in a worktree, record it as you claim: `trk claim <id> --worktree <path>`, or later `trk worktree set <id> --path <path>`. trk stores the real path, reads the branch from git and records your session. Then `trk where <path>` answers who owns the tree, which a disk cleanup needs.
   - When you remove the tree, clear it in the same step: `trk worktree clear <id>`.
   - Before you remove a tree, archive its `tmp/` to the item's `effective_archive`, with `<tree>` replaced by the tree's directory name. When that is null, use `~/work/archive/<tree>-tmp.tgz`. A program sets its own location with `trk edit <program> --archive <path template>`.
3. **Read the last handoff**, if the work was started before: `trk handoff show <id>`.
4. **Frame and judge.** A self-contained ticket moves from `framed` to `specd` with `trk advance <id> --judgment "<why it is complete on its own>"`. A spec-bound ticket moves when its project's spec is accepted (see below).
5. **Build** in a branch or worktree. Log every choice that could change correctness as a fork: `trk ledger add <id> --kind fork --decided … --alternative … --why …`.
6. **Prove it is built.** Record a `pr` evidence row (`--ref <PR or branch> --sha <head>`) and a passing `test-run` row at the same commit. Then `trk advance <id>`.
7. **Rate the risk.** `trk blast-radius <id>` rates the changed files GREEN, YELLOW or RED, using the lanes in `.trk/blast-radius` in the repo.
   - Do not pass `--base` for a ticket whose `pr` row is a GitHub pull request. trk asks `gh` for the PR's base branch, fetches that branch from `origin` now and rates only the PR's own changes.
   - A base you choose yourself, such as the commit you branched from, goes stale when your branch merges the base branch. The rating then includes the other team's files. trk refuses it (rule `stale-base`) when the PR's own file list, read with `gh api`, lacks a file the rating includes.
   - A PR stacked on another open PR by merge, such as 1820 on 1915, has the parent's changes in its own file list, so the default rating reads them as yours. Pass `--base <parent PR head sha>` and name the parent PR in the `pr` row's note, and do not merge the child before the parent PR has merged, or before the parent's ticket holds the review of the parent's files; trk does not check the order.
   - GREEN merges without review.
   - YELLOW needs an independent audit, and a verdict on every ledger row.
   - RED also needs a person's approval, recorded by that person. The `blast-radius` run opens a RED-read ask for the reviewer. The ask shows a review brief (see "Writing the review brief" below), so the approver can decide without reading the code first. The reviewer reads the brief and the diff, then runs `trk review record <id> approved --sha <head> --note "…" --as <person>`, with no GitHub review. trk refuses it for an agent actor, for a sha that is not the head, and when the head has no RED rating and open ask for that person. Pass `--ref <review URL>` instead to record a GitHub review. trk cannot authenticate who types `--as`: a direct approval is as trustworthy as the session that writes it.
   - Use `--base <commit>` only for a ticket with no pull request, or after a fast-forward or squash merge, when the default diff is empty. Pass the commit the work started from.
8. **Merge** and record a `merge` evidence row at the merge commit. Then `trk advance <id>`. A ticket that changes no repo uses `trk skip <id> merged --reason "…"`.
9. **Verify.** Record one passing `verified` run for each live criterion, at a commit that contains the merge. Then `trk advance <id> --outcome "<one sentence>"`. This closes the ticket `done`. `trk close --resolution done` is refused on a ticket.

A refused step is an answer, not an obstacle. The error lists what is `missing`. Do that work and try again. Never route around a gate by retracting evidence you dislike or writing as another actor.

## Reviewing someone else's ticket

The reviewer, in its own session:

```bash
trk ledger audit <id> --note "…" --as auditor
trk ledger verdict <id> <n> sound|risky|wrong --note "…" --as auditor
```

### Writing the review brief (RED tickets)

Michael, 2026-10-23, on the tr-ht564 RED-read ask: "this is not helpful to me. I need to understand how to make the decision, risk, impact, decision criteria, things to consider -- this is rated RED -- if you are a junior engineer going to a senior engineer, what would you bring forth so an informed decision can be made?"

When the ticket's latest counting `blast-radius` row is RED, `trk ledger audit` is refused (exit 1, rule `brief-required`) until the ticket has a `review-brief` section with all nine headings below, each with text. The refusal names the missing ones. The Merged gate asks for the same brief whenever the latest counting rating is RED, whatever order the audit and the rating came in, and `trk show` lists it as `missing: review-brief (missing …)`, so an audit made before the RED rating, after a re-rating, or before the brief was removed does not get a RED ticket past Merged without it. YELLOW and GREEN tickets are not affected. The independent auditor writes the brief before it audits: `trk section set <id> review-brief --file F --as auditor`. Use these headings, in this order, in plain words:

1. **Decision**: one sentence naming the change, the PR and the head sha.
2. **What changes and why**: the problem it solves, citing the ruling or ticket that asked for it.
3. **Why it's RED**: the blast-radius rule each RED file matched, and what that rule protects.
4. **Impact if wrong**: who or what breaks, how it would be noticed, whether data is at risk, whether it is reversible.
5. **Evidence**: tests and probes run, the audit verdicts, findings fixed or deferred.
6. **Rollback**: how to undo it, including whether any migration can be reversed.
7. **Decision criteria**: "approve if …" and "hold if …".
8. **Where to look**: two to four diff spots with file:line, plus questions only the approver can answer.
9. **Recommendation**: approve or hold, why, and the auditor's confidence.

A heading is a Markdown `#` heading, or a line that holds only bold text; case, a list number and a final colon do not matter. The RED-read ask's question shows the brief above the PR, the RED files and the approve command. Until the brief is complete, the question says "brief pending". When the section is written, changed or removed, trk rewrites each open RED-read ask's question to match. The Needs-you page and the item page show the same text.

The builder fixes a row that is risky or wrong, then settles it with `trk ledger settle <id> <n> fixed --sha <fix>`. The reviewer checks again. A row can also be deferred (trk files a ticket for it), accepted or overruled; the last two need a person's answer to an ask tied to the row.

A contradiction an auditor raised is settled by an auditor, with no ask. Michael, 2026-10-22: "the reviewer here should sign offf - they found the mistkae?" When the fix is in and the auditor's latest verdict on the row is `sound` at the fix sha or a descendant of it, an auditor session that did not build the ticket runs `trk ledger settle <id> <n> fixed --sha <fix> --as auditor`. trk refuses it, naming what is missing, for a builder, for a row a builder logged, and when there is no such `sound` verdict. A contradiction settled through an answered ask works as before.

## Asks: when an agent needs a person

An agent that needs a decision opens an ask. It does not stop and wait.

```bash
trk ask <item> --to product "Should the button open in a new tab?" --question -
```

The ask goes to a **role** (`product`, `engineering`, or `parent`, the owner of the item above). While it is open, the item leaves the frontier, so no other agent starts it. `trk waiting` lists the open asks for an actor, highest urgency score first. Each open ask has an urgency score, computed on read: the number of open items it holds up (its parent and what that blocks through `blocks` edges) × a cache factor that rises from 1 to 10 over the first 60 minutes of waiting (a `trk readdress` restarts the wait; a role, owner or move re-route does not), while the asker's prompt cache is warm, then falls back to 1 and × 10 if the ask or its parent is urgent. `trk waiting` sorts by it, highest first. `--json` adds `urgency` with the parts (`score`, `base`, `held_up`, `minutes`, `cache_warm`). A person answers with `trk answer <ask> --outcome "<gist>" --answer -`, or by commenting on the ask's page if you publish pages. The asker confirms a comment as the answer.

A person's free text is a reply, not yet an answer: it may not answer the question. A client that passes it on runs `trk reply <ask> --as <person> --body -`. The ask stays open, and the reply shows in the ask's `replies` with an id like `reply-412`. The asker confirms it with `trk answer <ask> --from-comment reply-412 --outcome "<gist>"`, or follows up with its own `trk reply`. A reply by the asker is listed but never confirmable (rule `own-reply`). `trk answer --as <person>` is only for a person who says "this is my answer".

Closing an item with an open ask is refused (exit 1, rule `open-asks`), naming each ask, whether it hangs on the item or on a descendant. Answer each ask, or withdraw it with `trk withdraw <ask> --reason "<why>"`. A close never withdraws one for you. This is Michael's ruling tr-29kgp of 2026-10-22: "Refuse to close until each open question is answered or withdrawn on purpose."

A good ask reads on its own. It names the question in its title, states the agent's recommendation in the first paragraph, lists two to four options and says what the answer unblocks. The `trk` skill has the full rule.

Things only a person may do are always asks: approving a RED change, accepting a spec on your behalf, sharing to a new team.

## Specs and programs

- A **project** holds a spec of six sections. A different session from the writer judges it, and `trk spec accept <project>` moves the project's framed tickets to `specd`. `trk spec draft <project> --reason "…"` sends it back.
- A **program** holds a destination: checkable lines. Create tickets only after the spec is accepted, then wire blocking edges with `trk link <a> blocks <b>`.
- A project or program closes `done` when every child is closed and each live criterion has a passing `close` row.

The `trk-create-program` skill walks a session through charting a program with you. The `trk-drive-program` skill walks a driver session through running one.

## Handoffs

Before a session stops, pauses for long, or runs `/clear`, it writes a handoff: `trk handoff add <id> --as worker` with a first line that says where the work stands and what comes next. A later session reads it with `trk handoff show <id>` and starts from there. A `/clear` makes a new session id, so the old claim lapses and the session must claim again.

## Starting worker sessions

trk does not start sessions. You, or the driver, start each one in a terminal. A good starting prompt names the item id, the skills to load (`trk` first), the actor to write as, and where to report. Say outright that every trk write carries `--as worker`.

A terminal multiplexer helps: one pane per worker, named after its item. herdr is one that reads trk's change feed and can open panes, route asks and renew claims for idle sessions. See the [README](../README.md#using-trk-with-herdr-optional). It is optional.

## Machine holds

Before starting a worker session or a heavy run (a full test suite, a corpus run), a driver checks `~/.local/state/hx/hold.json`, which `hx hold` and `hx resume` write.

**Held** means `~/.local/state/hx/hold.json` exists and its `until` is null, missing, or a time in the future. A file that can't be read or parsed counts as held, so a bad file never releases a hold. A missing file, or an `until` in the past, means not held. The rule needs only the path and this test, not the rest of hx's contract (the file holds `{id, reason, by, since, until|null}`).

While held, the driver starts nothing, keeps its workers holding, and says `held: <reason> (until <until|further notice>)` in its turn summary. When the hold ends it resumes and staggers its starts.

Worked example: one command prints `held: <reason> (until <until|further notice>)` or `not held`. Run once per case against a temp `$HOME`, it printed:

| File | Output |
|---|---|
| none | `not held` |
| `until` in the past | `not held` |
| `until` null | `held: demo pause (until further notice)` |
| `until` in the future | `held: load (until 2099-01-01T00:00:00Z)` |
| not JSON | `held: hold file unreadable (until further notice)` |

```bash
python3 -c '
import json, sys, datetime as d
try:
    f = json.load(open(sys.argv[1]))
    u, r = f.get("until"), f.get("reason", "no reason given")
    held = u is None or d.datetime.fromisoformat(u.replace("Z", "+00:00")).astimezone() > d.datetime.now().astimezone()
    why = r + " (until " + (u or "further notice") + ")"
except FileNotFoundError:
    held = False
except Exception:
    held, why = True, "hold file unreadable (until further notice)"
print("held: " + why if held else "not held")
' ~/.local/state/hx/hold.json
```

## Rules that keep a shared store safe

- Never edit the store's files. Every change goes through the CLI.
- Never run a worktree's build of trk against the real store. Use plain `trk …` for the live store; trk prints `trk: store <path> (from $TRK_HOME)` on stderr whenever `$TRK_HOME` is set, so a stray export is visible. For a build under test, set `TRK_HOME=<scratch>` and `TRK_TEST=1` on the same command line.
- Never stop test runs with `pkill -f` or `killall`. Other sessions on the machine run theirs at the same time. Stop only the process you started, by its process id.
- Put no secrets in a section, a handoff or an ask. Pages show them to everyone the publisher shares with.
