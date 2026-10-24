# Installing trk

This page explains what `install.sh` does, one step at a time, and how to do each step by hand. For the short version, read [README.md](../README.md#install).

## Before you start

- **Python 3.12 or newer.** Check with `python3 --version`.
- **uv.** This is a Python tool installer. trk uses it to put the `trk` command on your PATH.
- **git.** You need it to copy the repo and to run most gates.
- **Claude Code** (optional). The skills and hooks are for it.

## Getting the code

The repo has no hosted home, so you copy it by hand. There are two ways.

1. **Clone from a path or an ssh host.** Run this on the new machine:
   ```bash
   git clone user@host:~/projects/beads-alternative trk
   ```
   For a path on the same machine, use `git clone /path/to/beads-alternative trk`.
2. **Send one file.** On the machine that has the repo:
   ```bash
   ./install.sh make-bundle trk.bundle
   ```
   This packs the default branch (`main` or `master`) and the tags, with their history. It does not pack worktree refs or other local branches, because they mean nothing on the other machine. Add `--all-branches` to pack every local branch too. It refuses to overwrite a file that exists. Copy it to the new machine, then run `git clone trk.bundle trk`.

To bring a later version over, make a new bundle and run `git pull ../trk.bundle` in the clone, or `git pull` again from the path or host. The clone's `origin` stays the path or bundle you cloned from.

## What install.sh does

```text
./install.sh [--dry-run] [--no-editable] [--force] [--pages] [--me NAME]
./install.sh --uninstall [--dry-run]
./install.sh make-bundle [--all-branches] [FILE]
```

`--dry-run` prints every action with a `[dry-run]` mark and changes nothing. Use it first.

### 1. Checks

The script stops with a clear message if `python3` is missing or older than 3.12, or if `uv` is missing.

### 2. The `trk` command

The script runs `uv tool install --editable <checkout>`. uv builds an isolated environment for trk and links a `trk` program into its tool directory (usually `~/.local/bin`).

- **Editable** means the command runs the code in your checkout. A `git pull` takes effect at once. This is the default.
- `--no-editable` installs a copy. Updating then needs `./install.sh --force --no-editable`.
- If uv already has trk, the script leaves it alone. `--force` reinstalls.
- When the script installs the command, it records that in `~/.local/state/trk-install/tool-installed`. `--uninstall` removes the command only if that file exists, so it never removes a trk you installed yourself.

If `trk` is not found afterwards, run `uv tool update-shell` and open a new terminal.

### 3. The store

If `$TRK_HOME` (default `~/.local/share/trk`) holds no `store.db`, the script runs `trk init --me <you>`. The `--me NAME` option sets the actor id for you. The default is your user name. The script never changes an existing store.

`trk init` registers you as the human actor, registers the agent actors `herdr` and `publisher`, and gives you the roles `engineering`, `product` and `publisher`. It does not register a `worker` actor. Add one yourself (see the next steps the script prints).

### 4. The skills

The script copies `skills/trk`, `skills/trk-drive-program` and `skills/trk-create-program` into `~/.claude/skills`.

- A skill that is already there and identical is skipped.
- A skill that is there but different makes the script stop before it changes anything. It prints which files differ. Move your edits into `skills/` in the repo, or run again with `--force`, which keeps the old copy as `<name>.bak-<time>` and replaces it. `--dry-run` reports which skills it would replace. The repo copy is the source of truth, so edit skills in `skills/`.

### 5. The hooks

The script adds these entries to `~/.claude/settings.json`, using the absolute path of your checkout:

| Event | Script | Purpose |
|---|---|---|
| `SessionStart` | `hooks/trk-session-start.sh` | Exports the session id as `$TRK_SESSION`. |
| `UserPromptSubmit` | `hooks/trk-renew.sh` | Renews the session's claims. |
| `PostToolUse` | `hooks/trk-renew.sh` | Renews the session's claims. |

The merge keeps every other setting and every other hook. It copies the old file to `settings.json.trk-bak-<time>`, prints the diff, then writes. If an entry with the same command is already there, it adds nothing, so a second run changes nothing. If `settings.json` is not valid JSON, the script stops and changes nothing.

Because the entries hold the checkout's absolute path, moving the checkout breaks them. Run `./install.sh` again from the new place. It adds the new entries. Remove the old ones by hand, or run `./install.sh --uninstall` from the old place first.

Restart Claude Code after the merge. It reads settings when a session starts.

### 6. The page publisher timer (`--pages`)

This step is off unless you pass `--pages`. It writes `trk-pages.service` and `trk-pages.timer` into `~/.config/systemd/user` from the templates in `systemd/`, then runs `systemctl --user daemon-reload` and `systemctl --user enable --now trk-pages.timer`. The service runs the installed `trk pages tick`. If `$TRK_HOME` is set, the service sets it too. Read [operations.md](operations.md) before you turn this on, because publishing also needs `trk pages setup`.

`trk pages timer install` does the same job without the script.

### 7. Next steps

The script prints what to do next: check actors and roles, restart Claude Code, set up publishing if you want it, and try the quickstart.

## The token

The installer never reads, prints or stores a token. A page backend that publishes to a hosted service usually needs one, and you give it to trk in a separate step with `trk pages setup --token-file <path>`. That command checks the token and stores only the path. See [operations.md](operations.md#publishing-setup).

## Doing it by hand

```bash
uv tool install --editable .         # the command
trk init --me <you>                  # the store, once
cp -R skills/* ~/.claude/skills/     # the skills
```

Then add the entries from `hooks/settings-snippet.json` to `~/.claude/settings.json`. Replace `<TRK_REPO>` in the snippet with the absolute path of your checkout.

## Uninstalling

`./install.sh --uninstall` removes the timer units, the skills that still match the repo copy, the hook entries it added and the `trk` command, if this script installed it. It leaves the store. It leaves an edited skill alone and says so. Use `--dry-run` to look first.

## Testing the installer

`tests/test_install.py` runs the script in a temporary HOME with a temporary store and a fake `uv` and `systemctl`. It never touches the real ones. Do the same if you change the script: never run `install.sh` against a HOME you care about until a `--dry-run` looks right.
