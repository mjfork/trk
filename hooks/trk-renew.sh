#!/usr/bin/env bash
# Claude Code per-turn hook (UserPromptSubmit and PostToolUse): renew this session's claims.
# `trk claim renew` writes only leases past half their length, so this costs no tokens and
# almost never takes the write lock. The session uuid comes from the hook's JSON on stdin,
# else $TRK_SESSION. Silent, and always exits 0: a missing trk or store never disturbs a turn.
input=$(cat)
session=${TRK_SESSION:-}
if [[ $input =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
  session=${BASH_REMATCH[1]}
fi
if [[ -n $session ]] && command -v trk >/dev/null 2>&1; then
  trk claim renew --session "$session" >/dev/null 2>&1
fi
exit 0
