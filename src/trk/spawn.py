"""The spawn check (Page publisher spec: Flusher; Amendments to Core): after a committed write that
journaled a row able to mark pages, start a detached `trk pages flush --wait` unless a live one is
recorded in `<store>/flusher.pid`.

Imported by `store` on every write, so it stays small: stdlib only, and `subprocess` loads only
when a flusher is actually started. A transaction that journals nothing that marks pages (`claim
renew`, config, actors, `page.created`) never gets here, and before `pages setup` the check is one
meta read. The flusher registers and unregisters itself through the same pid file
(`register`/`unregister`, serialized by `flusher.lock`), so two flushers started by racing writes
leave one running.
"""

import os
import sys
from contextlib import contextmanager

PID_NAME = "flusher.pid"
LOCK_NAME = "flusher.lock"  # held only while a flusher registers or unregisters
LOG_NAME = "flusher.log"
LOG_KEEP_BYTES = 1024 * 1024  # a log past this size moves to flusher.log.1 when the next flusher starts
FLUSH_WAIT = ("pages", "flush", "--wait")

# Rows that mark their item and every ancestor (the Affected pages table); `pages.affected` reads these too.
ITEM_ROWS = frozenset({"item.created", "item.closed", "item.reopened", "item.updated", "spec.changed",
                       "stage.changed", "claim.taken", "claim.released", "claim.expired", "handoff.added"})
ITEM_PREFIXES = ("evidence.", "criterion.", "ledger.", "ask.")  # an ask row marks the ask's ancestors
EVERY_ROWS = frozenset({"actor.updated", "role.set"})  # these mark every page
TREE_ROWS = frozenset({"owner.changed", "item.moved", "edge.added", "edge.removed"})


def can_mark(change, item):
    """Whether a journal row of this kind, on this item, can mark a page: the rows `pages.affected`
    maps to a non-empty set (or every page)."""
    if change in EVERY_ROWS:
        return True
    if item is None:
        return False
    return change in ITEM_ROWS or change in TREE_ROWS or change.startswith(ITEM_PREFIXES)


def after_commit(st):
    """The spawn check, run once after a transaction that journaled a marking row has committed.
    It never fails the write: a flusher that cannot start is noted in flusher.log, and the timer
    catches its pages up."""
    try:
        if not (st.meta("publish_backend") or st.meta("publish_base_url")) or live_flusher(st.dir) is not None:
            return
        launch(st.dir)
    except Exception as exc:  # noqa: BLE001 - the write has committed; nothing here may undo or fail it
        _note(st.dir, f"could not start the page flusher: {exc}")


def _pid_path(directory):
    return directory / PID_NAME


def recorded_pid(directory):
    try:
        text = _pid_path(directory).read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def is_flusher(pid):
    """Whether `pid` is alive and its command line runs `pages flush --wait`. Without /proc (not
    Linux), a live pid is taken on trust."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = [part.decode("utf-8", "replace") for part in f.read().split(b"\0") if part]
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return False  # /proc works, so the process is gone
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    except OSError:
        return False
    span = len(FLUSH_WAIT)
    return any(tuple(argv[i:i + span]) == FLUSH_WAIT for i in range(len(argv) - span + 1))


def live_flusher(directory):
    """The pid recorded in flusher.pid when it is a live flusher, else None."""
    pid = recorded_pid(directory)
    return pid if pid is not None and is_flusher(pid) else None


def command():
    """How to run this trk: the same interpreter on the same package, so a worktree's write spawns
    that worktree's flusher and the installed trk its own."""
    return [sys.executable, "-m", "trk"]


def launch(directory):
    """Start `trk pages flush --wait` detached: its own session, stdin from /dev/null, stdout and
    stderr appended to flusher.log. Records the child in flusher.pid (it registers again as itself)
    and returns without waiting."""
    import subprocess

    log = directory / LOG_NAME
    try:
        if log.stat().st_size > LOG_KEEP_BYTES:
            os.replace(log, directory / (LOG_NAME + ".1"))
    except OSError:
        pass
    with open(log, "ab") as out:
        child = subprocess.Popen([*command(), *FLUSH_WAIT], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                 start_new_session=True, close_fds=True)
    register(directory, pid=child.pid)  # at once, so a write before the child registers itself starts none


@contextmanager
def _registry(directory):
    """Serializes register and unregister, so a stale record is replaced by one flusher only."""
    import fcntl

    with open(directory / LOCK_NAME, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def register(directory, pid=None):
    """Record this process as the flusher. False when another live flusher is already recorded:
    the caller exits and leaves the work to it. A stale record (dead pid, or not a flusher) is
    replaced. The pid file appears whole (written aside, then renamed), so the spawn check, which
    reads it without the lock, never sees it empty."""
    pid = os.getpid() if pid is None else pid
    with _registry(directory):
        other = recorded_pid(directory)
        if other is not None and other != pid and is_flusher(other):
            return False
        tmp = directory / f"{PID_NAME}.{pid}.tmp"
        tmp.write_text(f"{pid}\n")
        os.replace(tmp, _pid_path(directory))
        return True


def unregister(directory, pid=None):
    """Remove flusher.pid if it still records this process."""
    pid = os.getpid() if pid is None else pid
    with _registry(directory):
        if recorded_pid(directory) == pid:
            try:
                _pid_path(directory).unlink()
            except FileNotFoundError:
                pass


def _note(directory, message):
    try:
        with open(directory / LOG_NAME, "a", encoding="utf-8") as f:
            f.write(f"trk: {message}\n")
    except OSError:
        pass
