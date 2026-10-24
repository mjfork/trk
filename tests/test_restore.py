"""tr-acf0s: the store epoch in the feed, and `trk restore`, which rotates it.

A restore rewinds the journal, so seqs are reused. The epoch is how a feed consumer tells: it keeps
(epoch, seq), and a changed epoch means rebuild from --since 0 (ADR 0002, point 2).
"""

import hashlib
import sqlite3

from gated import DEST
from trk import store


def feed(trk):
    return trk("changes", "--since", "0", "--json").json()


def epoch_in(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_init_gives_the_store_an_epoch_the_feed_carries(trk, trk_home):
    trk("init", "--me", "michael")
    epoch = feed(trk)["epoch"]
    assert isinstance(epoch, str) and len(epoch) == 36
    trk("new", "program", "P", *DEST)
    assert feed(trk)["epoch"] == epoch == epoch_in(trk_home / "store.db")


def test_two_stores_have_different_epochs(trk, trk_home, tmp_path, monkeypatch):
    trk("init", "--me", "michael")
    first = feed(trk)["epoch"]
    monkeypatch.setenv("TRK_HOME", str(tmp_path / "other"))
    trk("init", "--me", "michael")
    assert feed(trk)["epoch"] != first


def test_a_store_without_an_epoch_gets_one_on_first_open_with_no_journal_row(trk, trk_home):
    trk("init", "--me", "michael")
    conn = sqlite3.connect(trk_home / "store.db")
    conn.execute("DELETE FROM meta WHERE key = 'epoch'")
    conn.commit()
    head = conn.execute("SELECT MAX(seq) FROM journal").fetchone()[0]
    conn.close()

    response = feed(trk)
    assert response["epoch"] == epoch_in(trk_home / "store.db") is not None
    assert response["head"] == head
    assert feed(trk)["epoch"] == response["epoch"]


def test_restore_over_a_newer_store_rewinds_head_and_rotates_the_epoch(trk, trk_home, tmp_path):
    trk("init", "--me", "michael")
    program = trk("new", "program", "Kept", *DEST).out.strip()
    backup = tmp_path / "b.db"
    trk("backup", str(backup))
    before = feed(trk)
    later = trk("new", "program", "Withdrawn", *DEST).out.strip()
    assert feed(trk)["head"] > before["head"]
    backup_sha = sha(backup)

    result = trk("restore", str(backup), "--json")
    assert result.code == 0, result.err
    restored = result.json()
    assert restored["schema"] == 1 and restored["restored"] == str(backup)
    assert restored["head"] == before["head"]
    assert restored["epoch"] != before["epoch"]

    after = feed(trk)
    assert after["head"] == before["head"]
    assert after["epoch"] == restored["epoch"]
    assert (trk_home / "feed.head").read_text() == f"{before['head']}\n"
    assert trk("show", program).code == 0
    assert trk("show", later).code == 3
    # Reading the backup never changed it: it still carries the old epoch.
    assert sha(backup) == backup_sha and epoch_in(backup) == before["epoch"]


def test_writes_after_a_restore_reuse_seqs_under_the_new_epoch(trk, tmp_path):
    trk("init", "--me", "michael")
    backup = tmp_path / "b.db"
    trk("backup", str(backup))
    head = feed(trk)["head"]
    trk("new", "program", "Withdrawn", *DEST)
    withdrawn_epoch = feed(trk)["epoch"]

    trk("restore", str(backup))
    trk("new", "program", "Fresh", *DEST)
    response = feed(trk)
    fresh = next(row for row in response["changes"] if row["change"] == "item.created" and row["title"] == "Fresh")
    assert fresh["seq"] == head + 1
    assert response["epoch"] != withdrawn_epoch


def test_restoring_a_missing_backup_is_not_found_and_changes_nothing(trk, trk_home, tmp_path):
    trk("init", "--me", "michael")
    before = feed(trk)
    result = trk("restore", str(tmp_path / "nope.db"), "--json")
    assert result.code == 3
    assert result.error()["rule"] == "no-backup"
    assert feed(trk) == before


def test_restoring_a_backup_newer_than_the_live_store(trk, trk_home, tmp_path):
    trk("init", "--me", "michael")
    old = tmp_path / "old.db"
    trk("backup", str(old))
    trk("new", "program", "Later", *DEST)
    new = tmp_path / "new.db"
    trk("backup", str(new))
    new_head = feed(trk)["head"]

    first = trk("restore", str(old), "--json").json()
    assert first["head"] < new_head
    second = trk("restore", str(new), "--json").json()
    assert second["head"] == new_head
    assert second["epoch"] not in (first["epoch"], epoch_in(new))
    assert [c["title"] for c in feed(trk)["changes"] if c["change"] == "item.created"] == ["Later"]


def test_a_backup_with_a_newer_schema_is_refused(trk, trk_home, tmp_path):
    trk("init", "--me", "michael")
    backup = tmp_path / "b.db"
    trk("backup", str(backup))
    conn = sqlite3.connect(backup)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(store.SCHEMA_VERSION + 1),))
    conn.commit()
    conn.close()
    before = feed(trk)
    result = trk("restore", str(backup), "--json")
    assert result.code == 1 and result.error()["rule"] == "schema-newer"
    assert feed(trk) == before


def test_a_file_that_is_not_a_store_is_refused(trk, tmp_path):
    trk("init", "--me", "michael")
    before = feed(trk)
    junk = tmp_path / "junk.db"
    junk.write_text("not sqlite at all, just text long enough to have a header" * 4)
    result = trk("restore", str(junk), "--json")
    assert result.code == 1 and result.error()["rule"] == "not-a-store"
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert trk("restore", str(empty), "--json").error()["rule"] == "not-a-store"
    assert feed(trk) == before


def test_restoring_the_live_store_onto_itself_is_a_usage_error(trk, trk_home):
    trk("init", "--me", "michael")
    assert trk("restore", str(trk_home / "store.db")).code == 2


def test_a_backup_from_before_epochs_is_given_a_fresh_one(trk, trk_home, tmp_path):
    trk("init", "--me", "michael")
    backup = tmp_path / "b.db"
    trk("backup", str(backup))
    conn = sqlite3.connect(backup)
    conn.execute("DELETE FROM meta WHERE key = 'epoch'")
    conn.commit()
    conn.close()
    result = trk("restore", str(backup), "--json")
    assert result.code == 0
    assert feed(trk)["epoch"] == result.json()["epoch"]


def test_restore_creates_a_missing_live_store(trk, trk_home, tmp_path, monkeypatch):
    trk("init", "--me", "michael")
    program = trk("new", "program", "P", *DEST).out.strip()
    backup = tmp_path / "b.db"
    trk("backup", str(backup))
    monkeypatch.setenv("TRK_HOME", str(tmp_path / "fresh"))
    result = trk("restore", str(backup))
    assert result.code == 0
    assert trk("show", program).code == 0


def test_plain_output_names_the_backup_head_and_epoch(trk, tmp_path):
    trk("init", "--me", "michael")
    backup = tmp_path / "b.db"
    trk("backup", str(backup))
    result = trk("restore", str(backup))
    epoch = feed(trk)["epoch"]
    assert result.out.strip() == f"restored {backup}: head {feed(trk)['head']}, epoch {epoch}"


def test_restore_leaves_nothing_beside_the_backup(trk, tmp_path):
    trk("init", "--me", "michael")
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    backup = shelf / "b.db"
    trk("backup", str(backup))
    assert trk("restore", str(backup)).code == 0
    assert sorted(p.name for p in shelf.iterdir()) == ["b.db"]


def test_a_backup_with_a_wal_beside_it_is_refused(trk, tmp_path):
    trk("init", "--me", "michael")
    backup = tmp_path / "b.db"
    trk("backup", str(backup))
    (tmp_path / "b.db-wal").write_bytes(b"uncheckpointed frames")
    before = feed(trk)
    result = trk("restore", str(backup), "--json")
    assert result.code == 1 and result.error()["rule"] == "backup-has-wal"
    assert feed(trk) == before


def test_a_meta_table_without_a_schema_version_is_not_a_store(trk, tmp_path):
    trk("init", "--me", "michael")
    for value in (None, "two"):
        odd = tmp_path / f"odd-{value}.db"
        conn = sqlite3.connect(odd)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if value:
            conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (value,))
        conn.commit()
        conn.close()
        result = trk("restore", str(odd), "--json")
        assert result.code == 1 and result.error()["rule"] == "not-a-store", result.err
