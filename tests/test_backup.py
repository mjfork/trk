import errno
import sqlite3

import pytest

from gated import DEST
from trk import store


def test_backup_copy_opens_as_a_store(trk, trk_home, tmp_path, monkeypatch):
    trk("init", "--me", "michael")
    program = trk("new", "program", "P", *DEST).out.strip()
    trk("new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", program)
    head = trk("changes", "--since", "0", "--json").json()["head"]

    copy = tmp_path / "copy" / "store.db"
    copy.parent.mkdir()
    result = trk("backup", str(copy))
    assert result.code == 0
    assert result.out.strip() == str(copy)

    monkeypatch.setenv("TRK_HOME", str(copy.parent))
    shown = trk("show", program, "--json")
    assert shown.code == 0
    assert [c["title"] for c in shown.json()["item"]["children"]] == ["D"]
    assert trk("changes", "--since", "0", "--json").json()["head"] == head
    assert sqlite3.connect(copy).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_json_names_the_copy_and_head(trk, tmp_path):
    trk("init", "--me", "michael")
    head = trk("changes", "--since", "0", "--json").json()["head"]
    copy = tmp_path / "b.db"
    result = trk("backup", str(copy), "--json")
    assert result.code == 0
    assert result.json() == {"schema": 1, "backup": str(copy), "head": head}


def test_backup_refuses_to_overwrite(trk, tmp_path):
    trk("init", "--me", "michael")
    copy = tmp_path / "b.db"
    copy.write_text("keep me")
    result = trk("backup", str(copy), "--json")
    assert result.code == 1
    assert result.error()["rule"] == "backup-exists"
    assert copy.read_text() == "keep me"


def test_backup_into_a_missing_directory_is_a_usage_error(trk, tmp_path):
    trk("init", "--me", "michael")
    result = trk("backup", str(tmp_path / "nope" / "b.db"), "--json")
    assert result.code == 2


def test_backup_without_a_store_exits_3(trk, tmp_path):
    assert trk("backup", str(tmp_path / "b.db")).code == 3


def test_backup_leaves_no_temp_files(trk, tmp_path):
    trk("init", "--me", "michael")
    trk("backup", str(tmp_path / "b.db"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.db", "trk"]


class _BackupFails:
    """A store connection whose online backup raises `error`; everything else is the real connection."""

    def __init__(self, conn, error):
        self._conn, self._error = conn, error

    def backup(self, dest):
        raise self._error

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _backup_raises(monkeypatch, error):
    real_open = store.open_store

    def opened(*args, **kwargs):
        st = real_open(*args, **kwargs)
        st.conn = _BackupFails(st.conn, error)
        return st

    monkeypatch.setattr(store, "open_store", opened)


def test_backup_on_a_locked_store_exits_4_and_leaves_nothing(trk, tmp_path, monkeypatch):
    trk("init", "--me", "michael")
    _backup_raises(monkeypatch, sqlite3.OperationalError("database is locked"))
    result = trk("backup", str(tmp_path / "b.db"))
    assert result.code == 4
    assert "busy timeout" in result.err and result.err.rstrip().endswith("[locked]")
    assert "Traceback" not in result.err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trk"]


def test_backup_on_a_busy_store_names_the_lock_rule_in_json(trk, tmp_path, monkeypatch):
    trk("init", "--me", "michael")
    _backup_raises(monkeypatch, sqlite3.OperationalError("database is busy"))
    result = trk("backup", str(tmp_path / "b.db"), "--json")
    assert result.code == 4 and result.error()["rule"] == "locked"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trk"]


def test_backup_on_another_sqlite_error_is_refused_with_a_json_error(trk, tmp_path, monkeypatch):
    trk("init", "--me", "michael")
    _backup_raises(monkeypatch, sqlite3.DatabaseError("disk I/O error"))
    result = trk("backup", str(tmp_path / "b.db"), "--json")
    assert result.code == 1
    assert result.error()["rule"] == "sqlite" and "disk I/O error" in result.error()["message"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trk"]


@pytest.mark.parametrize("error", [PermissionError(errno.EPERM, "Operation not permitted"),
                                   OSError(errno.EXDEV, "Invalid cross-device link")])
def test_a_failed_link_into_place_is_refused_and_leaves_nothing(trk, tmp_path, monkeypatch, error):
    trk("init", "--me", "michael")

    def link(src, dst):
        raise error

    monkeypatch.setattr(store.os, "link", link)
    result = trk("backup", str(tmp_path / "b.db"), "--json")
    assert result.code == 1
    assert result.error()["rule"] == "backup-failed" and error.strerror in result.error()["message"]
    human = trk("backup", str(tmp_path / "b.db"))
    assert human.code == 1 and human.err.rstrip().endswith("[backup-failed]")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trk"]


def test_a_backup_into_an_unwritable_directory_is_refused(trk, tmp_path, monkeypatch):
    trk("init", "--me", "michael")

    def mkstemp(**kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(tmp_path))

    monkeypatch.setattr(store.tempfile, "mkstemp", mkstemp)
    result = trk("backup", str(tmp_path / "b.db"), "--json")
    assert result.code == 1 and result.error()["rule"] == "backup-failed"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trk"]
