#!/usr/bin/env bash
# Install trk from this checkout, or remove it again. Safe to re-run.
#
#   ./install.sh [--dry-run] [--no-editable] [--force] [--pages] [--me NAME]
#   ./install.sh --uninstall [--dry-run]
#   ./install.sh make-bundle [--all-branches] [FILE]
#
# See `./install.sh --help`, README.md and docs/install.md. This script never reads, prints or
# stores a token: publishing is set up afterwards with `trk pages setup`, which reads a token
# file (mode 0600) that you name with --token-file.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
SKILLS=(trk trk-drive-program trk-create-program)

DRY=0 ALL_BRANCHES=0 UNINSTALL=0 EDITABLE=1 FORCE=0 PAGES=0 ME=${USER:-$(id -un)} MODE=install BUNDLE=

usage() {
  cat <<'EOF'
Usage: install.sh [options]
       install.sh make-bundle [--all-branches] [FILE]

Installs trk from this checkout: the `trk` command (with uv), a store if none exists, the three
Claude Code skills, and the session hooks in ~/.claude/settings.json.

Options:
  --dry-run       print what would happen and change nothing
  --uninstall     remove what install.sh added; the store is never touched
  --no-editable   install a copy of trk, not a link to this checkout
  --force         reinstall the trk command even if uv already has it, and replace skills that
                  differ from the repo copy (the old ones are kept as <name>.bak-<time>)
  --pages         also install and enable the page publisher's systemd user timer
  --me NAME       the person's actor id for a new store (default: your user name)
  -h, --help      show this help

make-bundle [--all-branches] [FILE]
                    write the default branch (main or master) and the tags, with their history, to
                    one file (default ./trk.bundle). --all-branches packs every local branch too.
                    Worktree and remote refs are never packed.
                    Copy it to another machine and run `git clone trk.bundle trk`.

Environment: TRK_HOME picks the store (default ~/.local/share/trk). HOME and XDG_CONFIG_HOME
decide where skills, settings and units go.
EOF
}

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
# Print a command in dry-run mode, run it otherwise.
act() {
  if ((DRY)); then say "[dry-run] $*"; else "$@"; fi
}

while (($#)); do
  case $1 in
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --no-editable) EDITABLE=0 ;;
    --force) FORCE=1 ;;
    --pages) PAGES=1 ;;
    --me) shift; [[ $# -gt 0 ]] || die "--me needs a name"; ME=$1 ;;
    make-bundle) MODE=bundle; if [[ ${2:-} != "" && ${2:-} != -* ]]; then BUNDLE=$2; shift; fi ;;
    --all-branches) ALL_BRANCHES=1 ;;
    *) if [[ $MODE == bundle && -z $BUNDLE && $1 != -* ]]; then BUNDLE=$1; else usage >&2; die "unknown option: $1"; fi ;;
    -h | --help) usage; exit 0 ;;
  esac
  shift
done
[[ $MODE == bundle ]] || ((! ALL_BRANCHES)) || die "--all-branches goes with make-bundle"

CLAUDE_DIR=$HOME/.claude
SETTINGS=$CLAUDE_DIR/settings.json
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
STORE=${TRK_HOME:-$HOME/.local/share/trk}
# Written only when this script installs the uv tool, so --uninstall removes only what it added.
MARKER=${XDG_STATE_HOME:-$HOME/.local/state}/trk-install/tool-installed

default_branch() { # origin's default branch, else main, else master
  local b
  b=$(git -C "$REPO" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null) && b=${b#origin/}
  for b in ${b:-} main master; do
    git -C "$REPO" show-ref --verify --quiet "refs/heads/$b" && { say "$b"; return; }
  done
  die "no main or master branch to bundle; use --all-branches"
}

make_bundle() {
  local out=${BUNDLE:-trk.bundle}
  command -v git >/dev/null || die "git is required for make-bundle"
  [[ -e $out ]] && die "$out already exists; remove it or pick another name"
  local refs=()
  if ((ALL_BRANCHES)); then
    refs=(--branches --tags)
  else
    refs=("$(default_branch)" --tags)
  fi
  act git -C "$REPO" bundle create "$(cd "$(dirname "$out")" && pwd -P)/$(basename "$out")" "${refs[@]}"
  ((DRY)) || say "On the other machine: git clone $out trk && cd trk && ./install.sh"
}

skill_state() { # same | differs | absent for one skill
  local dest=$CLAUDE_DIR/skills/$1
  if [[ ! -e $dest && ! -L $dest ]]; then say absent
  elif diff -r "$REPO/skills/$1" "$dest" >/dev/null 2>&1; then say same
  else say differs; fi
}

check_skills() { # refuse to overwrite a skill that differs from the repo copy, unless --force
  local s differing=()
  for s in "${SKILLS[@]}"; do [[ $(skill_state "$s") == differs ]] && differing+=("$s"); done
  ((${#differing[@]})) || return 0
  say "== skills that differ from the repo copy"
  for s in "${differing[@]}"; do
    say "skill $s:"
    diff -rq "$REPO/skills/$s" "$CLAUDE_DIR/skills/$s" | sed 's/^/  /' || true
  done
  if ((FORCE)); then
    say "--force given: these will be replaced; the old copies are kept as <name>.bak-<time>"
  elif ((DRY)); then
    say "[dry-run] a real run would refuse here; add --force to replace them"
  else
    die "the skills above differ from the repo copy (local edits, or an older install). Nothing was changed. Move your edits into skills/ in the repo, or run again with --force to replace them."
  fi
}

install_skills() {
  say "== skills"
  local s state stamp
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  for s in "${SKILLS[@]}"; do
    state=$(skill_state "$s")
    case $state in
      same) say "skill $s: already installed" ;;
      absent) say "skill $s: installing"
        act mkdir -p "$CLAUDE_DIR/skills"
        act cp -R "$REPO/skills/$s" "$CLAUDE_DIR/skills/$s" ;;
      differs) say "skill $s: replacing; keeping the old one as $s.bak-$stamp"
        act mv "$CLAUDE_DIR/skills/$s" "$CLAUDE_DIR/skills/$s.bak-$stamp"
        act cp -R "$REPO/skills/$s" "$CLAUDE_DIR/skills/$s" ;;
    esac
  done
}

uninstall_skills() {
  say "== skills"
  local s state
  for s in "${SKILLS[@]}"; do
    state=$(skill_state "$s")
    case $state in
      same) say "skill $s: removing"; act rm -rf "$CLAUDE_DIR/skills/$s" ;;
      absent) say "skill $s: not installed" ;;
      differs) say "skill $s: edited since install; leaving it alone" ;;
    esac
  done
}

settings_cmd() { # add|remove
  command -v python3 >/dev/null || die "python3 is required"
  local flags=()
  ((DRY)) && flags+=(--dry-run)
  python3 "$REPO/scripts/merge_settings.py" "$1" --settings "$SETTINGS" --repo "$REPO" "${flags[@]}"
}

render_units() { # prints the service unit with the installed trk path
  local trk=$1 env=
  [[ $trk =~ [[:space:]\"\'\\%\$] ]] && die "the trk path '$trk' has characters systemd would need quoted; install trk somewhere simpler"
  [[ -n ${TRK_HOME:-} ]] && env="Environment=TRK_HOME=$(cd "$TRK_HOME" && pwd -P)"$'\n'
  local tpl
  tpl=$(cat "$REPO/systemd/trk-pages.service.in"; printf x)
  tpl=${tpl%x}
  tpl=${tpl//@ENVIRONMENT@/$env}
  printf '%s' "${tpl//@TRK@/$trk}"
}

install_units() {
  say "== page publisher timer"
  local trk
  trk=$(command -v trk 2>/dev/null || true)
  [[ -z $trk ]] && trk=$(uv tool dir --bin 2>/dev/null)/trk
  if ((DRY)); then
    say "[dry-run] write $UNIT_DIR/trk-pages.service (ExecStart=$trk pages tick) and trk-pages.timer"
    say "[dry-run] systemctl --user daemon-reload && systemctl --user enable --now trk-pages.timer"
    return
  fi
  command -v systemctl >/dev/null || die "--pages needs systemctl (systemd)"
  mkdir -p "$UNIT_DIR"
  render_units "$trk" >"$UNIT_DIR/trk-pages.service"
  cp "$REPO/systemd/trk-pages.timer" "$UNIT_DIR/trk-pages.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now trk-pages.timer
  say "timer enabled. Publishing still needs 'trk pages setup' (see docs/operations.md)."
}

uninstall_units() {
  say "== page publisher timer"
  if [[ -e $UNIT_DIR/trk-pages.timer || -e $UNIT_DIR/trk-pages.service ]]; then
    if ((DRY)); then say "[dry-run] disable the timer and delete the units in $UNIT_DIR"
    else
      command -v systemctl >/dev/null && systemctl --user disable --now trk-pages.timer 2>/dev/null || true
      rm -f "$UNIT_DIR/trk-pages.timer" "$UNIT_DIR/trk-pages.service"
      command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
      say "units removed"
    fi
  else
    say "no units installed"
  fi
}

check_prereqs() {
  say "== checks"
  command -v python3 >/dev/null || die "python 3.12 or newer is required; python3 is not on PATH"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' ||
    die "python 3.12 or newer is required; found $(python3 --version 2>&1)"
  command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/); it is not on PATH"
  say "python3 and uv found"
}

tool_installed() { uv tool list 2>/dev/null | grep -q '^trk '; }

install_tool() {
  say "== trk command"
  local args=(tool install)
  ((EDITABLE)) && args+=(--editable)
  ((FORCE)) && args+=(--force)
  if tool_installed && ! ((FORCE)); then
    say "trk is already installed (use --force to reinstall)"
  else
    act uv "${args[@]}" "$REPO"
    if ((DRY)); then say "[dry-run] record that this script installed trk"
    else mkdir -p "$(dirname "$MARKER")" && printf '%s\n' "$REPO" >"$MARKER"; fi
  fi
}

install_store() {
  say "== store"
  if [[ -e $STORE/store.db ]]; then
    say "store exists at $STORE; leaving it alone"
  else
    say "creating a store at $STORE for actor '$ME'"
    if ((DRY)); then say "[dry-run] trk init --me $ME"
    else trk init --me "$ME" || die "trk init failed; is the uv tool bin directory on PATH? ($(uv tool dir --bin))"; fi
  fi
}

next_steps() {
  cat <<EOF

== next steps
1. Make sure \`trk\` is on your PATH: run \`trk --version\`. If it is not found, run \`uv tool update-shell\`.
2. Register the agent that will work for you, so its writes are not recorded as yours:
     trk actor add worker --kind agent
   Check actors and roles with `trk actors` and `trk roles`; reassign a role with
   `trk roles set <role> <actor>`.
   Then read docs/agents.md.
3. Restart Claude Code so it loads the new skills and hooks.
4. Optional, publishing pages: run \`trk pages setup --help\`. It reads a token file (mode 0600) named with --token-file; this script never sees it.
   Then run ./install.sh --pages for the timer. See docs/operations.md.
5. Try the quickstart in README.md.
EOF
}

if [[ $MODE == bundle ]]; then make_bundle; exit 0; fi

if ((UNINSTALL)); then
  say "Uninstalling trk. The store at $STORE stays."
  uninstall_units
  uninstall_skills
  say "== hooks"
  settings_cmd remove
  say "== trk command"
  if [[ ! -e $MARKER ]]; then
    say "trk was not installed by this script; leaving the command alone (remove it with: uv tool uninstall trk)"
  elif command -v uv >/dev/null && tool_installed; then
    act uv tool uninstall trk
    act rm -f "$MARKER"
  else
    say "trk is no longer installed with uv"
    act rm -f "$MARKER"
  fi
  exit 0
fi

check_prereqs
check_skills
install_tool
if ((DRY)) && ! command -v trk >/dev/null; then say "== store"; say "[dry-run] would run trk init if no store exists at $STORE"; else install_store; fi
install_skills
say "== hooks"
settings_cmd add
((PAGES)) && install_units
((DRY)) || next_steps
