"""The Claude Code session hooks in hooks/: they run as subprocesses, read the hook JSON on stdin,
and must stay silent and exit 0 whatever happens."""

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SESSION = "5f0c2a9e-0000-4000-8000-000000000001"


BIN = str(Path(sys.executable).parent)  # this checkout's venv, so the hook runs this source's trk


def run_hook(name, payload, **env):
    path = f"{BIN}{os.pathsep}{os.environ['PATH']}"
    return subprocess.run([HOOKS / name], input=json.dumps(payload), capture_output=True, text=True,
                          env={**os.environ, "PATH": path, **env}, check=False)


def test_session_start_exports_the_session_uuid(tmp_path):
    env_file = tmp_path / "env"
    result = run_hook("trk-session-start.sh", {"session_id": SESSION, "source": "startup"},
                      CLAUDE_ENV_FILE=str(env_file))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert env_file.read_text() == f"export TRK_SESSION={SESSION}\n"


def test_session_start_without_an_env_file_is_a_silent_no_op(monkeypatch):
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    result = run_hook("trk-session-start.sh", {"session_id": SESSION})
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_renew_hook_renews_a_due_lease_silently(trk, monkeypatch):
    trk("init", "--me", "michael")
    decision = trk("new", "work-item", "D", "--form", "decision", "--subtype", "task").out.strip()
    monkeypatch.setenv("TRK_NOW", "2026-10-10T12:00:00Z")
    trk("claim", decision, "--session", SESSION)
    before = trk("claims", "--json").json()["claims"][0]["lease_until"]

    result = run_hook("trk-renew.sh", {"session_id": SESSION, "hook_event_name": "UserPromptSubmit"},
                      TRK_NOW="2026-10-10T12:45:00Z")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    after = trk("claims", "--json").json()["claims"][0]["lease_until"]
    assert (before, after) == ("2026-10-10T13:00:00.000Z", "2026-10-10T13:45:00.000Z")


def test_renew_hook_is_silent_without_a_store():
    result = run_hook("trk-renew.sh", {"session_id": SESSION})
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_settings_snippet_runs_the_repo_scripts_on_session_start_and_every_turn():
    snippet = json.loads((HOOKS / "settings-snippet.json").read_text())["hooks"]
    commands = {event: [h["command"] for group in groups for h in group["hooks"]] for event, groups in snippet.items()}
    expected = {"SessionStart": "trk-session-start.sh", "UserPromptSubmit": "trk-renew.sh", "PostToolUse": "trk-renew.sh"}
    assert {event: [Path(p).name for p in paths] for event, paths in commands.items()} == {e: [n] for e, n in expected.items()}
    for paths in commands.values():
        assert [str(Path(p).parent) for p in paths] == ["<TRK_REPO>/hooks"]  # install.sh fills in the checkout
    for name in set(expected.values()):
        assert os.access(HOOKS / name, os.X_OK)
