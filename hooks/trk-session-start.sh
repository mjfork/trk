#!/usr/bin/env bash
# Claude Code SessionStart hook: export the session uuid as TRK_SESSION for every later
# Bash call in this session, so `trk claim` carries it. Reads the hook's JSON on stdin and
# appends to $CLAUDE_ENV_FILE. Fires again on /clear with the new uuid. Silent; always exits 0.
input=$(cat)
if [[ $input =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]] && [[ -n ${CLAUDE_ENV_FILE:-} ]]; then
  printf 'export TRK_SESSION=%q\n' "${BASH_REMATCH[1]}" >> "$CLAUDE_ENV_FILE"
fi
exit 0
