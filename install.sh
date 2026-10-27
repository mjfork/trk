#!/usr/bin/env bash
# Install trk from this checkout, or remove it again. Safe to re-run.
#
#   ./install.sh [--dry-run] [--no-editable] [--force] [--pages] [--me NAME]
#   ./install.sh --uninstall [--dry-run]
#   ./install.sh make-bundle [--all-branches] [FILE]
#
# See `./install.sh --help`, README.md and docs/install.md. This script never reads, prints or
# stores a token: publishing is set up afterwards with `trk pages setup --adapter COMMAND`, and an
# adapter for a hosted service keeps its own credentials (docs/adapters.md).
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
                  differ from the repo copy (the old ones are kept as ~/.local/share/trk-skill-backups/)
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
# Replaced skills are kept outside every folder Claude Code loads skills from, so a backup never loads as a skill.
SKILL_BACKUPS=${XDG_DATA_HOME:-$HOME/.local/share}/trk-skill-backups
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
  local refs=() branch head
  branch=$(default_branch)
  if ((ALL_BRANCHES)); then
    refs=(--branches --tags)
  else
    refs=("$branch" --tags)
  fi
  # HEAD tells a clone what to check out (git 2.48+ checks out nothing without it); only when it is that branch
  head=$(git -C "$REPO" symbolic-ref --quiet --short HEAD 2>/dev/null) || head=
  [[ $head == "$branch" ]] && refs=(HEAD ${refs[@]+"${refs[@]}"})
  act git -C "$REPO" bundle create "$(cd "$(dirname "$out")" && pwd -P)/$(basename "$out")" ${refs[@]+"${refs[@]}"}
  ((DRY)) || say "On the other machine: git clone -b $branch $out trk && cd trk && ./install.sh"
}

skill_state() { # same | differs | absent for one skill
  local dest=$CLAUDE_DIR/skills/$1
  if [[ ! -e $dest && ! -L $dest ]]; then say absent
  elif diff -r "$REPO/skills/$1" "$dest" >/dev/null 2>&1; then say same
  else say differs; fi
}

check_skills() { # refuse to overwrite a skill that differs from the repo copy, unless --force
  local s differing=()
  for s in ${SKILLS[@]+"${SKILLS[@]}"}; do [[ $(skill_state "$s") == differs ]] && differing+=("$s"); done
  ((${#differing[@]})) || return 0
  say "== skills that differ from the repo copy"
  for s in ${differing[@]+"${differing[@]}"}; do
    say "skill $s:"
    diff -rq "$REPO/skills/$s" "$CLAUDE_DIR/skills/$s" | sed 's/^/  /' || true
  done
  if ((FORCE)); then
    say "--force given: these will be replaced; the old copies are kept in $SKILL_BACKUPS"
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
  for s in ${SKILLS[@]+"${SKILLS[@]}"}; do
    state=$(skill_state "$s")
    case $state in
      same) say "skill $s: already installed" ;;
      absent) say "skill $s: installing"
        act mkdir -p "$CLAUDE_DIR/skills"
        act cp -R "$REPO/skills/$s" "$CLAUDE_DIR/skills/$s" ;;
      differs) say "skill $s: replacing; keeping the old one as $SKILL_BACKUPS/$s.$stamp"
        act mkdir -p "$SKILL_BACKUPS"
        act mv "$CLAUDE_DIR/skills/$s" "$SKILL_BACKUPS/$s.$stamp"
        act cp -R "$REPO/skills/$s" "$CLAUDE_DIR/skills/$s" ;;
    esac
  done
}

report_guidance() { # read-only: a site's own rules for each skill, which install never writes
  local s f
  f=${TRK_GUIDANCE_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/trk/guidance}
  for s in ${SKILLS[@]+"${SKILLS[@]}"}; do
    [[ -f $f/$s.md ]] && say "guidance: $s present ($(wc -c <"$f/$s.md" | tr -d ' ') B, $f/$s.md)"
  done
  return 0
}

uninstall_skills() {
  say "== skills"
  local s state
  for s in ${SKILLS[@]+"${SKILLS[@]}"}; do
    state=$(skill_state "$s")
    case $state in
      same) say "skill $s: removing"; act rm -rf "$CLAUDE_DIR/skills/$s" ;;
      absent) say "skill $s: not installed" ;;
      differs) say "skill $s: edited since install; leaving it alone" ;;
    esac
  done
}

settings_cmd() { # add|remove
  [[ -n $PY ]] || find_python || die "python 3.12 or newer is required"
  local flags=()
  ((DRY)) && flags+=(--dry-run)
  "$PY" "$REPO/scripts/merge_settings.py" "$1" --settings "$SETTINGS" --repo "$REPO" ${flags[@]+"${flags[@]}"}
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
  if ((DRY)) && ! command -v systemctl >/dev/null && [[ $(uname -s) == Darwin ]]; then
    say "[dry-run] $trk pages timer install   (a launchd agent, ~/Library/LaunchAgents/com.trk.pages.plist)"
    return
  fi
  if ((DRY)); then
    say "[dry-run] write $UNIT_DIR/trk-pages.service (ExecStart=$trk pages tick) and trk-pages.timer"
    say "[dry-run] systemctl --user daemon-reload && systemctl --user enable --now trk-pages.timer"
    return
  fi
  if ! command -v systemctl >/dev/null; then
    [[ $(uname -s) == Darwin ]] || die "--pages needs systemctl (systemd), or launchd on macOS"
    "$trk" pages timer install          # macOS: trk writes and loads its own launchd agent
    say "timer enabled. Publishing still needs 'trk pages setup' (see docs/operations.md)."
    return
  fi
  mkdir -p "$UNIT_DIR"
  render_units "$trk" >"$UNIT_DIR/trk-pages.service"
  cp "$REPO/systemd/trk-pages.timer" "$UNIT_DIR/trk-pages.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now trk-pages.timer
  say "timer enabled. Publishing still needs 'trk pages setup' (see docs/operations.md)."
}

uninstall_units() {
  say "== page publisher timer"
  local agent=$HOME/Library/LaunchAgents/com.trk.pages.plist
  if [[ -e $agent ]]; then
    if ((DRY)); then say "[dry-run] unload and delete $agent"
    else
      launchctl bootout "gui/$(id -u)/com.trk.pages" 2>/dev/null || true
      rm -f "$agent"
      say "launchd agent removed"
    fi
  fi
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

# The first python 3.12+ on PATH: python3, else a versioned one (macOS puts its own 3.9 python3 first).
PY=
find_python() {
  local c
  for c in python3 python3.14 python3.13 python3.12; do
    command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null &&
      { PY=$c; return 0; }
  done
  return 1
}

check_prereqs() {
  say "== checks"
  find_python || die "python 3.12 or newer is required; found $(python3 --version 2>&1 || echo 'no python3 on PATH')"
  command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/); it is not on PATH"
  say "$PY and uv found"
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
    act uv ${args[@]+"${args[@]}"} "$REPO"
    if ((DRY)); then say "[dry-run] record that this script installed trk"
    else mkdir -p "$(dirname "$MARKER")" && printf '%s\n' "$REPO" >"$MARKER"; fi
  fi
}

# A store created before trk registered `worker` has no such actor, and every skill writes `--as worker`,
# so the first worker claim on it is refused `unknown-actor`. A new store needs nothing: `trk init`
# registers it. Guarded on absence, so a rerun writes nothing and an actor someone registered under the
# name is left exactly as it is.
top_up_worker() {
  trk actors 2>/dev/null | grep -q '^worker[[:space:]]' && return 0
  say "registering the 'worker' agent actor (this store predates it)"
  # As the store's default actor, never as `worker` itself: the row records who registered it.
  trk actor add worker --kind agent || say "could not register 'worker'; run: trk actor add worker --kind agent"
}

install_store() {
  say "== store"
  if [[ -e $STORE/store.db ]]; then
    say "store exists at $STORE; leaving it alone"
    if ((DRY)); then say "[dry-run] register the 'worker' actor if the store has none"
    else top_up_worker; fi
  else
    say "creating a store at $STORE for actor '$ME'"
    if ((DRY)); then say "[dry-run] trk init --me $ME"
    else trk init --me "$ME" || die "trk init failed; is the uv tool bin directory on PATH? ($(uv tool dir --bin))"; fi
  fi
}

next_steps() {
  cat <<'EOF'

== next steps
1. Make sure `trk` is on your PATH: run `trk --version`. If it is not found, run `uv tool update-shell`.
2. The agent actors `worker`, `herdr` and `publisher` are registered for you, so an agent's writes
   are not recorded as yours. Register any others you need:
     trk actor add <name> --kind agent
   Check actors and roles with `trk actors` and `trk roles`; reassign a role with
   `trk roles set <role> <actor>`.
   Then read docs/agents.md.
3. Restart Claude Code so it loads the new skills and hooks.
4. Optional, publishing pages: `trk pages setup --adapter "trk-pages-folder --dir ~/trk-pages" --home <id>` writes pages to a directory. An adapter for a hosted service keeps its own credentials, which this script never sees (docs/adapters.md).
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
report_guidance
say "== hooks"
settings_cmd add
((PAGES)) && install_units
((DRY)) || next_steps
