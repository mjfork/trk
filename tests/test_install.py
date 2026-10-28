"""install.sh in a sandbox: a temp HOME, a temp store, TRK_TEST=1, and a fake `uv` and `systemctl`
on PATH, so nothing touches this machine's real tool, store, skills, settings or units."""

import fcntl
import hashlib
import json
import os
import pty
import sqlite3
import shutil
import subprocess
import sys
import termios
import threading
from pathlib import Path

import pytest

from trk import timer

REPO = Path(__file__).resolve().parent.parent
SKILLS = ("trk", "trk-drive-program", "trk-create-program")
RULE = "Bash(trk *)"            # the one permission rule the installer offers to add

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
        # Its own session, so the script has no controlling terminal: a run that would ask takes the
        # no-terminal path instead of blocking on a question. The tty paths get a pty on purpose.
        done = subprocess.run([str(REPO / "install.sh"), *args], env=env, capture_output=True, text=True,
                              check=False, start_new_session=True)
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
    assert any(p.read_text() == "local edits\n" for p in (box.home / ".local/share/trk-skill-backups").glob("trk.*/SKILL.md"))
    assert not list((box.home / ".claude/skills").glob("*bak*"))  # aq UP-0001: no backup loads as a skill


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


def actors_in(store):
    conn = sqlite3.connect(store / "store.db")
    try:
        return {row[0] for row in conn.execute("SELECT id FROM actors")}
    finally:
        conn.close()


def test_a_store_from_before_the_worker_actor_is_topped_up(box):
    """`trk init` registers `worker` now, but a store made before it does not have one, and every skill
    writes `--as worker`. Installing over such a store registers it; a rerun writes nothing."""
    box()
    store = box.home / "store"
    assert "worker" in actors_in(store)

    conn = sqlite3.connect(store / "store.db")          # as an older trk left it: no worker
    with conn:
        conn.execute("DELETE FROM actors WHERE id = 'worker'")
    conn.close()
    assert "worker" not in actors_in(store)

    again = box()

    assert "registering the 'worker' agent actor" in again.stdout
    assert "worker" in actors_in(store)

    once_more = box()

    assert "registering the 'worker' agent actor" not in once_more.stdout, "a rerun writes nothing"


def test_a_dry_run_does_not_top_up_the_worker_actor(box):
    box()
    store = box.home / "store"
    conn = sqlite3.connect(store / "store.db")
    with conn:
        conn.execute("DELETE FROM actors WHERE id = 'worker'")
    conn.close()

    done = box("--dry-run")

    assert "[dry-run] register the 'worker' actor" in done.stdout
    assert "worker" not in actors_in(store)


def allow_rules(home):
    """`permissions.allow` in the box's settings.json, or None when there is no such list."""
    path = home / ".claude/settings.json"
    if not path.exists():
        return None
    return json.loads(path.read_text()).get("permissions", {}).get("allow")


def marker(home):
    return home / ".local/state/trk-install/allow-added"


def test_allow_trk_adds_the_permission_rule_and_uninstall_takes_out_its_own(box):
    first = box("--allow-trk")

    assert RULE in first.stdout and allow_rules(box.home) == [RULE]
    assert marker(box.home).exists()           # it was this script that added it

    before = snapshot(box.home)
    again = box("--allow-trk")

    assert "nothing to do" in again.stdout, "a second run asks nothing and writes nothing"
    assert snapshot(box.home) == before

    box("--uninstall")

    assert "permissions" not in json.loads((box.home / ".claude/settings.json").read_text())
    assert not marker(box.home).exists()


def test_the_rule_joins_an_allow_list_without_disturbing_it(box):
    path = box.home / ".claude/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"], "deny": ["Bash(rm *)"]},
                                "model": "x"}))

    box("--allow-trk")

    settings = json.loads(path.read_text())
    assert settings["permissions"] == {"allow": ["Bash(ls)", RULE], "deny": ["Bash(rm *)"]}
    assert settings["model"] == "x"
    assert list(path.parent.glob("settings.json.trk-bak-*"))     # backed up before the merge

    box("--uninstall")

    settings = json.loads(path.read_text())
    assert settings["permissions"] == {"allow": ["Bash(ls)"], "deny": ["Bash(rm *)"]}


def test_no_allow_trk_leaves_the_rule_out_and_says_how_to_add_it(box):
    done = box("--no-allow-trk")

    assert allow_rules(box.home) is None
    assert "leaving permissions.allow alone" in done.stdout
    assert "./install.sh --allow-trk" in done.stdout
    assert not marker(box.home).exists()


def test_a_run_with_no_terminal_to_ask_on_skips_the_rule(box):
    done = box()        # the fixture gives it its own session, so there is no /dev/tty to ask on

    assert allow_rules(box.home) is None
    assert "no terminal to ask on" in done.stdout
    assert "./install.sh --allow-trk" in done.stdout
    assert not marker(box.home).exists()


def test_a_rule_this_script_did_not_add_is_left_alone(box):
    path = box.home / ".claude/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"allow": [RULE]}}))

    done = box()

    assert "nothing to do" in done.stdout
    assert not marker(box.home).exists()

    removed = box("--uninstall")

    assert "not added by this script" in removed.stdout
    assert allow_rules(box.home) == [RULE]


def test_a_dry_run_never_asks_and_writes_nothing_either_way(box):
    path = seed_settings(box.home)
    before = snapshot(box.home)

    asked = box("--dry-run")

    assert "a dry run never asks" in asked.stdout and snapshot(box.home) == before

    allowed = box("--dry-run", "--allow-trk")

    assert f'[dry-run] add "{RULE}"' in allowed.stdout and snapshot(box.home) == before

    declined = box("--dry-run", "--no-allow-trk")

    assert "--no-allow-trk: leaving permissions.allow alone" in declined.stdout
    assert snapshot(box.home) == before
    assert "permissions" not in json.loads(path.read_text())


def test_permissions_the_file_shapes_some_other_way_stop_the_step(box):
    path = box.home / ".claude/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"allow": "Bash(ls)"}}))

    done = box("--allow-trk", check=False)

    assert done.returncode != 0
    assert "not a JSON array" in done.stderr
    assert json.loads(path.read_text())["permissions"] == {"allow": "Bash(ls)"}


def on_a_terminal(box, args, typed, piped=None):
    """install.sh with a controlling terminal of its own: `typed` is answered at the terminal, and
    `piped`, when given, goes to stdin instead -- the shape `curl ... | bash` runs the script in."""
    master, slave = pty.openpty()
    if piped is None:
        pre, stdin = (lambda: os.login_tty(slave)), slave
    else:   # a session leader with the pty as its controlling terminal, but a pipe on stdin
        pre, stdin = (lambda: (os.setsid(), fcntl.ioctl(slave, termios.TIOCSCTTY, 0))), subprocess.PIPE
    done = subprocess.Popen([str(REPO / "install.sh"), *args], env=box.env, stdin=stdin, stdout=slave,
                            stderr=slave, preexec_fn=pre, pass_fds=(slave,))
    os.close(slave)
    chunks = []

    def drain():
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:         # the child let go of the pty
                return
            if not data:
                return
            chunks.append(data)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        os.write(master, typed)
        if piped is not None:
            done.stdin.write(piped)
            done.stdin.close()
        code = done.wait(timeout=120)
    except subprocess.TimeoutExpired:
        done.kill()
        raise AssertionError("install.sh did not finish; it is waiting for something")
    finally:
        reader.join(timeout=5)
        os.close(master)
    return code, b"".join(chunks).decode(errors="replace")


def test_the_question_is_asked_on_the_terminal_and_a_yes_adds_the_rule(box):
    code, out = on_a_terminal(box, [], b"y\n")

    assert code == 0, out
    assert "Allow trk commands" in out and RULE in out
    assert allow_rules(box.home) == [RULE]
    assert marker(box.home).exists()


def test_enter_at_the_question_leaves_the_rule_out(box):
    code, out = on_a_terminal(box, [], b"\n")

    assert code == 0, out
    assert "Allow trk commands" in out
    assert "leaving permissions.allow alone" in out
    assert allow_rules(box.home) is None and not marker(box.home).exists()


def test_the_answer_is_read_from_the_terminal_not_from_stdin(box):
    """Under `curl | bash` stdin is the script itself, so an answer may never be taken from it."""
    code, out = on_a_terminal(box, [], b"n\n", piped=b"y\ny\ny\n")

    assert code == 0, out
    assert "Allow trk commands" in out
    assert allow_rules(box.home) is None, "the 'n' at the terminal decided it, not the 'y' on stdin"
    assert not marker(box.home).exists()


def test_help_prints_the_options_and_exits_zero(box):
    """The catch-all case used to swallow -h and --help, so the help printed and the script failed."""
    done = box("--help")

    assert "--allow-trk" in done.stdout and "make-bundle" in done.stdout
    assert box("-h").stdout == done.stdout
