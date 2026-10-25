"""install.sh in a sandbox: a temp HOME, a temp store, TRK_TEST=1, and a fake `uv` and `systemctl`
on PATH, so nothing touches this machine's real tool, store, skills, settings or units."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from trk import timer

REPO = Path(__file__).resolve().parent.parent
SKILLS = ("trk", "trk-drive-program", "trk-create-program")

FAKE_UV = r"""#!/usr/bin/env bash
state=$HOME/.fake-uv-installed
case "$1 $2" in
  "tool list") [[ -e $state ]] && echo "trk v0.1.0" ;;
  "tool dir") echo "$HOME/.local/bin" ;;
  "tool uninstall") rm -f "$state" "$HOME/.local/bin/trk" ;;
  "tool install")
    mkdir -p "$HOME/.local/bin"
    printf '%s\n' "$*" >"$state"
    printf '#!/usr/bin/env bash\nPYTHONPATH=%s exec %s -m trk "$@"\n' "$FAKE_TRK_SRC" "$FAKE_PYTHON" >"$HOME/.local/bin/trk"
    chmod +x "$HOME/.local/bin/trk" ;;
  *) echo "fake uv: unexpected $*" >&2; exit 2 ;;
esac
"""

FAKE_SYSTEMCTL = '#!/usr/bin/env bash\necho "$*" >>"$HOME/.systemctl-calls"\n'


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    for name, body in (("uv", FAKE_UV), ("systemctl", FAKE_SYSTEMCTL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (bin_dir / "python3").write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")   # this python, not the box's
    (bin_dir / "python3").chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:{home}/.local/bin:/usr/bin:/bin",
        "TRK_HOME": str(home / "store"),
        "TRK_TEST": "1",
        "XDG_CONFIG_HOME": str(home / "xdg"),
        "FAKE_TRK_SRC": str(REPO / "src"),
        "FAKE_PYTHON": sys.executable,
        "USER": "tester",
    }

    def run(*args, check=True):
        done = subprocess.run([str(REPO / "install.sh"), *args], env=env, capture_output=True, text=True, check=False)
        if check:
            assert done.returncode == 0, (args, done.stdout, done.stderr)
        return done

    run.home, run.env = home, env
    return run


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def seed_settings(home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}],
                                                          "SessionStart": [{"hooks": [{"type": "command", "command": "mine2.sh"}]}]}}))
    return path


def commands(settings):
    return [h["command"] for entries in settings.get("hooks", {}).values() for e in entries for h in e["hooks"]]


def test_clean_install_then_rerun_then_uninstall(box):
    settings_path = seed_settings(box.home)
    first = box()
    assert "next steps" in first.stdout
    assert (box.home / "store" / "store.db").exists()
    assert (box.home / ".local/bin/trk").exists()
    for skill in SKILLS:
        assert subprocess.run(["diff", "-r", REPO / "skills" / skill, box.home / ".claude/skills" / skill]).returncode == 0
    settings = json.loads(settings_path.read_text())
    assert settings["model"] == "x"
    mine = {"mine.sh", "mine2.sh"}
    ours = set(commands(settings)) - mine
    assert ours == {f"{REPO}/hooks/trk-session-start.sh", f"{REPO}/hooks/trk-renew.sh"}
    assert mine <= set(commands(settings))
    assert list(settings_path.parent.glob("settings.json.trk-bak-*"))  # backed up before the merge

    before = snapshot(box.home)
    second = box()
    assert snapshot(box.home) == before
    assert "already" in second.stdout

    removed = box("--uninstall")
    assert "store at" in removed.stdout
    assert (box.home / "store" / "store.db").exists()
    assert not (box.home / ".local/bin/trk").exists()
    for skill in SKILLS:
        assert not (box.home / ".claude/skills" / skill).exists()
    settings = json.loads(settings_path.read_text())
    assert set(commands(settings)) == mine and settings["model"] == "x"


def test_install_with_no_settings_file_creates_one(box):
    box()
    settings = json.loads((box.home / ".claude/settings.json").read_text())
    assert len(commands(settings)) == 3
    box("--uninstall")
    assert json.loads((box.home / ".claude/settings.json").read_text()) == {}


def test_existing_store_is_never_touched(box):
    box()
    store = box.home / "store"
    before = snapshot(store)
    box("--force")
    assert snapshot(store) == before


def test_dry_run_changes_nothing_in_either_direction(box):
    seed_settings(box.home)
    before = snapshot(box.home)
    out = box("--dry-run").stdout
    assert "[dry-run]" in out and snapshot(box.home) == before and not (box.home / "store").exists()
    box()
    before = snapshot(box.home)
    assert "[dry-run]" in box("--uninstall", "--dry-run").stdout
    assert snapshot(box.home) == before


def test_no_editable_flag_reaches_uv(box):
    box("--no-editable")
    assert "--editable" not in (box.home / ".fake-uv-installed").read_text()
    box("--uninstall")
    box()
    assert "--editable" in (box.home / ".fake-uv-installed").read_text()


def test_differing_skill_is_refused_without_force_and_replaced_with_it(box):
    box()
    skill = box.home / ".claude/skills/trk/SKILL.md"
    skill.write_text("local edits\n")
    before = snapshot(box.home)
    refused = box(check=False)
    assert refused.returncode != 0 and "differ from the repo copy" in refused.stderr
    assert "SKILL.md" in refused.stdout and snapshot(box.home) == before
    dry = box("--dry-run")
    assert "would refuse" in dry.stdout and snapshot(box.home) == before
    dry_force = box("--dry-run", "--force")
    assert "will be replaced" in dry_force.stdout and snapshot(box.home) == before
    box("--force")
    assert skill.read_bytes() == (REPO / "skills/trk/SKILL.md").read_bytes()
    assert any(p.read_text() == "local edits\n" for p in (box.home / ".claude/skills").glob("trk.bak-*/SKILL.md"))


def test_uninstall_leaves_an_edited_skill_alone(box):
    box()
    skill = box.home / ".claude/skills/trk-drive-program/SKILL.md"
    skill.write_text("local edits\n")
    assert "edited since install" in box("--uninstall").stdout
    assert skill.exists()


@pytest.mark.parametrize("content", ["", "  \n\t\n"])
def test_empty_or_whitespace_settings_count_as_empty_object(box, content):
    path = box.home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    box()
    assert len(commands(json.loads(path.read_text()))) == 3
    path.write_text(content)
    assert "up to date" in box("--uninstall").stdout
    assert path.read_text() == content


def test_uninstall_leaves_a_trk_it_did_not_install(box):
    uv_state = box.home / ".fake-uv-installed"
    shim = box.home / ".local/bin/trk"
    shim.parent.mkdir(parents=True)
    shim.write_text(f'#!/usr/bin/env bash\nPYTHONPATH={REPO / "src"} exec {sys.executable} -m trk "$@"\n')
    shim.chmod(0o755)
    uv_state.write_text("tool install earlier\n")
    out = box().stdout
    assert "already installed" in out and uv_state.read_text() == "tool install earlier\n"
    removed = box("--uninstall")
    assert "not installed by this script" in removed.stdout
    assert uv_state.exists() and shim.exists()


def test_uninstall_removes_the_trk_it_installed_and_its_marker(box):
    box()
    marker = box.home / ".local/state/trk-install/tool-installed"
    assert marker.exists()
    box("--uninstall")
    assert not marker.exists() and not (box.home / ".local/bin/trk").exists()


def test_invalid_settings_json_stops_the_install_without_changes(box):
    path = box.home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    done = box(check=False)
    assert done.returncode != 0 and "not valid JSON" in done.stderr
    assert path.read_text() == "{not json"


def test_pages_installs_the_units_the_cli_would_write(box):
    box("--pages")
    unit_dir = box.home / "xdg/systemd/user"
    trk = str(box.home / ".local/bin/trk")
    expected = timer.units(trk, str((box.home / "store").resolve()))
    for name, text in expected.items():
        assert (unit_dir / name).read_text() == text
    calls = (box.home / ".systemctl-calls").read_text()
    assert "--user enable --now trk-pages.timer" in calls
    box("--uninstall")
    assert not (unit_dir / "trk-pages.timer").exists()


def test_old_python_is_refused(box, tmp_path):
    for name in ("python3", "python3.14", "python3.13", "python3.12"):   # every python the script looks for
        fake = tmp_path / "bin" / name
        fake.write_text("#!/usr/bin/env bash\nexit 1\n")
        fake.chmod(0o755)
    done = box(check=False)
    assert done.returncode != 0 and "python 3.12" in done.stderr


def test_make_bundle_writes_a_cloneable_file(box, tmp_path):
    repo = tmp_path / "repo"  # its own repo: the test does not depend on where pytest started
    repo.mkdir()
    shutil.copy(REPO / "install.sh", repo / "install.sh")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo)]
    for argv in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
        subprocess.run([*git, *argv], check=True, capture_output=True)
    out = tmp_path / "trk.bundle"
    done = subprocess.run([str(repo / "install.sh"), "make-bundle", str(out)], env=box.env, cwd=tmp_path,
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert subprocess.run(["git", "bundle", "verify", out], capture_output=True, cwd=repo).returncode == 0
    again = subprocess.run([str(repo / "install.sh"), "make-bundle", str(out)], env=box.env, capture_output=True, text=True, check=False)
    assert again.returncode != 0 and "already exists" in again.stderr
    clone = tmp_path / "clone"
    assert subprocess.run(["git", "clone", "-q", out, clone], capture_output=True).returncode == 0
    assert (clone / "install.sh").exists()


def test_ac1_make_bundle_packs_the_default_branch_and_tags_but_no_worktree_refs(box, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(REPO / "install.sh", repo / "install.sh")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo)]
    for argv in (["init", "-q", "-b", "master"], ["add", "."], ["commit", "-qm", "x"], ["tag", "v1"],
                 ["branch", "unmerged"], ["worktree", "add", "-q", "-b", "wt-branch", str(tmp_path / "wt")]):
        subprocess.run([*git, *argv], check=True, capture_output=True)

    def heads(*flags):
        out = tmp_path / f"b{len(flags)}.bundle"
        done = subprocess.run([str(repo / "install.sh"), "make-bundle", *flags, str(out)], env=box.env,
                              cwd=tmp_path, capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stderr
        clone = tmp_path / f"clone{len(flags)}"
        assert subprocess.run(["git", "clone", "-q", out, clone], capture_output=True).returncode == 0
        refs = subprocess.run(["git", "-C", clone, "for-each-ref", "--format=%(refname)"], capture_output=True,
                              text=True, check=True).stdout.split()
        assert not [r for r in refs if "worktrees/" in r]
        return refs

    assert heads() == ["refs/heads/master", "refs/remotes/origin/HEAD", "refs/remotes/origin/master", "refs/tags/v1"]
    everything = heads("--all-branches")
    assert "refs/remotes/origin/unmerged" in everything and "refs/remotes/origin/wt-branch" in everything
