"""The background flusher and the timer (Page publisher spec: Flusher; Timer and the pull; Amendments
to Core's spawn check and second lock file), against the fake page backend: in process, or
behind a localhost HTTP server for the one test that starts a real detached flusher. The real
service is never called, and no systemd unit is installed: the timer tests use a temp unit
directory and a stub `systemctl`."""

import fcntl
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from gated import section_file
from test_pages_diff import at, dirty, ok, published, writes_to  # noqa: F401 (fixtures)
from test_pages_flush import db, flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_renewal import rejected, renew, renewal_asks
from test_pages_setup import ME, fake, journal, setup_argv, token_file, tree  # noqa: F401 (fixtures)
from trk import clock, flusher, pages, spawn, store, timer

REAL_LAUNCH = spawn.launch  # conftest swaps it for a recorder in every test
SRC = Path(__file__).resolve().parent.parent / "src"

T0 = "2026-10-11T09:00:00Z"


def section_set(trk, item_id, text):
    ok(trk, "section", "set", item_id, "what-to-build", "--file", str(section_file("w", text)))


@pytest.fixture
def sleeps(monkeypatch):
    """`flush --wait`'s sleeps: recorded, and each moves $TRK_NOW on by its length instead of waiting."""
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        monkeypatch.setenv("TRK_NOW", (clock.now() + timedelta(seconds=seconds)).isoformat())

    monkeypatch.setattr(flusher, "_sleep", sleep)
    return slept


def flush_wait(trk):
    result = trk("pages", "flush", "--wait", "--json")
    assert result.code == 0, result.err
    return result.json()


def lock_free(trk_home):
    """Whether pages.lock can be taken right now by someone else."""
    with open(trk_home / flusher.LOCK_NAME, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


def write_lock_free(trk_home):
    """Whether no SQLite write transaction is open on the store right now."""
    conn = sqlite3.connect(trk_home / "store.db", timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


@pytest.fixture
def stand_in():
    """A live process whose command line reads `… pages flush --wait`, as a running flusher's does."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", *spawn.FLUSH_WAIT])
    yield proc
    proc.kill()
    proc.wait()


# ----- AC 1 and AC 2: the spawn check -----------------------------------------------------------------


def test_before_setup_a_write_spawns_no_flusher(trk, trk_home, tree, spawned):
    section_set(trk, tree["T"], "Before setup.\n")
    assert spawned == []
    assert not (trk_home / spawn.PID_NAME).exists()


def test_after_setup_a_marking_write_starts_one_flusher(trk, trk_home, live, spawned):
    assert spawned == []  # setup journals config rows only, which mark nothing
    section_set(trk, live["T"], "After setup.\n")
    assert spawned == [trk_home]


def test_a_write_while_a_flusher_is_recorded_starts_none(trk, trk_home, live, spawned, stand_in):
    (trk_home / spawn.PID_NAME).write_text(f"{stand_in.pid}\n")
    section_set(trk, live["T"], "One.\n")
    section_set(trk, live["J"], "Two.\n")
    assert spawned == []


def test_a_stale_pid_file_does_not_stop_the_spawn(trk, trk_home, live, spawned):
    dead = subprocess.Popen([sys.executable, "-c", "pass", *spawn.FLUSH_WAIT])
    dead.wait()
    (trk_home / spawn.PID_NAME).write_text(f"{dead.pid}\n")
    section_set(trk, live["T"], "Dead flusher.\n")
    (trk_home / spawn.PID_NAME).write_text(f"{os.getpid()}\n")  # alive, but not a flusher
    section_set(trk, live["J"], "Not a flusher.\n")
    assert spawned == [trk_home, trk_home]


def test_claim_renew_never_spawns(trk, trk_home, live, spawned, monkeypatch):
    ok(trk, "claim", live["D"], "--session", "S1", "--lease", "30m", "--as", "worker")
    spawned.clear()
    at(monkeypatch, "2026-10-11T09:20:00Z")
    ok(trk, "claim", "renew", "--session", "S1", "--as", "worker")
    assert spawned == []


def test_a_spawn_that_fails_never_fails_the_write(trk, trk_home, live, monkeypatch):
    def broken(directory):
        raise OSError("no fork today")

    monkeypatch.setattr(spawn, "launch", broken)
    section_set(trk, live["T"], "Still written.\n")
    assert "no fork today" in (trk_home / spawn.LOG_NAME).read_text()


@pytest.mark.parametrize("change", ["claim.renewed", "actor.added", "config.changed", "page.created", "edge.added",
                                    "item.created", "ask.answered", "evidence.added", "owner.changed",
                                    "item.moved", "role.set", "actor.updated", "claim.expired"])
def test_the_spawn_check_agrees_with_the_marking_table(trk, trk_home, live, change):
    """A row the check passes over must mark nothing; a row it counts must be one the table maps."""
    conn = db(trk_home)
    data = {"edge.added": {"src": live["T"], "dst": live["D"], "type": "blocks"},
            "item.moved": {"old": {"parent": live["P"]}, "new": {"parent": live["J"]}}}.get(change, {})
    row = {"change": change, "item": live["T"], "data": json.dumps(data)}
    assert spawn.can_mark(change, live["T"]) == bool(pages.affected(conn, row))
    assert spawn.can_mark(change, None) == bool(pages.affected(conn, {**row, "item": None}))


def test_launch_starts_a_detached_flusher_logging_to_the_store(trk_home, monkeypatch):
    trk_home.mkdir()
    probe = ("import os, sys; print(os.getsid(0) == os.getpid(), sys.stdin.read() == '', *sys.argv[1:]); "
             "sys.stdout.flush()")
    monkeypatch.setattr(spawn, "command", lambda: [sys.executable, "-c", probe])
    REAL_LAUNCH(trk_home)
    log = trk_home / spawn.LOG_NAME
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and "--wait" not in (log.read_text() if log.exists() else ""):
        time.sleep(0.05)
    assert log.read_text().split() == ["True", "True", *spawn.FLUSH_WAIT]


def test_launch_records_the_child_before_it_registers(trk_home, monkeypatch):
    trk_home.mkdir()
    monkeypatch.setattr(spawn, "command", lambda: [sys.executable, "-c", "import time; time.sleep(30)"])
    REAL_LAUNCH(trk_home)
    pid = spawn.recorded_pid(trk_home)
    try:
        assert pid is not None and spawn.live_flusher(trk_home) == pid
        assert spawn.register(trk_home, pid=pid)  # the child registering itself keeps its record
    finally:
        os.kill(pid, 9)


def test_register_leaves_one_flusher_and_replaces_a_stale_record(trk_home, stand_in):
    trk_home.mkdir()
    assert spawn.register(trk_home, pid=stand_in.pid)
    assert not spawn.register(trk_home)  # another live flusher holds it
    spawn.unregister(trk_home)  # not ours: left alone
    assert spawn.recorded_pid(trk_home) == stand_in.pid
    (trk_home / spawn.PID_NAME).write_text("999999999\n")
    assert spawn.register(trk_home)
    assert spawn.recorded_pid(trk_home) == os.getpid()
    spawn.unregister(trk_home)
    assert not (trk_home / spawn.PID_NAME).exists()


# ----- AC 3: the --wait loop and its exit rule ---------------------------------------------------------


def test_flush_wait_sleeps_until_the_page_is_due_then_sends_it_and_exits(trk, trk_home, published, fake, sleeps,
                                                                           monkeypatch):
    at(monkeypatch, "2026-10-11T09:10:00Z")
    section_set(trk, published["T"]["id"], "Changed.\n")
    seen = []

    def watch(seconds):
        seen.append((spawn.recorded_pid(trk_home), lock_free(trk_home), write_lock_free(trk_home)))
        sleep(seconds)

    sleep = flusher._sleep
    monkeypatch.setattr(flusher, "_sleep", watch)
    report = flush_wait(trk)
    assert sleeps == [60.0]  # due 60 s after the row
    assert seen == [(os.getpid(), True, True)]  # registered while it slept; neither lock held
    assert report["updated"] == [published["T"]["id"]]  # P and J were marked too, and render the same
    assert sorted(report["unchanged"]) == sorted(published[k]["id"] for k in ("P", "J"))
    assert len(writes_to(fake, published["T"]["slug"])) == 1
    assert dirty(trk_home) == set()
    assert not (trk_home / spawn.PID_NAME).exists()


def test_flush_wait_with_nothing_dirty_exits_at_once(trk, trk_home, published, fake, sleeps):
    assert flush_wait(trk) == {"schema": 1, **{outcome: [] for outcome in flusher.OUTCOMES}, "held": False,
                               "renewal_ask": None}
    assert sleeps == []
    assert fake.calls == []
    assert not (trk_home / spawn.PID_NAME).exists()


def test_flush_wait_exits_when_only_failed_pages_are_dirty(trk, trk_home, published, fake, sleeps, monkeypatch):
    at(monkeypatch, "2026-10-11T09:10:00Z")
    section_set(trk, published["T"]["id"], "Will fail.\n")
    fake.fail_next("500", tool="update", times=3)
    report = flush_wait(trk)
    assert sleeps == [60.0]  # one sleep to the due time, none after every page failed
    assert report["failed"] == [published["T"]["id"]]
    assert dirty(trk_home) == {published["T"]["id"]}
    assert page_row(trk_home, published["T"]["id"])["last_error"]
    assert not (trk_home / spawn.PID_NAME).exists()


def test_a_401_ends_the_wait(trk, trk_home, published, fake, sleeps, monkeypatch):
    at(monkeypatch, "2026-10-11T09:10:00Z")
    section_set(trk, published["T"]["id"], "Rejected.\n")
    fake.fail_next("401")
    flush_wait(trk)
    assert sleeps == [60.0]  # no sleep after the 401: the run ended
    assert page_row(trk_home, published["T"]["id"])["last_error"]
    assert dirty(trk_home) == {published["T"]["id"]}
    assert not (trk_home / spawn.PID_NAME).exists()


def test_a_write_landing_as_the_flusher_finishes_is_still_sent(trk, trk_home, published, fake, sleeps, spawned,
                                                               monkeypatch):
    """The head-vs-cursor recheck: a write that commits after the last mark but before the pid file
    goes found the flusher still registered (so started none); the flusher must loop again for it."""
    at(monkeypatch, "2026-10-11T09:10:00Z")
    real_unregister = spawn.unregister
    late = []

    real_live = spawn.live_flusher

    def unregister_then_write(directory, pid=None):
        if not late:
            # The pid file still names this flusher, so the late writer's spawn check finds it live
            # (in a real run its cmdline says so; here the test process stands in for it).
            assert spawn.recorded_pid(directory) == os.getpid()
            monkeypatch.setattr(spawn, "live_flusher", lambda directory: spawn.recorded_pid(directory))
            section_set(trk, published["D"]["id"], "Late.\n")
            monkeypatch.setattr(spawn, "live_flusher", real_live)
            late.append(True)
        real_unregister(directory, pid)

    monkeypatch.setattr(spawn, "unregister", unregister_then_write)
    report = flush_wait(trk)
    assert spawned == []  # the late writer started no flusher of its own
    assert late == [True]
    assert sleeps == [60.0]  # the second round waited for the late write's quiet window
    assert len(writes_to(fake, published["D"]["slug"])) == 1
    assert published["D"]["id"] in report["updated"]
    assert dirty(trk_home) == set()


def test_a_second_flusher_exits_at_once(trk, trk_home, live, fake, sleeps, stand_in):
    (trk_home / spawn.PID_NAME).write_text(f"{stand_in.pid}\n")
    report = flush_wait(trk)
    assert report == {"schema": 1, **{outcome: [] for outcome in flusher.OUTCOMES}, "held": False, "renewal_ask": None}
    assert fake.calls == []
    assert spawn.recorded_pid(trk_home) == stand_in.pid  # left in place for the live one


def test_flush_wait_marks_writes_that_land_during_a_pass(trk, trk_home, published, fake, sleeps, monkeypatch):
    """A write committed mid-pass is marked before the flusher sleeps, so it is not left for the timer."""
    at(monkeypatch, "2026-10-11T09:10:00Z")
    section_set(trk, published["T"]["id"], "First.\n")
    real_send = flusher._send
    wrote = []

    def send_then_write(st, client, item_id, **kwargs):
        outcome = real_send(st, client, item_id, **kwargs)
        if not wrote:
            wrote.append(True)
            section_set(trk, published["D"]["id"], "During the pass.\n")
        return outcome

    monkeypatch.setattr(flusher, "_send", send_then_write)
    flush_wait(trk)
    assert sleeps == [60.0, 60.0]
    assert len(writes_to(fake, published["D"]["slug"])) == 1
    assert dirty(trk_home) == set()


# ----- AC 4: pages.lock per unit of network work -------------------------------------------------------


def test_every_call_runs_under_pages_lock_and_outside_any_write_transaction(trk, trk_home, live, fake, sleeps,
                                                                              monkeypatch):
    held = []
    real_call = fake.call

    def watched(name, arguments, **kwargs):
        held.append((lock_free(trk_home), write_lock_free(trk_home)))
        return real_call(name, arguments, **kwargs)

    monkeypatch.setattr(fake, "call", watched)
    flush_wait(trk)
    assert held and set(held) == {(False, True)}


def test_pages_lock_refuses_an_open_write_transaction(trk, trk_home, live):
    st = store.open_store()
    try:
        with st.write_lock(), pytest.raises(RuntimeError), flusher.pages_lock(st):
            pass
    finally:
        st.close()


# ----- tick ------------------------------------------------------------------------------------------


def test_tick_publishes_what_is_due_without_waiting(trk, trk_home, live, fake, sleeps):
    result = trk("pages", "tick", "--json")
    assert result.code == 0, result.err
    assert sorted(result.json()["published"]) == sorted([*live.values(), ME])
    assert sleeps == []
    assert not (trk_home / spawn.PID_NAME).exists()


def test_tick_runs_the_token_check_and_the_next_tick_after_setup_catches_up(trk, trk_home, live, fake, tmp_path):
    """Spec AC 10: a 401 holds every page write and opens one renewal ask; setup with a new token,
    as michael, answers it, and the next tick publishes every dirty page."""
    rejected(fake)
    held = trk("pages", "tick", "--json")
    assert held.code == 0, held.err
    assert held.json()["held"] is True and held.json()["renewal_ask"] == renewal_asks(trk_home)[0]["id"]
    assert fake.tool_calls("publish") == []
    assert renew(trk, fake, tmp_path, live["P"], "--as", "michael").code == 0
    caught_up = trk("pages", "tick", "--json").json()
    assert caught_up["held"] is False
    assert caught_up["published"] == [live[k] for k in ("P", "J", "D", "T")] + [ME]
    assert renewal_asks(trk_home)[0]["status"] == "closed"


def test_flush_wait_while_held_exits_at_once(trk, trk_home, live, fake, sleeps):
    rejected(fake)
    trk("pages", "flush", "--json")  # the 401 sets the hold
    fake.requests.clear()
    report = flush_wait(trk)
    assert report["held"] is True and report["published"] == []
    assert fake.requests == [] and sleeps == []
    assert not (trk_home / spawn.PID_NAME).exists()


# ----- AC 5: the timer ---------------------------------------------------------------------------------


@pytest.fixture
def units(tmp_path, monkeypatch):
    """A temp unit directory and a stub `systemctl` that logs its arguments and reports the timer
    enabled and active while its unit file exists."""
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    unit_dir = config / "systemd" / "user"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    calls = tmp_path / "systemctl.log"
    stub = stub_dir / "systemctl"
    stub.write_text(f"""#!/bin/sh
echo "$*" >> {calls}
case "$2" in
  is-enabled) if [ -f {unit_dir}/trk-pages.timer ]; then echo enabled; else echo not-found; exit 1; fi ;;
  is-active) if [ -f {unit_dir}/trk-pages.timer ]; then echo active; else echo inactive; exit 3; fi ;;
  fail-me) exit 1 ;;
esac
exit 0
""")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(timer, "trk_path", lambda: "/opt/trk bin/trk")

    def log():
        return calls.read_text().splitlines() if calls.exists() else []

    return unit_dir, log


def test_timer_install_writes_and_enables_the_pair(trk, trk_home, units):
    unit_dir, log = units
    result = trk("pages", "timer", "install", "--json")
    assert result.code == 0, result.err
    service = (unit_dir / "trk-pages.service").read_text()
    timer_unit = (unit_dir / "trk-pages.timer").read_text()
    assert 'ExecStart="/opt/trk bin/trk" pages tick' in service.splitlines()
    assert f"Environment=TRK_HOME={trk_home}" in service.splitlines()
    assert "Type=oneshot" in service.splitlines()
    assert "OnUnitActiveSec=60s" in timer_unit.splitlines()
    assert "WantedBy=timers.target" in timer_unit.splitlines()
    assert log()[:2] == ["--user daemon-reload", "--user enable --now trk-pages.timer"]
    assert result.json() == {
        "schema": 1, "unit_dir": str(unit_dir), "installed": True, "enabled": "enabled", "active": "active",
        "exec_start": '"/opt/trk bin/trk" pages tick', "trk_home": str(trk_home)}


def test_timer_status_and_remove(trk, trk_home, units):
    unit_dir, log = units
    assert trk("pages", "timer", "status", "--json").json()["installed"] is False
    assert trk("pages", "timer", "install").code == 0
    assert trk("pages", "timer", "status", "--json").json()["installed"] is True
    before = len(log())
    result = trk("pages", "timer", "remove", "--json")
    assert result.code == 0, result.err
    assert result.json()["removed"] == ["trk-pages.service", "trk-pages.timer"]
    assert result.json()["installed"] is False
    assert not (unit_dir / "trk-pages.service").exists() and not (unit_dir / "trk-pages.timer").exists()
    assert log()[before:before + 2] == ["--user disable --now trk-pages.timer", "--user daemon-reload"]
    again = trk("pages", "timer", "remove", "--json")
    assert again.code == 0 and again.json()["removed"] == []


def test_timer_quotes_and_resolves_trk_home(trk, units, monkeypatch, tmp_path):
    unit_dir, _ = units
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRK_HOME", "my $store")
    assert trk("pages", "timer", "install").code == 0
    assert f'Environment="TRK_HOME={tmp_path}/my $store"' in (unit_dir / "trk-pages.service").read_text().splitlines()


def test_timer_without_trk_home_leaves_the_environment_out(trk, units, monkeypatch):
    unit_dir, _ = units
    monkeypatch.delenv("TRK_HOME")
    monkeypatch.setattr(store, "guard_default", lambda directory, doing: None)  # as if run by the installed trk
    assert trk("pages", "timer", "install").code == 0
    assert "Environment=" not in (unit_dir / "trk-pages.service").read_text()


@pytest.fixture
def agents(tmp_path, monkeypatch):
    """macOS: a temp LaunchAgents folder and a stub `launchctl` that logs its arguments and reports the
    agent loaded while its plist exists."""
    folder = tmp_path / "LaunchAgents"
    monkeypatch.setenv("TRK_LAUNCH_AGENTS", str(folder))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(timer, "launchd", lambda: True)
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    calls = tmp_path / "launchctl.log"
    stub = stub_dir / "launchctl"
    stub.write_text(f"""#!/bin/sh
echo "$*" >> {calls}
case "$1" in print) [ -f {folder}/com.trk.pages.plist ] || exit 113 ;; esac
exit 0
""")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(timer, "trk_path", lambda: "/opt/trk bin/trk")

    def log():
        return calls.read_text().splitlines() if calls.exists() else []

    return folder, log


def test_on_macos_the_timer_is_a_launchd_agent(trk, trk_home, agents, tmp_path):
    import plistlib
    folder, log = agents
    result = trk("pages", "timer", "install", "--json")
    assert result.code == 0, result.err
    agent = plistlib.loads((folder / "com.trk.pages.plist").read_bytes())
    assert agent["Label"] == "com.trk.pages" and agent["StartInterval"] == 60
    assert agent["ProgramArguments"] == ["/opt/trk bin/trk", "pages", "tick"]
    assert agent["EnvironmentVariables"]["TRK_HOME"] == str(trk_home)
    assert agent["EnvironmentVariables"]["PATH"].startswith("/opt/trk bin" + os.pathsep)
    assert agent["StandardOutPath"] == str(tmp_path / "state/trk/pages-timer.log")
    uid = os.getuid()
    assert log()[:2] == [f"bootout gui/{uid}/com.trk.pages", f"bootstrap gui/{uid} {folder}/com.trk.pages.plist"]
    assert result.json() == {
        "schema": 1, "unit_dir": str(folder), "installed": True, "enabled": "enabled", "active": "active",
        "exec_start": "'/opt/trk bin/trk' pages tick", "trk_home": str(trk_home)}
    assert "timer com.trk.pages.plist in" in trk("pages", "timer", "status").out


def test_on_macos_remove_unloads_and_deletes_the_agent(trk, trk_home, agents):
    folder, log = agents
    assert trk("pages", "timer", "install").code == 0
    before = len(log())
    result = trk("pages", "timer", "remove", "--json")
    assert result.code == 0, result.err
    assert result.json()["removed"] == ["com.trk.pages.plist"] and result.json()["installed"] is False
    assert result.json()["active"] == "inactive" and not (folder / "com.trk.pages.plist").exists()
    assert log()[before] == f"bootout gui/{os.getuid()}/com.trk.pages"
    assert trk("pages", "timer", "remove", "--json").json()["removed"] == []


def test_on_macos_a_test_never_touches_the_real_launch_agents(trk, trk_home, agents, monkeypatch):
    monkeypatch.delenv("TRK_LAUNCH_AGENTS")
    result = trk("pages", "timer", "install")
    assert result.code != 0 and "TRK_LAUNCH_AGENTS" in result.err


def test_a_failing_systemctl_is_refused(trk, units, monkeypatch):
    real = timer.systemctl  # every call made as `systemctl --user fail-me`, which the stub fails
    monkeypatch.setattr(timer, "systemctl", lambda *argv, check=True: real("fail-me", check=check))
    result = trk("pages", "timer", "install", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "systemctl"


def test_a_foreign_build_without_trk_home_will_not_install(trk, units, monkeypatch):
    unit_dir, log = units
    monkeypatch.delenv("TRK_HOME")
    monkeypatch.setattr(store, "running_source", lambda: Path("/not/the/installed/trk"))
    result = trk("pages", "timer", "install", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "foreign-build"
    assert not unit_dir.exists() and log() == []


def test_tests_cannot_reach_the_default_unit_directory(trk, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    result = trk("pages", "timer", "install", "--json")
    assert result.code == 2
    assert result.error()["rule"] == "test-units"


# ----- a real detached flusher, end to end ------------------------------------------------------------


def test_a_write_starts_a_real_detached_flusher_that_publishes_and_exits(trk, trk_home, tree, tmp_path, monkeypatch):
    """The detached process runs the adapter as a child of its own, so this one publishes with the folder adapter."""
    out = tmp_path / "pages"
    ok(trk, "pages", "setup", "--adapter", shlex.join([sys.executable, "-m", "trk.folder", "--dir", str(out)]),
       "--home", tree["P"])
    monkeypatch.setattr(spawn, "launch", REAL_LAUNCH)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")])))
    section_set(trk, tree["T"], "Detached.\n")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if len(list(out.glob("*.html"))) == 5 and not (trk_home / spawn.PID_NAME).exists():
            break
        time.sleep(0.1)
    assert len(list(out.glob("*.html"))) == 5, (trk_home / spawn.LOG_NAME).read_text()
    assert not (trk_home / spawn.PID_NAME).exists()
    assert (trk_home / spawn.LOG_NAME).exists()


