# Operating trk

This page covers the jobs that come after install: publishing pages, the timer, backup and restore, and the change feed's epoch.

## Where things live

| What | Where |
|---|---|
| The store | `$TRK_HOME`, default `~/.local/share/trk/`. It holds `store.db` (the data) and `feed.head` (a hint file for programs that watch changes). |
| The `trk` command | uv's tool directory, usually `~/.local/bin/trk`. |
| Skills | `~/.claude/skills/{trk,trk-drive-program,trk-create-program}`. |
| Hooks | Entries in `~/.claude/settings.json`. |
| Timer units | `~/.config/systemd/user/trk-pages.{service,timer}`. |

The store is machine-wide. Every project and session on the machine shares it. Never copy, edit or delete `store.db` by hand while trk is in use. Use `trk backup` and `trk restore`.

## Publishing setup

Publishing is optional. trk writes pages through a **page backend**, chosen at setup:

- **`folder`** (built in) writes each page as an HTML file in a directory. It needs no token, and its pages take no comments, so asks are answered with `trk answer` only.
- **An installed backend** publishes to a hosted service, where people can comment and a comment on an ask becomes a reply to it. A backend is a Python package that registers itself under the `trk.page_backends` entry-point group; `src/trk/publisher.py` states the interface it implements. A hosted backend usually needs a token.

### The folder backend

```bash
trk pages setup --backend folder --dir ~/trk-pages --home <open item id>
```

- `--dir` is where the pages go. Setup creates it.
- `--base-url` (optional) is where that directory is served, for example `https://intranet.example/trk`. Pages then link to each other by that URL. Without it they link by `file://` path, which works when you open them on this machine.
- `--home` is an open item. The publisher puts its own asks under it.

### A backend with a token

1. **Make a token** in the service, with the permissions its backend names. Write it to a file that only you can read:
   ```bash
   install -m 600 /dev/null ~/path/to/token
   $EDITOR ~/path/to/token
   ```
   trk refuses a token file with looser permissions (rule `token-file-mode`). Never put the token in a ticket, a handoff, a chat or a command line.
2. **Run setup:**
   ```bash
   trk pages setup --backend <name> --base-url https://your-service.example --token-file ~/path/to/token \
     --expires 2027-01-31 --home <open item id> --team <team slug>
   ```
   - `--base-url` must be `https`, or `http` to localhost only, because the token goes with every call.
   - `--expires` is the token's last day. The publisher uses it to warn you.
   - `--home` is an open item. The publisher puts its own asks, such as "renew the token", under it.
   - `--team` is the team every page is shared with.
   - Setup checks the token with one harmless call. It stores the backend's name, the service address, the token file's path, the team, the expiry date and the home item. It never stores or prints the token.
Then **start the timer** (next section).

Until setup has run, trk publishes nothing, `trk pages` says "not set up", and `trk pages flush`, `pull` and `tick` exit 1.

Every page is shared with the whole team from the moment it is created. Keep customer names, models and data, and any secret, out of titles, sections and handoffs.

### How publishing works

Each item gets one page. When a write changes an item, trk marks the affected pages dirty and starts a short-lived background process, the flusher. After a quiet window of about 60 seconds per page, the flusher sends only what changed, then exits. People comment on a page. A comment on an ask's box is recorded as a reply to that ask. The person who asked then confirms the reply as the answer with `trk answer --from-comment`. A reply made with `trk reply` has no page, and is confirmed the same way with its `reply-<n>` id.

Useful commands:

| Command | What it does |
|---|---|
| `trk pages` | Lists pages. `--dirty` shows those waiting to be sent, `--errors` those that failed. |
| `trk pages flush --now` | Sends every dirty page now. |
| `trk pages pull` | Reads new comments, sends the replies, and closes the threads of asks that closed since. |
| `trk pages tidy <page> [--dry-run]` | Resolves the threads of asks that are already closed, on one page (an item id, or `needs-you:<actor>`). Run it with `--dry-run` first. |
| `trk pages tick` | Pull, then flush. This is what the timer runs. |
| `trk pages render <id> --out page.html` | Renders one page locally, with no network. Good for checking a page. |
| `trk pages republish` | Sends a page again in full. |

### The timer

The timer is a systemd user timer that runs `trk pages tick` about once a minute. It is the backstop for a flusher that died, and it runs the comment pull. Install it either way:

```bash
./install.sh --pages          # from the checkout, renders systemd/ templates
trk pages timer install       # the same units, written by trk itself
trk pages timer status
trk pages timer remove
```

Both ways write `trk-pages.service` and `trk-pages.timer` to `~/.config/systemd/user` and enable the timer. The service runs the installed `trk` by its absolute path, and sets `TRK_HOME` if it was set at install. If you move `trk` or change `TRK_HOME`, install again; it rewrites the units.

For the timer to keep running when you are logged out, run `loginctl enable-linger $USER` once. Check the log with `journalctl --user -u trk-pages.service`.

### Token expiry

This applies to a backend with a token. Every flush and pull checks the expiry date. Fourteen days before it, or on any "unauthorized" answer from the backend, the publisher opens an ask titled "Mint a new tracker token" under the home item. It is addressed to the `publisher` role. To renew, make a new token, write it to the token file, and run `trk pages setup` again with the new `--expires` date. A successful setup with a date more than 14 days away answers the ask and lets held pages go out.

While the backend rejects the token, pages stay dirty and the store is unaffected. Nothing is lost.

### When a page fails

`trk pages --errors` lists failed pages and the last error. Fix the cause (a missing grant, a network problem), then run `trk pages flush --now`. A reply that failed for good is listed too. Settle it with `trk pages reply resend <id>` or `trk pages reply dismiss <id> --reason "<why>"`.

## Backup and restore

```bash
trk backup /path/to/trk-backup.db
trk restore /path/to/trk-backup.db
```

- `trk backup` writes a consistent copy of the store, even while other sessions write. It never overwrites a file.
- `trk restore` replaces the store with a backup. It reads the backup and never changes it. Stop other trk work first, and restart any program that watches the change feed.
- Back up before you upgrade across a schema change, and on a schedule you trust. A cron line such as `0 3 * * * trk backup ~/backups/trk-$(date +\%F).db` works.

Do not restore by copying files. Only `trk restore` does the full job.

### The epoch

The change feed (`trk changes --since <n>`) numbers every change. A program that watches it, such as a herdr plugin or the publisher, remembers the last number it saw. A restore rewinds the history, so numbers get reused. To warn watchers, the store has an **epoch**: an opaque id set by `trk init`. `trk restore` replaces it with a new one. A watcher that sees a different epoch, or a position past the feed's end, starts again from the beginning. You do not manage the epoch. You only need to know that a restore makes watchers re-read history once.

## Upgrading

Update the checkout, run `./install.sh`, and restart Claude Code. See the [README](../README.md#upgrading). A new trk migrates the store the first time it runs. After that, an older trk refuses the store with `schema-newer`. So update every checkout on the machine together, and keep the checkout that `trk` runs from on a released branch.

If you work on trk itself, never run a build from a second checkout or worktree against the real store. trk refuses (`foreign-build`). Set `TRK_HOME` to a scratch directory and `TRK_TEST=1` for such runs. `TRK_TEST=1` also makes trk refuse the default store and the default systemd directory, so a test cannot touch the real ones.

## Leases and sweeps

A claim is a lease of 60 minutes (`trk config` shows `lease-minutes`). The hooks renew it. Every trk command first ends lapsed leases. `trk sweep` does that on its own. `trk claims` lists live claims.

## Settings

`trk config` lists the store's settings, and `trk config set <key> <value>` changes one. The important ones are `default-actor` (who a write is recorded as when `--as` is missing), `shared-agent` and `lease-minutes`.
