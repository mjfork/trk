"""Migrations. AC 9 (Asks and owners): a store the Core (schema 1) CLI wrote opens under this build,
migrated through every schema to the current one. Handoffs: the same store written by the Asks and
owners (schema 2) CLI. Stage gates: a store the Handoffs (schema 3) CLI wrote gets its written
criteria as rows and its open tickets' stages.

The fixture in tests/fixtures/schema1/ was built by build.py with the Core CLI at the commit in
BUILT_FROM. It holds an ownerless top-level program P (created by worker) over project J with
tickets A, B and C, an owned program O, the edge A blocks B, C closed, a lapsed claim on A
(swept at 14:00) and a live one (session s-live, until 22:01). tests/fixtures/schema3/ is described
in its build.py.
"""

import io
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from feed_contract import feed_validator
from gated import DEST
from trk import store

FIXTURE = Path(__file__).parent / "fixtures" / "schema1"
OPENED_AT = "2026-10-10T14:30:00.000Z"  # after the sweep, inside the live lease

V1_COLUMNS = ("seq", "at", "change", "item", "kind", "form", "title", "parent", "root", "depth", "actor",
              "session", "owner", "status_old", "status_new", "data")


def query(home, statement, *params):
    conn = sqlite3.connect(home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(statement, params).fetchall()
    finally:
        conn.close()


def v1_journal(home):
    """Every journal row, byte for byte, in the columns schema 1 had."""
    return [tuple(r) for r in query(home, f"SELECT {', '.join(V1_COLUMNS)} FROM journal ORDER BY seq")]


def ids(home):
    """The fixture's items by title."""
    return {r["title"]: r["id"] for r in query(home, "SELECT id, title FROM items")}


@pytest.fixture
def v1_store(trk_home, monkeypatch):
    trk_home.mkdir()
    for name in ("store.db", "feed.head"):
        shutil.copyfile(FIXTURE / name, trk_home / name)
    monkeypatch.setenv("TRK_NOW", OPENED_AT)
    return trk_home


def test_the_fixture_is_a_checkpointed_schema_1_store_built_by_core():
    assert len((FIXTURE / "BUILT_FROM").read_text().strip()) == 40
    assert not any(FIXTURE.glob("store.db-*"))
    conn = sqlite3.connect(f"file:{FIXTURE / 'store.db'}?immutable=1", uri=True)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == ("1",)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'roles'").fetchone()
        assert conn.execute("SELECT count(*) FROM items WHERE parent_id IS NULL AND owner IS NULL").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM items WHERE status = 'closed'").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM edges").fetchone() == (1,)
        assert [tuple(r) for r in conn.execute('SELECT session, "end" FROM claims ORDER BY id')] == [
            ("s-lapsed", "expired"), ("s-live", None),
        ]
    finally:
        conn.close()


def test_ac9_a_schema_1_store_opens_at_the_current_schema(trk, v1_store):
    old_rows = v1_journal(v1_store)
    old_head = old_rows[-1][0]
    item = ids(v1_store)

    response = trk("changes", "--since", "0", "--json").json()  # the first open migrates

    assert query(v1_store, "SELECT value FROM meta WHERE key = 'schema_version'")[0][0] == str(store.SCHEMA_VERSION)
    assert query(v1_store, "SELECT count(*) FROM handoffs")[0][0] == 0
    assert query(v1_store, "SELECT value FROM meta WHERE key = 'default_owner'")[0][0] == "michael"
    assert [tuple(r) for r in query(v1_store, "SELECT role, actor FROM roles ORDER BY role")] == [
        ("engineering", "michael"), ("product", "michael"), ("publisher", "michael"),
    ]
    # The old rows are untouched; the migration's rows follow them in step order.
    assert v1_journal(v1_store)[:len(old_rows)] == old_rows
    new_rows = response["changes"][len(old_rows):]
    assert [(r["seq"], r["change"], r["item"], r["actor"], r["at"]) for r in new_rows] == [
        (old_head + 1, "config.changed", None, "michael", OPENED_AT),
        (old_head + 2, "role.set", None, "michael", OPENED_AT),
        (old_head + 3, "role.set", None, "michael", OPENED_AT),
        (old_head + 4, "owner.changed", item["Ownerless program"], "michael", OPENED_AT),
        # Schema 4: the open tickets, which wrote no sections, are Framed.
        (old_head + 5, "stage.changed", item["Ticket A"], "michael", OPENED_AT),
        (old_head + 6, "stage.changed", item["Ticket B"], "michael", OPENED_AT),
        # Schema 5: the publisher agent and its role.
        (old_head + 7, "actor.added", None, "michael", OPENED_AT),
        (old_head + 8, "role.set", None, "michael", OPENED_AT),
    ]
    assert [r["data"] for r in new_rows] == [
        {"key": "default-owner", "old": None, "new": "michael"},
        {"role": "product", "old": None, "new": "michael"},
        {"role": "engineering", "old": None, "new": "michael"},
        {"old": None, "new": "michael", "reason": "migration"},
        {"old": None, "new": "framed", "via": "migration"},
        {"old": None, "new": "framed", "via": "migration"},
        {"id": "publisher", "kind": "agent", "display": "publisher"},
        {"role": "publisher", "old": None, "new": "michael"},
    ]
    assert (new_rows[3]["owner"], new_rows[3]["root"], new_rows[3]["depth"]) == ("michael", item["Ownerless program"], 0)
    # Every row replays, old ones with a null addressee, and validates against the contract.
    assert response["head"] == old_head + 8 == int((v1_store / "feed.head").read_text())
    assert [r["seq"] for r in response["changes"]] == list(range(1, old_head + 9))
    assert all(r["addressee"] is None for r in response["changes"])
    assert list(feed_validator().iter_errors(response)) == []


def test_after_migration_every_top_level_item_has_its_own_owner_and_the_rest_is_intact(trk, v1_store):
    item = ids(v1_store)
    trk("changes", "--since", "0")
    owners = {r["title"]: r["owner"] for r in query(v1_store, "SELECT title, owner FROM items")}
    assert owners == {"Ownerless program": "michael", "Owned program": "worker", "Project J": None,
                      "Ticket A": None, "Ticket B": None, "Ticket C": None}
    a = trk("show", item["Ticket A"], "--json").json()["item"]
    assert a["effective_owner"] == "michael"
    assert a["claim"]["session"] == "s-live"  # the live claim survived the open's sweep
    assert [(e["type"], e["id"]) for e in a["edges"]] == [("blocks", item["Ticket B"])]
    c = trk("show", item["Ticket C"], "--json").json()["item"]
    assert (c["resolution"], c["stage"]) == ("done", None)


def test_the_migrated_store_takes_writes_and_new_verbs(trk, v1_store):
    trk("changes", "--since", "0")
    assert trk("roles", "set", "product", "worker").code == 0
    p = trk("new", "program", "After", "--json", *DEST).json()["item"]
    assert p["owner"] == "michael"
    result = trk("edit", ids(v1_store)["Ownerless program"], "--no-owner", "--json")
    assert result.error()["rule"] == "owner-required"


def test_a_second_open_migrates_nothing(trk, v1_store):
    trk("changes", "--since", "0")
    head = int((v1_store / "feed.head").read_text())
    trk("changes", "--since", "0")
    assert query(v1_store, "SELECT max(seq) FROM journal")[0][0] == head


@pytest.mark.feed_unchecked  # the store stays at schema 1, whose rows predate `addressee`
def test_a_failing_migration_leaves_the_store_at_schema_1(trk, v1_store, monkeypatch):
    old_rows = v1_journal(v1_store)

    def broken(tx):
        store._schema_1_to_2(tx)
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "MIGRATIONS", {1: broken})
    with pytest.raises(RuntimeError):
        trk("changes", "--since", "0")
    assert query(v1_store, "SELECT value FROM meta WHERE key = 'schema_version'")[0][0] == "1"
    assert not query(v1_store, "SELECT 1 FROM sqlite_master WHERE name = 'roles'")
    assert v1_journal(v1_store) == old_rows
    assert (v1_store / "feed.head").read_text().strip() == str(old_rows[-1][0])


@pytest.mark.feed_unchecked  # the store stays at schema 1, whose rows predate `addressee`
def test_a_failing_later_step_rolls_back_the_earlier_ones_too(trk, v1_store, monkeypatch):
    old_rows = v1_journal(v1_store)

    def broken(tx):
        store._schema_2_to_3(tx)
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "MIGRATIONS", {**store.MIGRATIONS, 2: broken})
    with pytest.raises(RuntimeError):
        trk("changes", "--since", "0")
    assert query(v1_store, "SELECT value FROM meta WHERE key = 'schema_version'")[0][0] == "1"
    assert not query(v1_store, "SELECT 1 FROM sqlite_master WHERE name IN ('roles', 'handoffs')")
    assert v1_journal(v1_store) == old_rows


# --- schema 2 -> 3: handoffs ------------------------------------------------------------------
# tests/fixtures/schema2/ is the same store built by the Asks and owners (schema 2) CLI.

FIXTURE_2 = Path(__file__).parent / "fixtures" / "schema2"


@pytest.fixture
def v2_store(trk_home, monkeypatch):
    trk_home.mkdir()
    for name in ("store.db", "feed.head"):
        shutil.copyfile(FIXTURE_2 / name, trk_home / name)
    monkeypatch.setenv("TRK_NOW", OPENED_AT)
    return trk_home


def journal_rows(home):
    """Every journal row in the columns schema 8 had (`client` came with schema 9, null on old rows)."""
    return [tuple(r) for r in query(
        home, "SELECT seq, at, change, item, kind, form, title, parent, root, depth, actor, session, owner,"
              " status_old, status_new, data, addressee FROM journal ORDER BY seq")]


def test_the_schema_2_fixture_is_a_checkpointed_store_without_handoffs():
    assert len((FIXTURE_2 / "BUILT_FROM").read_text().strip()) == 40
    assert not any(FIXTURE_2.glob("store.db-*"))
    conn = sqlite3.connect(f"file:{FIXTURE_2 / 'store.db'}?immutable=1", uri=True)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == ("2",)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'roles'").fetchone()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'handoffs'").fetchone()
    finally:
        conn.close()


def test_a_schema_2_store_opens_with_handoffs_and_only_the_stage_rows_new(trk, v2_store):
    old_rows = journal_rows(v2_store)
    response = trk("changes", "--since", "0", "--json").json()
    assert query(v2_store, "SELECT value FROM meta WHERE key = 'schema_version'")[0][0] == str(store.SCHEMA_VERSION)
    assert query(v2_store, "SELECT count(*) FROM handoffs")[0][0] == 0
    # Schema 3 journals nothing; schema 4 gives the open tickets A and B their stage; schema 5 adds the
    # publisher agent and its role.
    assert journal_rows(v2_store)[:len(old_rows)] == old_rows
    new = response["changes"][len(old_rows):]
    assert [(r["change"], r["title"]) for r in new] == [("stage.changed", "Ticket A"), ("stage.changed", "Ticket B"),
                                                        ("actor.added", None), ("role.set", None)]
    assert response["head"] == old_rows[-1][0] + 4 == int((v2_store / "feed.head").read_text())
    assert list(feed_validator().iter_errors(response)) == []


def test_the_migrated_store_keeps_handoffs(trk, v2_store, monkeypatch):
    a = ids(v2_store)["Ticket A"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("Checkpoint after the migration.\n"))
    assert trk("handoff", "add", a, "--session", "s-live", "--as", "worker").code == 0
    assert trk("handoff", "show", a).out == "Checkpoint after the migration.\n"
    item = trk("show", a, "--json").json()["item"]
    assert item["handoff"]["first_line"] == "Checkpoint after the migration."
    assert item["claim"]["session"] == "s-live"
    last = trk("changes", "--since", "0", "--json").json()["changes"][-1]
    assert (last["change"], last["item"], last["session"]) == ("handoff.added", a, "s-live")


# --- schema 3 -> 4: Stage gates ---------------------------------------------------------------

FIXTURE_3 = Path(__file__).parent / "fixtures" / "schema3"
OPENED_3 = "2026-10-10T13:00:00.000Z"


@pytest.fixture
def v3_store(trk_home, monkeypatch):
    trk_home.mkdir()
    for name in ("store.db", "feed.head"):
        shutil.copyfile(FIXTURE_3 / name, trk_home / name)
    monkeypatch.setenv("TRK_NOW", OPENED_3)
    return trk_home


def test_the_schema_3_fixture_is_a_checkpointed_store_built_by_handoffs():
    assert len((FIXTURE_3 / "BUILT_FROM").read_text().strip()) == 40
    assert not any(FIXTURE_3.glob("store.db-*"))
    conn = sqlite3.connect(f"file:{FIXTURE_3 / 'store.db'}?immutable=1", uri=True)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == ("3",)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'criteria'").fetchone()
    finally:
        conn.close()


def criteria(trk, item_id):
    return [(c["handle"], c["text"], c["version"], c["retired"]) for c in trk("criteria", item_id, "--json").json()["criteria"]]


def test_written_criteria_become_rows_and_open_tickets_get_stages(trk, v3_store):
    old_rows = v1_journal(v3_store)
    old_head = old_rows[-1][0]
    item = ids(v3_store)
    old_text = {title: query(v3_store, "SELECT body FROM sections WHERE item_id = ? AND handle = ?", item[title],
                             "destination" if title == "P" else "acceptance-criteria")
                for title in ("P", "J", "A", "C", "E")}

    response = trk("changes", "--since", str(old_head), "--json").json()  # the first open migrates

    assert query(v3_store, "SELECT value FROM meta WHERE key = 'schema_version'")[0][0] == str(store.SCHEMA_VERSION)
    assert v1_journal(v3_store)[:len(old_rows)] == old_rows  # the journal's old rows are untouched
    rows = response["changes"][:-2]  # the last two are schema 5's: the publisher agent and its role
    assert [r["change"] for r in response["changes"][-2:]] == ["actor.added", "role.set"]
    assert {(r["actor"], r["at"]) for r in rows} == {("michael", OPENED_3)}
    assert all(r["data"]["via"] == "migration" for r in rows)
    assert [(r["change"], r["title"]) for r in rows] == [
        ("item.updated", "P"), ("criterion.added", "P"), ("criterion.added", "P"),
        ("item.updated", "J"), ("criterion.added", "J"),
        ("item.updated", "A"), *[("criterion.added", "A")] * 3,
        ("item.updated", "C"), *[("criterion.added", "C")] * 3,
        ("stage.changed", "A"), ("stage.changed", "A"), ("stage.changed", "B"), ("stage.changed", "D"),
    ]
    assert rows[0]["data"] == {"fields": ["section:destination", "section:destination-imported"], "old": {},
                               "new": {}, "via": "migration"}
    assert [r["data"] for r in rows if r["change"] == "stage.changed"] == [
        {"old": None, "new": "framed", "via": "migration"}, {"old": "framed", "new": "specd", "via": "migration"},
        {"old": None, "new": "framed", "via": "migration"}, {"old": None, "new": "framed", "via": "migration"},
    ]
    assert list(feed_validator().iter_errors(response)) == []

    # A destination list, prose criteria and a checklist (nested lines folded into their item).
    assert criteria(trk, item["P"]) == [("ac-1", "Agents find work from data", 1, False),
                                        ("ac-2", "Every write is journaled, and replayable", 1, False)]
    assert criteria(trk, item["J"]) == [("ac-1", "The whole flow works end to end, checked by one run.", 1, False)]
    checklist = [("ac-1", "`trk new` refuses an empty ticket", 1, False),
                 ("ac-2", "the refusal lists what is missing - nested detail", 1, False),
                 ("ac-3", "numbered item", 1, False)]
    assert criteria(trk, item["A"]) == criteria(trk, item["C"]) == checklist
    # The written text is kept under the -imported handle; a decision's section stays as written.
    for title in ("P", "J", "A", "C"):
        imported = "destination-imported" if title == "P" else "acceptance-criteria-imported"
        assert trk("section", "get", item[title], imported).out == old_text[title][0][0]
    assert trk("section", "get", item["E"], "acceptance-criteria").out == old_text["E"][0][0]
    # Stages: A under the accepted project is Spec'd; B (no sections) and D (draft project) are
    # Framed; the closed ticket and everything that is not a ticket have none.
    stages = {title: trk("show", item[title], "--json").json()["item"]["stage"] for title in item}
    assert stages == {"P": None, "J": None, "A": "specd", "B": "framed", "C": None, "E": None, "K": None,
                      "D": "framed"}


def test_the_migrated_store_takes_criterion_writes(trk, v3_store):
    item = ids(v3_store)
    assert trk("criterion", "add", item["A"], "a fourth").out.strip() == "ac-4"
    result = trk("section", "set", item["A"], "acceptance-criteria", "--file", str(FIXTURE_3 / "build.py"), "--json")
    assert result.error()["rule"] == "generated-section"


# --- schema 4 -> 5: Page publisher ------------------------------------------------------------

FIXTURE_4 = Path(__file__).parent / "fixtures" / "schema4"
OPENED_4 = "2026-10-10T13:00:00.000Z"
PAGES_TABLES = ("pages", "page_handles", "page_comments", "page_replies")


@pytest.fixture
def v4_store(trk_home, monkeypatch):
    trk_home.mkdir()
    for name in ("store.db", "feed.head"):
        shutil.copyfile(FIXTURE_4 / name, trk_home / name)
    monkeypatch.setenv("TRK_NOW", OPENED_4)
    return trk_home


def test_the_schema_4_fixture_is_a_checkpointed_store_built_by_stage_gates():
    assert len((FIXTURE_4 / "BUILT_FROM").read_text().strip()) == 40
    assert not any(FIXTURE_4.glob("store.db-*"))
    conn = sqlite3.connect(f"file:{FIXTURE_4 / 'store.db'}?immutable=1", uri=True)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == ("4",)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'criteria'").fetchone()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'pages'").fetchone()
    finally:
        conn.close()


def test_a_schema_4_store_gets_the_page_tables_and_the_publisher(trk, v4_store):
    old_rows = journal_rows(v4_store)
    old_head = old_rows[-1][0]

    response = trk("changes", "--since", str(old_head), "--json").json()  # the first open migrates

    assert query(v4_store, "SELECT value FROM meta WHERE key = 'schema_version'")[0][0] == str(store.SCHEMA_VERSION)
    assert journal_rows(v4_store)[:len(old_rows)] == old_rows
    assert [(r["change"], r["item"], r["actor"], r["at"], r["data"]) for r in response["changes"]] == [
        ("actor.added", None, "michael", OPENED_4, {"id": "publisher", "kind": "agent", "display": "publisher"}),
        ("role.set", None, "michael", OPENED_4, {"role": "publisher", "old": None, "new": "michael"}),
    ]
    assert response["head"] == old_head + 2 == int((v4_store / "feed.head").read_text())
    assert list(feed_validator().iter_errors(response)) == []
    # The bookkeeping tables start empty; the publisher's defaulted settings are in meta.
    for table in PAGES_TABLES:
        assert query(v4_store, f"SELECT count(*) FROM {table}")[0][0] == 0
    meta = dict(tuple(r) for r in query(v4_store, "SELECT key, value FROM meta WHERE key LIKE 'publish%'"))
    assert meta == {"publish_team": "tracker", "publish_renewal_ask": ""}
    assert ("publisher", "michael") in [(r["role"], r["actor"]) for r in trk("roles", "--json").json()["roles"]]
    # The rest is intact: the ask still waits on product.
    assert [a["title"] for a in trk("waiting", "--all", "--json").json()["asks"]] == ["Which way?"]


def test_an_author_id_links_one_actor_at_most(trk, v4_store):
    trk("changes", "--since", "0")
    assert [r["service_user_id"] for r in query(v4_store, "SELECT service_user_id FROM actors")] == [None] * 4
    conn = sqlite3.connect(v4_store / "store.db")
    try:
        conn.execute("UPDATE actors SET service_user_id = 'u-1' WHERE id = 'michael'")
        conn.execute("UPDATE actors SET service_user_id = NULL WHERE id = 'worker'")  # many unlinked actors are fine
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE actors SET service_user_id = 'u-1' WHERE id = 'worker'")
    finally:
        conn.close()


def test_a_fresh_store_has_the_same_page_schema_as_a_migrated_one(trk, v4_store, tmp_path, monkeypatch):
    trk("changes", "--since", "0")
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("TRK_HOME", str(fresh))
    trk("init", "--me", "michael")

    def shape(home):
        return {(r["name"], r["sql"]) for r in query(home, "SELECT name, sql FROM sqlite_master WHERE name LIKE 'page%'"
                                                    " OR name = 'actors_by_service_user'")}

    assert shape(fresh) == shape(v4_store) != set()
    columns = [r["name"] for r in query(fresh, "PRAGMA table_info(actors)")]
    assert columns[-1] == "service_user_id" == [r["name"] for r in query(v4_store, "PRAGMA table_info(actors)")][-1]


def test_an_actor_or_role_already_named_publisher_is_kept(trk, v4_store):
    conn = sqlite3.connect(v4_store / "store.db")
    try:
        with conn:
            conn.execute("INSERT INTO actors (id, kind, display, created_at) VALUES"
                         " ('publisher', 'human', 'Pat Lisher', '2026-10-10T12:30:00.000Z')")
            conn.execute("INSERT INTO roles (role, actor, updated_at) VALUES ('publisher', 'worker', '2026-10-10T12:30:00.000Z')")
    finally:
        conn.close()
    head = journal_rows(v4_store)[-1][0]
    response = trk("changes", "--since", str(head), "--json").json()
    assert response["changes"] == []
    assert trk("show", "--json", "x").code == 3  # the store opened at the current schema
    assert query(v4_store, "SELECT kind, display FROM actors WHERE id = 'publisher'")[0][:] == ("human", "Pat Lisher")
    assert query(v4_store, "SELECT actor FROM roles WHERE role = 'publisher'")[0][0] == "worker"


# ----- schema 6: Needs-you pages ------------------------------------------------------------------------

FIXTURE_5 = Path(__file__).parent / "fixtures" / "schema5"
OPENED_5 = "2026-10-10T13:00:00.000Z"


@pytest.fixture
def v5_store(trk_home, monkeypatch):
    trk_home.mkdir()
    for name in ("store.db", "feed.head"):
        shutil.copyfile(FIXTURE_5 / name, trk_home / name)
    monkeypatch.setenv("TRK_NOW", OPENED_5)
    return trk_home


def test_the_schema_5_fixture_is_a_checkpointed_store_built_by_the_page_publisher():
    assert len((FIXTURE_5 / "BUILT_FROM").read_text().strip()) == 40
    assert not any(FIXTURE_5.glob("store.db-*"))
    conn = sqlite3.connect(f"file:{FIXTURE_5 / 'store.db'}?immutable=1", uri=True)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == ("5",)
        assert conn.execute("PRAGMA foreign_key_list(pages)").fetchall() != []  # what schema 6 drops
    finally:
        conn.close()


def test_a_schema_5_store_keeps_its_page_rows_and_takes_a_person_page(trk, v5_store):
    p = query(v5_store, "SELECT id FROM items WHERE title = 'P'")[0][0]
    conn = sqlite3.connect(v5_store / "store.db")
    try:
        with conn:
            conn.execute("INSERT INTO pages (item_id, slug, url, version, state, published_at, due_at, first_dirty_at, "
                         "last_error, last_error_at) VALUES (?, 's-1', 'https://a/s-1', 3, 'live', '2026-10-10T12:30:00Z',"
                         " '2026-10-10T12:40:00Z', '2026-10-10T12:39:00Z', 'boom', '2026-10-10T12:41:00Z')", (p,))
            conn.executemany("INSERT INTO page_handles (item_id, handle, shell_hash) VALUES (?, ?, ?)",
                             [(p, "@root", "h0"), (p, "waiting", "h1")])
    finally:
        conn.close()
    before = [tuple(r) for r in query(v5_store, "SELECT * FROM pages")], \
        [tuple(r) for r in query(v5_store, "SELECT * FROM page_handles ORDER BY handle")]
    old_head = journal_rows(v5_store)[-1][0]

    response = trk("changes", "--since", str(old_head), "--json").json()  # the first open migrates

    assert query(v5_store, "SELECT value FROM meta WHERE key = 'schema_version'")[0][0] == str(store.SCHEMA_VERSION)
    assert response["changes"] == []  # the step journals nothing
    assert ([tuple(r) for r in query(v5_store, "SELECT * FROM pages")],
            [tuple(r) for r in query(v5_store, "SELECT * FROM page_handles ORDER BY handle")]) == before
    for table in ("pages", "page_handles"):
        assert query(v5_store, f"PRAGMA foreign_key_list({table})") == []
    assert not query(v5_store, "SELECT name FROM sqlite_master WHERE name LIKE '%schema5%'")
    conn = sqlite3.connect(v5_store / "store.db")
    try:
        with conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("INSERT INTO pages (item_id) VALUES ('needs-you:michael')")
            conn.execute("INSERT INTO page_handles (item_id, handle, shell_hash) VALUES ('needs-you:michael', 'x', 'h')")
    finally:
        conn.close()
    assert trk("pages", "render", "needs-you:kim", "--out", "-").out.count('data-comment-id="ask-') == 1


def test_a_fresh_store_has_the_same_page_schema_as_a_schema_5_one_migrated(trk, v5_store, tmp_path, monkeypatch):
    trk("changes", "--since", "0")
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("TRK_HOME", str(fresh))
    trk("init", "--me", "michael")

    def shape(home):
        return {(r["name"], r["sql"]) for r in query(home, "SELECT name, sql FROM sqlite_master WHERE name LIKE 'page%'")}

    assert shape(fresh) == shape(v5_store) != set()


# ----- schema 7: replies to asks (tr-3vgrb) ---------------------------------------------------------------


def test_a_schema_5_store_keeps_its_comment_rows_and_takes_replies(trk, v5_store):
    conn = sqlite3.connect(v5_store / "store.db")
    try:
        with conn:
            conn.execute("INSERT INTO page_comments (comment_id, slug, handle, ask_id, disposition, seen_at) "
                         "VALUES ('c-1', 's-1', 'waiting', NULL, 'which-ask', '2026-10-10T12:30:00.000Z')")
    finally:
        conn.close()
    old_head = journal_rows(v5_store)[-1][0]

    assert trk("changes", "--since", str(old_head), "--json").json()["changes"] == []  # migrates, journals nothing

    assert [tuple(r) for r in query(v5_store, "SELECT * FROM page_comments")] == \
        [("c-1", "s-1", "waiting", None, "which-ask", "2026-10-10T12:30:00.000Z", None)]
    assert not query(v5_store, "SELECT name FROM sqlite_master WHERE name LIKE '%schema6%'")
    conn = sqlite3.connect(v5_store / "store.db")
    try:
        with conn:
            conn.execute("INSERT INTO page_comments (comment_id, slug, handle, ask_id, disposition, seen_at, body) "
                         "VALUES ('c-2', 's-1', 'waiting', NULL, 'reply', '2026-10-10T12:31:00.000Z', 'Metres.')")
    finally:
        conn.close()


# ----- schema 8: comments routed to an owner (tr-yd424) ---------------------------------------------------


def test_a_schema_5_store_takes_the_routed_disposition_and_keeps_its_comment_rows(trk, v5_store):
    conn = sqlite3.connect(v5_store / "store.db")
    try:
        with conn:
            conn.execute("INSERT INTO page_comments (comment_id, slug, handle, ask_id, disposition, seen_at) "
                         "VALUES ('c-1', 's-1', 'header', NULL, 'ignored', '2026-10-10T12:30:00.000Z')")
    finally:
        conn.close()
    assert trk("changes", "--since", str(journal_rows(v5_store)[-1][0]), "--json").json()["changes"] == []
    assert [tuple(r) for r in query(v5_store, "SELECT * FROM page_comments")] == \
        [("c-1", "s-1", "header", None, "ignored", "2026-10-10T12:30:00.000Z", None)]
    assert not query(v5_store, "SELECT name FROM sqlite_master WHERE name LIKE '%schema7%'")
    conn = sqlite3.connect(v5_store / "store.db")
    try:
        with conn:
            conn.execute("INSERT INTO page_comments (comment_id, slug, handle, ask_id, disposition, seen_at, body) "
                         "VALUES ('c-2', 's-1', 'header', NULL, 'routed', '2026-10-10T12:31:00.000Z', 'Why?')")
    finally:
        conn.close()
    assert [tuple(r) for r in query(v5_store, "SELECT disposition FROM page_comments WHERE comment_id = 'c-2'")] == [("routed",)]


# --- schema 12 (UP-0018): items is rebuilt to take form findings, stage reviewed and resolution moot ---

def test_a_migrated_store_has_the_fresh_items_table_and_keeps_every_row(trk, v5_store, tmp_path, monkeypatch):
    before = query(v5_store, "SELECT * FROM items ORDER BY id")
    trk("changes", "--since", "0")
    after = query(v5_store, "SELECT * FROM items ORDER BY id")
    assert [dict(r) for r in after] == [{**dict(r), **{k: None for k in dict(a).keys() - dict(r).keys()}}
                                        for r, a in zip(before, after)]
    assert query(v5_store, "PRAGMA foreign_key_check") == []
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("TRK_HOME", str(fresh))
    trk("init", "--me", "michael")

    def shape(home):
        return {(r["name"], " ".join(r["sql"].replace('"', "").split()))
                for r in query(home, "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'items' AND sql IS NOT NULL")}

    assert shape(fresh) == shape(v5_store) != set()
    assert {r["name"] for r in query(v5_store, "PRAGMA table_info(items)")} == \
        {r["name"] for r in query(fresh, "PRAGMA table_info(items)")}


def test_a_migrated_store_takes_a_findings_item(trk, v5_store):
    trk("changes", "--since", "0")
    program = query(v5_store, "SELECT id FROM items WHERE kind = 'program' AND status = 'open' LIMIT 1")[0]["id"]
    made = trk("new", "work-item", "Audit", "--form", "findings", "--parent", program, "--criterion", "It holds.",
               "--section", "question=@" + str(_written(v5_store)), "--as", "michael")
    assert made.code == 0, made.err
    item = made.out.strip()
    assert tuple(query(v5_store, "SELECT form, stage FROM items WHERE id = ?", item)[0]) == ("findings", "framed")


def _written(home):
    path = home.parent / "question.md"
    path.write_text("Which readers drop keys?\n")
    return path
