# trk

trk is a small issue tracker for work done by AI coding agents and the people who direct them. You run it from the command line. It keeps all its data in one SQLite file on your machine. It has no server and no dependencies beyond Python.

Most trackers assume a person reads each ticket and moves it along by hand. trk assumes an agent does that, in a fresh Claude Code session, with no memory of earlier sessions. So trk does three things for the agent:

- **It says what to do next.** `trk ready` lists the work that nothing blocks. An agent takes one item with a claim, which is a lease that lapses if the agent stops.
- **It checks the work.** A ticket moves through stages called gates. Each gate refuses to open until the evidence is on record: a pull request, a passing test run, a safety rating, a review. The agent cannot skip a gate by saying it is done.
- **It asks a person only when it must.** An agent that needs a decision opens an ask addressed to a role. The ask takes that item off the list of ready work until someone answers.

trk is a CLI first. It also ships Claude Code skills that teach an agent the rules, hooks that keep an agent's claims alive, and an optional publisher that shows each item as a web page. It works with [herdr](#using-trk-with-herdr-optional), a terminal multiplexer for agents, but does not need it.

## Contents

- [Install](#install)
  - [Get the code onto the machine](#get-the-code-onto-the-machine)
  - [Run the installer](#run-the-installer)
- [Quickstart](#quickstart)
- [Concepts](#concepts)
- [Using trk with Claude Code](#using-trk-with-claude-code)
- [Using trk with herdr (optional)](#using-trk-with-herdr-optional)
- [Publishing pages (optional)](#publishing-pages-optional)
- [The store is machine-wide](#the-store-is-machine-wide)
- [Upgrading](#upgrading)
- [Backup and restore](#backup-and-restore)
- [Uninstall](#uninstall)
- [Troubleshooting](#troubleshooting)
- [Repo layout](#repo-layout)
- [More documentation](#more-documentation)

## Install

You need Python 3.12 or newer, [uv](https://docs.astral.sh/uv/) (a Python tool installer), and git. Claude Code is optional but is the point of the skills and hooks.

### Get the code onto the machine

This repo has no hosted home. Copy it from a machine that has it, in one of two ways.

**Clone from a path or an ssh host.** Git can clone straight from another machine:

```bash
git clone user@host:~/projects/beads-alternative trk
```

Replace `user@host:~/projects/beads-alternative` with the real location. A plain directory path works too: `git clone /path/to/beads-alternative trk`.

**Send one file.** On the machine that has the repo, make a bundle. A bundle is a single file that holds the whole repo and its history:

```bash
cd beads-alternative
./install.sh make-bundle trk.bundle    # or: git bundle create trk.bundle master --tags
```

Copy `trk.bundle` to the new machine by any means you like (`scp`, a USB stick). Then clone from it:

```bash
git clone trk.bundle trk
```

### Run the installer

```bash
cd trk
./install.sh --dry-run    # shows what would change, changes nothing
./install.sh
```

The installer does these steps. Every step skips itself when its work is already done, so you can run the script again at any time.

1. Checks for Python 3.12 or newer and for uv.
2. Installs the `trk` command with `uv tool install --editable`. "Editable" means the command runs the code in this checkout, so a `git pull` updates it. Pass `--no-editable` to install a copy instead.
3. Creates the store with `trk init` if none exists. It never touches an existing store. Pass `--me NAME` to choose the actor id for yourself; the default is your user name.
4. Copies the three Claude Code skills from `skills/` into `~/.claude/skills`. If an installed skill differs from the repo copy, it stops and changes nothing; `--force` replaces it and keeps the old copy aside.
5. Adds three hook entries to `~/.claude/settings.json`. It backs the file up first, shows you the diff, and leaves every other setting alone.
6. With `--pages`, installs and starts the page publisher's timer. See [Publishing pages](#publishing-pages-optional).
7. Prints the next steps.

The installer never reads, prints or stores a token. Run `./install.sh --help` for every option. [docs/install.md](docs/install.md) explains each step in detail.

Check that it worked:

```bash
trk --version
```

If the shell cannot find `trk`, run `uv tool update-shell` and open a new terminal.

## Quickstart

This walk-through uses a scratch store, so it cannot touch your real one. The commands below are tested: a test runs every block marked `bash quickstart` on a fresh store.

First, point trk at a scratch store. `TRK_HOME` is the store's directory.

```bash quickstart
export TRK_HOME=$(mktemp -d)/store
trk init --me alice --display "Alice"
```

`alice` is you: the person. trk calls every person and every agent an **actor**. Register an agent actor too, called `worker`, so agents write under their own name and not under yours. Then list the actors and the roles they hold:

```bash quickstart
trk actor add worker --kind agent
trk actors
trk roles
```

A **role** is a name that asks are addressed to. At first, you hold every role.

Create a **program**: the top-level item, which states the outcome you want as checkable lines called criteria. Each command prints the new item's id, so the script keeps it.

```bash quickstart
PROGRAM=$(trk new program "Ship the widget" --criterion "The widget works end to end")
```

File a **ticket** under it. A ticket is one piece of work sized for one agent session. A self-contained ticket carries its own spec: what happens today, what should happen, and at least one criterion.

```bash quickstart
printf 'There is no widget button.\n' > current.md
printf 'The page has a widget button that opens the widget.\n' > desired.md
TICKET=$(trk new work-item "Add the widget button" --form self-contained --parent "$PROGRAM" \
  --section current-behaviour=@current.md --section desired-behaviour=@desired.md \
  --criterion "The button opens the widget")
```

Look at the **frontier**: the list of items you can start now.

```bash quickstart
trk ready --root "$PROGRAM"
trk tree
```

An agent takes the ticket with a claim. A claim needs a session id. Claude Code's hooks set one for you; here we make one up.

```bash quickstart
SESSION=11111111-1111-4111-8111-111111111111
trk claim "$TICKET" --as worker --session "$SESSION"
```

Now move the ticket through its gates. The stages are `framed`, `specd`, `built`, `merged` and `verified`. The first step is a judgment that the ticket is complete on its own. It must come from a session that wrote none of the ticket: here alice wrote it from her terminal, so the worker may judge it.

```bash quickstart
trk advance "$TICKET" --judgment "The ticket states current and desired behaviour." --as worker --session "$SESSION"
```

To leave `specd`, the ticket needs evidence that the work is built: a pull request row and a passing test run, both tied to a commit. This ticket changes no repo, so the pull request is `none` and the commit is a placeholder of zeros. On a real ticket, use `--ref <pull request URL or branch>` and `--sha "$(git rev-parse HEAD)"`.

```bash quickstart
SHA=0000000000000000000000000000000000000000
trk ledger add "$TICKET" --kind fork --decided "Use a plain button" --alternative "A link" --why "It is simpler" --as worker
trk evidence add "$TICKET" --kind pr --gate built --ref none --sha $SHA --note "No repo change" --as worker
trk evidence add "$TICKET" --kind test-run --gate built --ref "manual check" --sha $SHA --verdict pass --note "Checked by hand" --as worker
trk advance "$TICKET" --as worker
```

A gate that is not met refuses and lists what is missing. Try `trk advance` on a ticket that lacks evidence to see it. The ledger row above is a **fork**: a choice that matters, recorded so a reviewer can check it.

A ticket that changes no repo skips the `merged` gate. Then it needs one passing check per criterion, from someone who looked. A person records that with `human-accept`.

```bash quickstart
trk skip "$TICKET" merged --reason "Changes no repo" --as worker
trk evidence add "$TICKET" --kind human-accept --gate verified --criterion ac-1 --ref "Alice tried it" --sha $SHA --verdict pass --note "The button opens the widget" --as alice
trk advance "$TICKET" --outcome "The widget button opens the widget." --as worker
```

The ticket is now closed `done`. See where things stand:

```bash quickstart
trk show "$TICKET"
trk criteria "$PROGRAM"
```

Every command takes `--json` for machine-readable output. Agents read JSON; people read the plain text.

Real tickets also pass a `merged` gate. It needs a `merge` evidence row and a `trk blast-radius` rating of the change: GREEN merges as is, YELLOW needs an independent review, RED needs a person's approval. [docs/agents.md](docs/agents.md) walks through the whole flow.

## Concepts

The full vocabulary is in [CONTEXT.md](CONTEXT.md). These are the terms you need first.

| Term | Meaning |
|---|---|
| **Item** | Anything trk tracks. Every item has one parent at most. |
| **Program, project, work item** | The three kinds, from largest to smallest. A program states an outcome. A project has a spec. A work item is one piece of work. |
| **Ticket** | A work item an agent can build. It is either *spec-bound* (a slice of a project's spec) or *self-contained* (its own spec). |
| **Decision** | A work item that answers a question instead of building something. |
| **Ask** | A question to a role. While it is open, it blocks its parent. |
| **Actor** | A registered person or agent. Every write names one with `--as`. |
| **Role** | A name an ask is addressed to, such as `engineering` or `product`. One actor holds each role. |
| **Frontier** | The items you can start now, from `trk ready`. |
| **Claim** | A lease an agent session takes on an item. It lapses after 60 minutes unless renewed. |
| **Gate** | A check a ticket must pass to reach the next stage. It reads the evidence on record. |
| **Evidence** | A row that is never edited: a pull request, a test run, a review, a judgment. A wrong row is withdrawn with a retraction row. |
| **Ledger** | A ticket's record of choices (forks), plus the verdicts and settlements of reviewers. |
| **Handoff** | A checkpoint note a session leaves so a later session can restart the work. |

Blocking is an edge between two items, never text. `trk link A blocks B` means B waits on A.

## Using trk with Claude Code

trk works without Claude Code, but it is built for it. The package adds three parts.

**Skills.** A skill is a folder of instructions that Claude Code loads on demand. The installer copies three into `~/.claude/skills`:

- `trk` teaches an agent the rules: which flags to pass, how gates and asks work, how to write an ask a person can answer fast. Every agent that touches trk should load it. It has a recipe book in `references/cookbook.md`.
- `trk-drive-program` teaches one session to drive a whole program: pick the next work, hand it to worker sessions, check what comes back.
- `trk-create-program` teaches a session to chart a new program with you, one question at a time.

The files in `skills/` are the source of truth. Edit them there, then run `./install.sh` again to copy them. If you edited an installed copy, the installer refuses to overwrite it unless you pass `--force`; then it keeps your copy aside as `<name>.bak-<time>`.

**Hooks.** A hook is a script Claude Code runs on an event. Two scripts in `hooks/` serve trk:

- `trk-session-start.sh` runs when a session starts. It exports the session id as `$TRK_SESSION`, so `trk claim` knows which session holds a claim.
- `trk-renew.sh` runs on every prompt and after every tool call. It renews the session's claims. It costs no model tokens and does nothing unless a lease is past half its life.

Without the hooks, pass `--session <uuid>` yourself and run `trk claim renew --session <uuid>` often. A claim that lapses is gone. A `/clear` makes a new session id, so claim again after one.

**Sessions.** A claim belongs to a Claude Code session. A subagent shares its parent's session, so it can never be the independent reviewer of its parent's work. A review that must be independent needs a separate session. The gates enforce this.

## Using trk with herdr (optional)

herdr is a terminal multiplexer for coding agents. A herdr plugin can read trk's change feed, open a pane for each claimed item, and route asks to the right session. trk never calls herdr and stores no herdr ids. The plugin only reads trk's JSON, through `trk changes --since <seq> --json`. The contract is in [docs/adr/0002-herdr-contract.md](docs/adr/0002-herdr-contract.md).

You do not need herdr. Anything that can run `trk` and read JSON can drive the same flow. Parts of the skills mention herdr as one example of how to start and message worker sessions. Treat those parts as optional.

## Publishing pages (optional)

trk can publish each item as a web page, with a table of its children, its open asks and its evidence. Without publishing, trk works fully and shows "not set up" when you ask about pages. `trk pages render <id>` prints any page's HTML whether publishing is set up or not.

Pages go through a page adapter, a program trk runs for each call ([docs/adapters.md](docs/adapters.md)):

- `trk-pages-folder`, installed with trk, writes each page as an HTML file in a directory:
  ```bash
  trk pages setup --adapter "trk-pages-folder --dir ~/trk-pages" --home <id of an open item>
  ```
  Add `--base-url https://…` inside the quotes if a web server serves that directory, so the pages link to each other there.
- An adapter for a hosted service, where people comment on a page and a comment on an ask becomes a reply to it, lives in its own repository, in any language. It keeps its own credentials: trk never reads, stores or prints a secret. `python -m trk.conformance -- <command>` checks an adapter against the protocol.

`--home` names the item that the publisher's own asks sit under. Look at `trk pages setup --help` for the details. Then start the timer, which runs `trk pages tick` about every minute: `./install.sh --pages` (or `trk pages timer install`).

[docs/operations.md](docs/operations.md) covers the timer, the adapter's credentials expiring and what to do when a page fails.

Pages are visible to everyone the publisher shares them with. Keep secrets and customer data out of titles, sections and handoffs.

## The store is machine-wide

trk keeps one store for the whole machine, in `~/.local/share/trk/` (or in `$TRK_HOME` if you set it). Every project, repo and Claude Code session on the machine shares it. That is deliberate: one frontier, one set of claims, and one place for a person to look.

Three things follow:

- Items are not tied to a repo. A ticket names its repo with `--repo`, and gates read git history there.
- A second user on the same machine gets a second store, because the path is under their home directory.
- Do not copy store files by hand. Use `trk backup` and `trk restore`.

## Upgrading

If you installed the default way (editable), update the checkout:

```bash
cd trk
git pull          # or: git fetch from your bundle or ssh host, then merge
./install.sh      # refreshes the skills and hooks
```

The `trk` command runs the checkout's code, so it updates at once. A new version migrates the store's schema the first time it runs. Keep the checkout on a released branch, because an unfinished branch could migrate your store to a schema that other builds cannot read.

If you installed with `--no-editable`, run `./install.sh --force --no-editable` to reinstall.

## Backup and restore

```bash
trk backup ~/trk-backup-2026-10-18.db     # a consistent copy; never overwrites a file
trk restore ~/trk-backup-2026-10-18.db    # replaces the store with the copy
```

Restore rewinds the store's history, so it also gives the store a new epoch, an id that tells programs reading the change feed to start over. Do not copy `store.db` by hand. See [docs/operations.md](docs/operations.md).

## Uninstall

```bash
./install.sh --uninstall --dry-run   # look first
./install.sh --uninstall
```

This removes the `trk` command (only if the installer put it there), the three skills (unless you edited them), the three hook entries and the timer. It never deletes the store. To delete the store as well, remove `~/.local/share/trk` yourself, after you decide you no longer need the history.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `trk: command not found` | The uv tool directory is not on your PATH. Run `uv tool update-shell`, then open a new terminal. |
| The installer says python 3.12 is required | no `python3`, `python3.12`, `python3.13` or `python3.14` on PATH is 3.12 or newer (macOS's own `python3` is 3.9). Install Python 3.12 or newer, for example with Homebrew, and run the installer again. |
| `claim` says it needs a session | The hooks are not active. Restart Claude Code after the install, or pass `--session <uuid>`. |
| An agent's claim vanished | The lease lapsed after 60 minutes, or `/clear` made a new session. Claim again. |
| A write was recorded under your name | The agent forgot `--as <actor>`. The store's default actor is the person who ran `trk init`. Every agent write needs `--as`. |
| `foreign-build` | A second checkout or a worktree tried to change your real store. Run the installed `trk`, or set `TRK_HOME` to a scratch directory. |
| `schema-newer` | The store was migrated by a newer trk than this one. Update this checkout. |
| `gate-unmet` | A gate lacks evidence. The message lists what is missing. Record it and try again. |
| Pages say "not set up" | Publishing is off until `trk pages setup` has run. |

Exit codes: 0 done, 1 refused by a rule, 2 usage error, 3 not found, 4 conflict. With `--json`, a failure prints a JSON error on stderr with `code`, `rule` and `message`.

## Repo layout

| Path | Contents |
|---|---|
| `src/trk/` | The program. |
| `skills/` | The three Claude Code skills (source of truth). |
| `hooks/` | The session hook scripts and the settings snippet. |
| `systemd/` | Templates for the page publisher's timer. |
| `install.sh`, `scripts/` | The installer and its helpers. |
| `docs/` | This documentation, the specs and the decision records. |
| `contract/` | JSON schemas for the change feed. |
| `tests/` | The test suite. Run it with `pytest`. |
| `CONTEXT.md`, `AGENTS.md` | The vocabulary, and the rules for agents working on trk itself. |

## More documentation

- [docs/install.md](docs/install.md): every installer step and option.
- [docs/operations.md](docs/operations.md): pages, the timer, backup, restore and the epoch.
- [docs/agents.md](docs/agents.md): how several sessions work together through trk.
- [docs/cli.md](docs/cli.md): every command and flag, generated from `--help`.
- [CONTEXT.md](CONTEXT.md): the vocabulary.
- [docs/specs/](docs/specs/) and [docs/adr/](docs/adr/): the specs and the decisions behind trk.
