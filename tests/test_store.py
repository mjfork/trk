import sqlite3
import uuid

from trk import store
from gated import DEST


def sql(trk_home, statement, *params):
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        with conn:
            return conn.execute(statement, params).fetchall()
    finally:
        conn.close()


def test_store_lives_in_trk_home_with_wal_and_meta(trk, trk_home):
    trk("init", "--me", "michael", "--prefix", "tr")
    assert sql(trk_home, "PRAGMA journal_mode") == [("wal",)]
    meta = dict(sql(trk_home, "SELECT key, value FROM meta"))
    uuid.UUID(meta.pop("epoch"))  # an opaque uuid
    assert meta == {
        "schema_version": str(store.SCHEMA_VERSION),
        "id_prefix": "tr",
        "default_actor": "michael",
        "default_owner": "michael",
        "lease_minutes": "60",
        "publish_team": "tracker",
        "publish_renewal_ask": "",
    }
    assert (trk_home / "feed.head").exists()


def test_connections_enforce_foreign_keys_and_busy_timeout(trk, trk_home):
    trk("init", "--me", "michael")
    st = store.open_store()
    try:
        assert st.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert st.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
    finally:
        st.close()


def test_schema_has_every_table(trk, trk_home):
    trk("init", "--me", "michael")
    tables = {name for (name,) in sql(trk_home, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"meta", "actors", "items", "item_repos", "sections", "edges", "claims", "journal", "roles"} <= tables


def test_init_creates_schema_3_directly_with_the_ask_and_stage_columns(trk, trk_home):
    trk("init", "--me", "michael")
    columns = {table: [name for (_, name, *_) in sql(trk_home, f"PRAGMA table_info({table})")]
               for table in ("items", "journal", "roles")}
    assert columns["items"][-11:] == ["asker", "stage", "merged_skipped", "red_ask", "client", "worktree_path",
                                      "worktree_branch", "worktree_repo", "worktree_session", "worktree_at", "archive"]
    assert columns["journal"][-2:] == ["addressee", "client"]
    assert columns["roles"] == ["role", "actor", "updated_at"]


def test_newer_schema_is_refused(trk, trk_home):
    trk("init", "--me", "michael")
    sql(trk_home, "UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    result = trk("new", "program", "P", "--json", *DEST)
    assert result.code == 1
    assert result.error()["rule"] == "schema-newer"
    assert sql(trk_home, "SELECT count(*) FROM items") == [(0,)]


def test_older_schema_is_migrated_on_open_in_a_journaled_transaction(trk, trk_home, monkeypatch):
    trk("init", "--me", "michael")
    trk("actor", "add", "kim", "--kind", "human")
    ran = []

    def step(tx):
        ran.append((type(tx).__name__, tx.conn.in_transaction, tx.actor))
        tx.journal("actor.updated", {}, data={"id": "kim", "kind": "human", "display": "Kim"})

    current = store.SCHEMA_VERSION
    monkeypatch.setattr(store, "SCHEMA_VERSION", current + 1)
    monkeypatch.setattr(store, "MIGRATIONS", {current: step})
    item_id = trk("new", "program", "P", "--as", "kim", *DEST).out.strip()
    assert ran == [("Transaction", True, "michael")]  # the default actor writes migration rows
    assert trk("show", item_id).code == 0
    assert sql(trk_home, "SELECT value FROM meta WHERE key = 'schema_version'") == [(str(current + 1),)]
    rows = sql(trk_home, "SELECT seq, change, actor FROM journal ORDER BY seq")
    assert [row[1:] for row in rows[-3:]] == [("actor.updated", "michael"), ("item.created", "kim"),
                                              ("criterion.added", "kim")]
    assert int((trk_home / "feed.head").read_text()) == rows[-1][0]


def test_a_missing_migration_step_refuses_and_leaves_the_version(trk, trk_home):
    trk("init", "--me", "michael")
    sql(trk_home, "UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    result = trk("show", "tr-aaaaa", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "migration-missing"
    assert sql(trk_home, "SELECT value FROM meta WHERE key = 'schema_version'") == [("0",)]


def test_an_init_that_left_an_empty_store_file_can_be_redone(trk, trk_home):
    trk_home.mkdir()
    (trk_home / "store.db").touch()
    result = trk("show", "tr-aaaaa", "--json")
    assert result.code == 3
    assert result.error()["rule"] == "no-store"
    assert trk("init", "--me", "michael").code == 0
    assert trk("new", "program", "P", *DEST).code == 0


def test_test_mode_refuses_the_default_store(trk, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TRK_HOME")
    result = trk("init", "--me", "michael", "--json")
    assert result.code == 2
    assert result.error()["rule"] == "test-store"
    assert not (tmp_path / "home" / ".local").exists()

    monkeypatch.setenv("TRK_HOME", str(tmp_path / "home" / ".local" / "share" / "trk"))
    assert trk("show", "tr-aaaaa").code == 2


def test_fixture_isolates_every_test_from_the_live_store(trk_home):
    assert trk_home.resolve() != store.default_dir().resolve()


def test_lock_timeout_is_a_conflict(trk, trk_home, monkeypatch):
    trk("init", "--me", "michael")
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(trk_home / "store.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        result = trk("new", "program", "P", "--json", *DEST)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert result.code == 4
    assert result.error()["rule"] == "locked"


def test_a_failed_write_commits_nothing(trk, trk_home):
    trk("init", "--me", "michael")
    head_before = (trk_home / "feed.head").read_text()
    st = store.open_store()
    try:
        try:
            with st.transaction("michael") as tx:
                tx.journal("item.updated", {})
                raise RuntimeError("validation failed")
        except RuntimeError:
            pass
    finally:
        st.close()
    assert sql(trk_home, "SELECT count(*) FROM journal WHERE change = 'item.updated'") == [(0,)]
    assert (trk_home / "feed.head").read_text() == head_before
