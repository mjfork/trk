"""The store: location, schema, open/migrate, and the single serialized write path."""

import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import claims, clock, config, ids, roles, spawn  # config and roles import store back; all bind modules only
from .errors import conflict, not_found, refused, usage

SCHEMA_VERSION = 11
DEFAULT_LEASE_MINUTES = 60
BUSY_TIMEOUT_MS = 10_000

DB_NAME = "store.db"
HEAD_NAME = "feed.head"

ACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Function roles that asks are addressed to; `parent` is relative and never stored here.
ROLES_TABLE = """
CREATE TABLE roles (
    role       TEXT PRIMARY KEY,
    actor      TEXT NOT NULL REFERENCES actors (id),
    updated_at TEXT NOT NULL
);
"""

# Checkpoint records kept with an item (handoffs.py); append-only like the journal.
HANDOFFS_TABLE = """
CREATE TABLE handoffs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL REFERENCES items (id),
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL REFERENCES actors (id),
    session    TEXT,
    first_line TEXT NOT NULL,
    body       TEXT NOT NULL
);
CREATE INDEX handoffs_by_item ON handoffs (item_id, id);

CREATE TRIGGER handoffs_no_update BEFORE UPDATE ON handoffs
BEGIN
    SELECT RAISE(ABORT, 'handoffs are append-only');
END;
CREATE TRIGGER handoffs_no_delete BEFORE DELETE ON handoffs
BEGIN
    SELECT RAISE(ABORT, 'handoffs are append-only');
END;
"""

# The complete schema, version 5. Internal spellings use underscores (work_item, spec_bound);
# the CLI and the feed use hyphens. Journal rows store the feed's spellings as written.
SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE actors (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('human', 'agent')),
    display    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    service_user_id TEXT
);

CREATE TABLE items (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('program', 'project', 'work_item')),
    form       TEXT CHECK (form IN ('spec_bound', 'self_contained', 'decision', 'ask')),
    subtype    TEXT CHECK (subtype IN ('research', 'prototype', 'grilling', 'task')),
    title      TEXT NOT NULL,
    parent_id  TEXT REFERENCES items (id),
    rank       TEXT NOT NULL,
    urgent     INTEGER NOT NULL DEFAULT 0 CHECK (urgent IN (0, 1)),
    owner      TEXT REFERENCES actors (id),
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    resolution TEXT CHECK (resolution IN ('done', 'wont_do', 'superseded', 'out_of_scope', 'duplicate')),
    outcome    TEXT,
    spec_state TEXT CHECK (spec_state IN ('draft', 'accepted')),
    jira_url   TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at  TEXT,
    addressee_role TEXT,
    asker      TEXT REFERENCES actors (id),
    stage      TEXT CHECK (stage IN ('framed', 'specd', 'built', 'merged', 'verified')),
    merged_skipped INTEGER NOT NULL DEFAULT 0 CHECK (merged_skipped IN (0, 1)),
    red_ask    TEXT REFERENCES items (id),
    client     TEXT,
    worktree_path    TEXT,
    worktree_branch  TEXT,
    worktree_repo    TEXT,
    worktree_session TEXT,
    worktree_at      TEXT,
    archive    TEXT,
    CHECK ((kind = 'work_item') = (form IS NOT NULL)),
    CHECK ((kind = 'project') = (spec_state IS NOT NULL))
);
CREATE INDEX items_by_parent ON items (parent_id, rank);

CREATE TABLE item_repos (
    item_id TEXT NOT NULL REFERENCES items (id),
    repo    TEXT NOT NULL,
    PRIMARY KEY (item_id, repo)
);

CREATE TABLE sections (
    item_id    TEXT NOT NULL REFERENCES items (id),
    handle     TEXT NOT NULL,
    body       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (item_id, handle)
);

CREATE TABLE edges (
    src        TEXT NOT NULL REFERENCES items (id),
    type       TEXT NOT NULL CHECK (type IN ('blocks', 'relates', 'supersedes')),
    dst        TEXT NOT NULL REFERENCES items (id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (src, type, dst),
    CHECK (src <> dst)
);
CREATE INDEX edges_by_dst ON edges (dst, type);

CREATE TABLE claims (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       TEXT NOT NULL REFERENCES items (id),
    session       TEXT NOT NULL,
    actor         TEXT NOT NULL REFERENCES actors (id),
    lease_minutes INTEGER NOT NULL,
    taken_at      TEXT NOT NULL,
    lease_until   TEXT NOT NULL,
    ended_at      TEXT,
    "end"         TEXT CHECK ("end" IN ('released', 'expired')),
    CHECK ((ended_at IS NULL) = ("end" IS NULL))
);
CREATE UNIQUE INDEX claims_live_by_item ON claims (item_id) WHERE ended_at IS NULL;
CREATE INDEX claims_live_by_session ON claims (session) WHERE ended_at IS NULL;
CREATE INDEX claims_live_by_lease ON claims (lease_until) WHERE ended_at IS NULL;

CREATE TABLE journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    change     TEXT NOT NULL,
    item       TEXT,
    kind       TEXT,
    form       TEXT,
    title      TEXT,
    parent     TEXT,
    root       TEXT,
    depth      INTEGER,
    actor      TEXT NOT NULL,
    session    TEXT,
    owner      TEXT,
    status_old TEXT,
    status_new TEXT,
    data       TEXT NOT NULL DEFAULT '{}',
    addressee  TEXT,
    client     TEXT
);
CREATE INDEX journal_by_root ON journal (root, seq);

CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only');
END;
CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only');
END;
"""

# Stage gates: criterion rows with every version's text, and the append-only evidence and ledger
# tables (written by later verbs; created here so one migration covers them all).
GATES_TABLES = """
CREATE TABLE criteria (
    item_id        TEXT NOT NULL REFERENCES items (id),
    n              INTEGER NOT NULL,
    text           TEXT NOT NULL,
    rank           TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    retired        INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
    retired_reason TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (item_id, n)
);

CREATE TABLE criterion_versions (
    item_id    TEXT NOT NULL REFERENCES items (id),
    n          INTEGER NOT NULL,
    version    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    actor      TEXT NOT NULL REFERENCES actors (id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, n, version)
);

CREATE TABLE evidence (
    id                TEXT PRIMARY KEY,
    item_id           TEXT NOT NULL REFERENCES items (id),
    gate              TEXT NOT NULL,
    criterion_n       INTEGER,
    criterion_version INTEGER,
    kind              TEXT NOT NULL,
    ref               TEXT,
    sha               TEXT,
    verdict           TEXT CHECK (verdict IN ('pass', 'fail')),
    note              TEXT NOT NULL,
    detail            TEXT,
    actor             TEXT NOT NULL REFERENCES actors (id),
    session           TEXT,
    seq               INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX evidence_by_item ON evidence (item_id, seq);

CREATE TABLE ledger (
    item_id          TEXT NOT NULL REFERENCES items (id),
    n                INTEGER NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('fork', 'contradiction', 'reward-hack')),
    decided          TEXT NOT NULL,
    alternatives     TEXT NOT NULL DEFAULT '[]',
    why              TEXT NOT NULL,
    would_have_asked TEXT,
    conflicts_with   TEXT,
    files            TEXT NOT NULL DEFAULT '[]',
    origin           TEXT NOT NULL CHECK (origin IN ('builder', 'auditor')),
    noise            INTEGER NOT NULL DEFAULT 0 CHECK (noise IN (0, 1)),
    repeat_of        TEXT,
    topic            TEXT,
    refers_to        INTEGER,
    actor            TEXT NOT NULL REFERENCES actors (id),
    session          TEXT,
    sha              TEXT,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (item_id, n)
);

CREATE TABLE ledger_verdicts (
    item_id    TEXT NOT NULL REFERENCES items (id),
    n          INTEGER NOT NULL,
    verdict    TEXT NOT NULL CHECK (verdict IN ('sound', 'risky', 'wrong', 'confirmed', 'refuted')),
    note       TEXT NOT NULL,
    sha        TEXT,
    actor      TEXT NOT NULL REFERENCES actors (id),
    session    TEXT,
    seq        INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE ledger_settlements (
    item_id    TEXT NOT NULL REFERENCES items (id),
    n          INTEGER NOT NULL,
    settlement TEXT NOT NULL CHECK (settlement IN ('fixed', 'deferred', 'accepted', 'overruled')),
    sha        TEXT,
    ask        TEXT REFERENCES items (id),
    ticket     TEXT REFERENCES items (id),
    adr        TEXT,
    note       TEXT,
    actor      TEXT NOT NULL REFERENCES actors (id),
    session    TEXT,
    seq        INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE ledger_promotions (
    item_id    TEXT NOT NULL REFERENCES items (id),
    n          INTEGER NOT NULL,
    adr        TEXT NOT NULL,
    actor      TEXT NOT NULL REFERENCES actors (id),
    session    TEXT,
    seq        INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE ledger_asks (
    item_id TEXT NOT NULL REFERENCES items (id),
    n       INTEGER NOT NULL,
    ask_id  TEXT NOT NULL REFERENCES items (id),
    PRIMARY KEY (item_id, n, ask_id)
);
""" + "".join(
    f"""
CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} is append-only');
END;
CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} is append-only');
END;
"""
    for table in ("evidence", "ledger", "ledger_verdicts", "ledger_settlements", "ledger_promotions")
)

# Page publisher: its bookkeeping (a cache of what was sent to the page adapter, never journaled)
# and the actors column that links an actor to a page commenter's author id. A page's key
# (`item_id`) is an item id, or `needs-you:<actor>` for a person's Needs-you page, so neither
# bookkeeping table references items (schema 6 dropped that foreign key).
PAGES_TABLES = """
CREATE TABLE pages (
    item_id        TEXT PRIMARY KEY,
    slug           TEXT,
    url            TEXT,
    version        INTEGER,
    state          TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('publishing', 'live')),
    published_at   TEXT,
    due_at         TEXT,
    first_dirty_at TEXT,
    last_error     TEXT,
    last_error_at  TEXT
);

CREATE TABLE page_handles (
    item_id    TEXT NOT NULL,
    handle     TEXT NOT NULL,
    shell_hash TEXT NOT NULL,
    PRIMARY KEY (item_id, handle)
);

CREATE TABLE page_comments (
    comment_id  TEXT PRIMARY KEY,
    slug        TEXT NOT NULL,
    handle      TEXT,
    ask_id      TEXT REFERENCES items (id),
    disposition TEXT NOT NULL
                CHECK (disposition IN ('answer', 'reply', 'already-answered', 'withdrawn', 'which-ask', 'use-cli',
                                       'ignored', 'routed')),
    seen_at     TEXT NOT NULL,
    body        TEXT
);

CREATE TABLE page_replies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    body       TEXT NOT NULL,
    resolve    INTEGER NOT NULL CHECK (resolve IN (0, 1)),
    state      TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'sent', 'failed')),
    created_at TEXT NOT NULL,
    sent_at    TEXT
);
"""

# SQLite can't add a UNIQUE column, so the actors column gets a partial unique index instead.
SERVICE_USER_INDEX = (
    "CREATE UNIQUE INDEX actors_by_service_user ON actors (service_user_id) WHERE service_user_id IS NOT NULL"
)

PUBLISHER = "publisher"

# Publisher settings with a default; the rest are written by `pages setup` and the publisher itself.
PUBLISH_DEFAULTS = (("publish_team", "tracker"), ("publish_renewal_ask", ""))

# A ledger row withdrawn by a driver (tr-zn1dk): rows are never edited, so the withdrawal is its own
# append-only row, one per ledger row.
LEDGER_WITHDRAWALS_TABLE = """
CREATE TABLE ledger_withdrawals (
    item_id    TEXT NOT NULL REFERENCES items (id),
    n          INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    actor      TEXT NOT NULL REFERENCES actors (id),
    session    TEXT,
    seq        INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, n)
);
CREATE TRIGGER ledger_withdrawals_no_update BEFORE UPDATE ON ledger_withdrawals
BEGIN
    SELECT RAISE(ABORT, 'ledger_withdrawals is append-only');
END;
CREATE TRIGGER ledger_withdrawals_no_delete BEFORE DELETE ON ledger_withdrawals
BEGIN
    SELECT RAISE(ABORT, 'ledger_withdrawals is append-only');
END;
"""

SCHEMA += ROLES_TABLE + HANDOFFS_TABLE + GATES_TABLES + PAGES_TABLES + LEDGER_WITHDRAWALS_TABLE


def _schema_1_to_2(tx):
    """Asks and owners: the role table and ask columns, the default owner, seeded roles, and an
    own owner for every top-level item without one (`owner.changed`, reason `migration`)."""
    for statement in _statements(ROLES_TABLE):
        tx.conn.execute(statement)
    tx.conn.execute("ALTER TABLE items ADD COLUMN addressee_role TEXT")
    tx.conn.execute("ALTER TABLE items ADD COLUMN asker TEXT REFERENCES actors (id)")
    tx.conn.execute("ALTER TABLE journal ADD COLUMN addressee TEXT")
    me = tx.store.meta("default_actor")
    config.seed(tx, me)
    roles.seed(tx, me)
    from . import items  # imported here: items imports names from store, so store cannot import it first

    for row in tx.conn.execute("SELECT id FROM items WHERE parent_id IS NULL AND owner IS NULL ORDER BY rank").fetchall():
        items.set_own_owner(tx, row["id"], me, reason="migration")


def _schema_2_to_3(tx):
    """Handoffs: the append-only handoffs table. It starts empty, so the step journals no rows."""
    for statement in _statements(HANDOFFS_TABLE):
        tx.conn.execute(statement)


def _schema_3_to_4(tx):
    """Stage gates: the stage columns and the gates tables, written criteria imported as rows, and a
    stage for every open ticket.

    Items are visited depth-first in rank order, closed ones included. Every row says `via: migration`.
    """
    tx.conn.execute("ALTER TABLE items ADD COLUMN stage TEXT"
                    " CHECK (stage IN ('framed', 'specd', 'built', 'merged', 'verified'))")
    tx.conn.execute("ALTER TABLE items ADD COLUMN merged_skipped INTEGER NOT NULL DEFAULT 0"
                    " CHECK (merged_skipped IN (0, 1))")
    tx.conn.execute("ALTER TABLE items ADD COLUMN red_ask TEXT REFERENCES items (id)")
    for statement in _statements(GATES_TABLES):
        tx.conn.execute(statement)
    from . import items, stages  # imported here, as items is above

    walk = [row for row, _ in items.outline(tx.conn, include_closed=True)]
    for row in walk:
        _import_criteria(tx, row)
    for row in walk:
        if stages.is_ticket(row) and row["status"] == "open":
            path = stages.set_born(tx.conn, items.get(tx.conn, row["id"]))
            stages.journal_moves(tx, row["id"], None, path, "migration")


def _import_criteria(tx, row):
    """The schema 4 migration: a non-empty written `acceptance-criteria` / `destination` becomes criterion
    rows, and the section moves to `<handle>-imported` (journaled as `item.updated`) so nothing written is lost."""
    from . import criteria, items, sections  # imported here: the hot path (claim renew) loads no gates module

    handle = criteria.generated_handle(row["kind"], row["form"])
    body = sections.body(tx.conn, row["id"], handle) if handle else None
    if body is None or not body.strip():
        return
    imported = f"{handle}-imported"
    sections.delete(tx.conn, row["id"], handle)
    sections.put(tx.conn, row["id"], imported, body, tx.now)
    tx.journal("item.updated", items.feed_fields(tx.conn, row["id"]),
               data={"fields": [f"section:{handle}", f"section:{imported}"], "old": {}, "new": {}, "via": "migration"})
    for text in criteria.parse_written(body):
        criteria.journal_added(tx, row["id"], criteria.insert(tx, row["id"], text), via="migration")


def _schema_4_to_5(tx):
    """Page publisher: the page bookkeeping tables, `actors.service_user_id`, the publisher's settings,
    and the `publisher` agent and function role (held by the default owner)."""
    for statement in _statements(PAGES_TABLES):
        tx.conn.execute(statement)
    tx.conn.execute("ALTER TABLE actors ADD COLUMN service_user_id TEXT")
    seed_publisher(tx, tx.store.meta("default_owner"))


def _schema_5_to_6(tx):
    """Needs-you pages: `pages` and `page_handles` are rebuilt without their foreign key to items, since a
    person's page is keyed `needs-you:<actor>`. Their rows are copied as they are; the step journals none."""
    for table in ("pages", "page_handles"):
        tx.conn.execute(f"ALTER TABLE {table} RENAME TO {table}_schema5")
    for statement in _statements(PAGES_TABLES):
        if statement.lstrip().startswith(("CREATE TABLE pages ", "CREATE TABLE page_handles ")):
            tx.conn.execute(statement)
    for table in ("pages", "page_handles"):
        tx.conn.execute(f"INSERT INTO {table} SELECT * FROM {table}_schema5")
        tx.conn.execute(f"DROP TABLE {table}_schema5")


def _schema_6_to_7(tx):
    """Replies to asks (tr-3vgrb): `page_comments` is rebuilt to take the `reply` disposition and the
    comment's text (`body`, kept for a reply so `trk answer --from-comment` can take it). Its rows are
    copied with no body; the step journals none."""
    tx.conn.execute("ALTER TABLE page_comments RENAME TO page_comments_schema6")
    for statement in _statements(PAGES_TABLES):
        if statement.lstrip().startswith("CREATE TABLE page_comments "):
            tx.conn.execute(statement)
    tx.conn.execute("INSERT INTO page_comments (comment_id, slug, handle, ask_id, disposition, seen_at) "
                    "SELECT comment_id, slug, handle, ask_id, disposition, seen_at FROM page_comments_schema6")
    tx.conn.execute("DROP TABLE page_comments_schema6")


def _schema_7_to_8(tx):
    """Comments routed to an owner (tr-yd424): `page_comments` is rebuilt to take the `routed` disposition.
    Its rows are copied as they are; the step journals none."""
    tx.conn.execute("ALTER TABLE page_comments RENAME TO page_comments_schema7")
    for statement in _statements(PAGES_TABLES):
        if statement.lstrip().startswith("CREATE TABLE page_comments "):
            tx.conn.execute(statement)
    tx.conn.execute("INSERT INTO page_comments SELECT * FROM page_comments_schema7")
    tx.conn.execute("DROP TABLE page_comments_schema7")


def _schema_8_to_9(tx):
    """Client names (tr-fz0az): `items.client` and `journal.client` (the effective client after the change).
    Existing items have no client and existing journal rows keep a null one; the step journals none.
    `_migrate` adds the columns before the first step, since an older step's journal rows and hierarchy
    reads already use them; this step then finds them there."""
    _add_client_columns(tx.conn)


def _add_client_columns(conn):
    for table in ("items", "journal"):
        if not any(col["name"] == "client" for col in conn.execute(f"PRAGMA table_info({table})")):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN client TEXT")


def _schema_9_to_10(tx):
    """Worktree and archive location (tr-zwb4a): `items.worktree_path`, `_branch`, `_repo`, `_session` and
    `_at` (when it was recorded, so `trk where` lists the newest first), and `items.archive`. Existing items
    record neither; the step journals none."""
    columns = {col["name"] for col in tx.conn.execute("PRAGMA table_info(items)")}
    for column in WORKTREE_COLUMNS + ("archive",):
        if column not in columns:
            tx.conn.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")


def _schema_10_to_11(tx):
    """Withdrawn ledger rows (tr-zn1dk): the `ledger_withdrawals` table. No existing row is withdrawn; the
    step journals none."""
    for statement in _statements(LEDGER_WITHDRAWALS_TABLE):
        tx.conn.execute(statement)


WORKTREE_COLUMNS = ("worktree_path", "worktree_branch", "worktree_repo", "worktree_session", "worktree_at")


def seed_publisher(tx, owner):
    """init and the schema 5 migration: the service-user index, the publisher's default settings, the
    `publisher` agent (`actor.added`) and the `publisher` role held by `owner` (`role.set`). An actor
    or role already named `publisher` is kept as it is, with no row."""
    tx.conn.execute(SERVICE_USER_INDEX)
    tx.conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", PUBLISH_DEFAULTS)
    if not tx.conn.execute("SELECT 1 FROM actors WHERE id = ?", (PUBLISHER,)).fetchone():
        add_actor(tx, PUBLISHER, "agent", PUBLISHER)  # an actor someone already registered under the name is kept
    if roles.holder(tx.conn, PUBLISHER) is None:
        roles.set_role(tx, PUBLISHER, owner)


def add_actor(tx, actor_id, kind, display):
    """Insert a registered actor and journal `actor.added` (item null)."""
    tx.conn.execute("INSERT INTO actors (id, kind, display, created_at) VALUES (?, ?, ?, ?)",
                    (actor_id, kind, display, clock.stamp(tx.now)))
    tx.journal("actor.added", {}, data={"id": actor_id, "kind": kind, "display": display})


# MIGRATIONS[v] upgrades a store from schema v to v + 1 inside the write Transaction, so its rows
# are journaled and feed.head is rewritten like any other write.
MIGRATIONS = {1: _schema_1_to_2, 2: _schema_2_to_3, 3: _schema_3_to_4, 4: _schema_4_to_5, 5: _schema_5_to_6,
              6: _schema_6_to_7, 7: _schema_7_to_8, 8: _schema_8_to_9, 9: _schema_9_to_10, 10: _schema_10_to_11}


def default_dir():
    return Path.home() / ".local" / "share" / "trk"


def store_dir():
    """$TRK_HOME, else ~/.local/share/trk/. Under $TRK_TEST=1 the default path is refused."""
    home = os.environ.get("TRK_HOME")
    path = Path(home).expanduser() if home else default_dir()
    if os.environ.get("TRK_TEST") == "1" and path.resolve() == default_dir().resolve():
        raise usage(
            f"$TRK_TEST=1 refuses the default store {default_dir()}; set $TRK_HOME", rule="test-store"
        )
    return path


def home_note():
    """The stderr line naming the store when $TRK_HOME is set, else None."""
    home = os.environ.get("TRK_HOME")
    return f"trk: store {Path(home).expanduser()} (from $TRK_HOME)" if home else None


def uv_tool_dir():
    """Where `uv tool install` puts tool envs: $UV_TOOL_DIR, else $XDG_DATA_HOME/uv/tools, as uv resolves it."""
    if os.environ.get("UV_TOOL_DIR"):
        return Path(os.environ["UV_TOOL_DIR"]).expanduser()
    data = os.environ.get("XDG_DATA_HOME")
    return (Path(data).expanduser() if data else Path.home() / ".local" / "share") / "uv" / "tools"


def installed_sources():
    """The dirs the installed trk imports its package from: each site-packages of the `uv tool` env
    (a regular install) and each path an editable install's trk .pth names. Empty if none is installed."""
    sources = set()
    for site in (uv_tool_dir() / "trk").glob("lib/python*/site-packages"):
        sources.add(site.resolve())
        for pth in site.glob("*trk*.pth"):
            for line in (raw.strip() for raw in pth.read_text().splitlines()):
                if line and not line.startswith(("#", "import")):
                    sources.add((site / line).resolve())  # as `site` reads it: relative to site-packages
    return sources


def running_source():
    """The dir this process imports the trk package from."""
    return Path(__file__).resolve().parent.parent


def guard_default(directory, doing):
    """Refuse (rule `foreign-build`) when `directory` is the default store and this code is not the
    installed trk. A worktree or second checkout that migrated the live store would lock every
    installed build out with `schema-newer`."""
    if directory.resolve() != default_dir().resolve():
        return
    running, sources = running_source(), installed_sources()
    if running in sources:
        return
    installed = ", ".join(str(s) for s in sorted(sources)) or "none found"
    raise refused(
        "foreign-build",
        f"this trk runs from {running}, not the installed trk ({installed}); it will not {doing} "
        f"the default store {directory}. Run the installed trk, or set $TRK_HOME to a scratch store",
    )


def busy_timeout_ms():
    """How long a write waits for the lock: BUSY_TIMEOUT_MS, or $TRK_BUSY_TIMEOUT_MS under $TRK_TEST=1."""
    override = os.environ.get("TRK_BUSY_TIMEOUT_MS")
    if not override or os.environ.get("TRK_TEST") != "1":
        return BUSY_TIMEOUT_MS
    if not override.isdigit() or int(override) < 1:
        raise usage(f"$TRK_BUSY_TIMEOUT_MS is not a positive number of milliseconds: {override!r}")
    return int(override)


def _connect(path):
    timeout_ms = busy_timeout_ms()
    conn = sqlite3.connect(path, isolation_level=None, timeout=timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    return conn


class Store:
    def __init__(self, directory, conn):
        self.dir = directory
        self.conn = conn

    def meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def close(self):
        self.conn.close()

    @contextmanager
    def write_lock(self):
        """BEGIN IMMEDIATE ... COMMIT, rolled back on any failure; a lock timeout exits 4."""
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise sqlite_failure(exc) from exc
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    @contextmanager
    def transaction(self, actor, session=None):
        """One serialized write; feed.head is rewritten before COMMIT if rows were appended. Once it has
        committed, a write that journaled a row able to mark pages runs the spawn check."""
        with self.write_lock():
            tx = Transaction(self, actor, session)
            yield tx
            if tx.head is not None:
                write_head(self.dir, tx.head)
        if tx.marks_pages:
            spawn.after_commit(self)


class Transaction:
    def __init__(self, store, actor, session):
        self.store = store
        self.conn = store.conn
        self.actor = actor
        self.session = session
        self.now = clock.now()
        self.head = None
        self.marks_pages = False  # whether a row journaled here can mark pages (the spawn check)

    def journal(self, change, fields, *, data=None, status_old=None, status_new=None, at=None, session=None,
                actor=None, addressee=None):
        """Append one feed row. `fields` carries item, kind, form, title, parent, root, depth, owner, client.

        `at`, `session` and `actor` default to the transaction's; claim rows override them. `addressee`
        is set on ask.* rows and comment.routed: the actor the ask's role (or the comment's owner) resolved to as
        the row is written.
        """
        row = {
            "at": at or clock.stamp(self.now),
            "change": change,
            "item": None,
            "kind": None,
            "form": None,
            "title": None,
            "parent": None,
            "root": None,
            "depth": None,
            "owner": None,
            "client": None,
            **fields,
            "actor": actor or self.actor,
            "session": session if session is not None else self.session,
            "status_old": status_old,
            "status_new": status_new,
            "data": json.dumps(data or {}, sort_keys=True),
            "addressee": addressee,
        }
        columns = ", ".join(row)
        marks = ", ".join(f":{name}" for name in row)
        cursor = self.conn.execute(f"INSERT INTO journal ({columns}) VALUES ({marks})", row)
        self.head = cursor.lastrowid
        self.marks_pages = self.marks_pages or spawn.can_mark(change, row["item"])
        return self.head


def write_head(directory, seq):
    """Rewrite feed.head atomically: write feed.head.tmp, then rename it over feed.head."""
    tmp = directory / (HEAD_NAME + ".tmp")
    tmp.write_text(f"{seq}\n")
    os.replace(tmp, directory / HEAD_NAME)


def lock_timeout(exc):
    """The exit-4 conflict for SQLite giving up on a lock after the busy timeout; None for any other error."""
    if "locked" in str(exc) or "busy" in str(exc):
        return conflict("locked", f"another writer held the store's write lock past the {busy_timeout_ms()} ms "
                                  "busy timeout; nothing was written, try again")
    return None


def sqlite_failure(exc):
    """The error for a SQLite error: the exit-4 lock conflict, else a refusal under rule `sqlite`.

    A bare DatabaseError ("file is not a database", "database disk image is malformed") means the
    store file itself is damaged, so the message points at the backups.
    """
    if type(exc) is sqlite3.DatabaseError:
        return refused("sqlite", f"the store file is corrupt or not a SQLite database ({exc}); "
                                 "restore it from a copy written by `trk backup`")
    return lock_timeout(exc) or refused("sqlite", str(exc))


def create(prefix, me, display):
    """Create a fresh store; refuses if one already exists. Returns the open Store.

    A store exists once its schema does: an empty store.db left by a failed init is reused.
    """
    if not ACTOR_RE.match(me):
        raise usage(f"actor id {me!r} is not a lowercase slug")
    if not ids.PREFIX_RE.match(prefix):
        raise usage(f"prefix {prefix!r} is not a lowercase slug of letters and digits")
    directory = store_dir()
    db_path = directory / DB_NAME
    guard_default(directory, "create")
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory, _connect(db_path))
    try:
        with store.transaction(actor=me) as tx:
            if _has_schema(tx.conn):
                raise refused("store-exists", f"a store already exists at {db_path}")
            for statement in _statements(SCHEMA):
                tx.conn.execute(statement)
            tx.conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("id_prefix", prefix),
                    ("default_actor", me),
                    ("lease_minutes", str(DEFAULT_LEASE_MINUTES)),
                    ("epoch", new_epoch()),
                ],
            )
            for actor_id, kind, actor_display in ((me, "human", display or me), ("herdr", "agent", "herdr")):
                add_actor(tx, actor_id, kind, actor_display)
            config.seed(tx, me)
            roles.seed(tx, me)
            seed_publisher(tx, me)
    except BaseException:
        store.close()
        raise
    return store


def _statements(script):
    """Split the schema into statements, keeping trigger bodies whole."""
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    return [s for s in statements if s]


def _has_schema(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is not None


def open_store(sweep=True):
    """Open the existing store, migrate an older schema, refuse a newer one, then sweep lapsed leases.

    `trk sweep` passes sweep=False and sweeps itself, so its rows name who ran it.
    """
    directory = store_dir()
    db_path = directory / DB_NAME
    missing = not_found(f"no store at {db_path}; run `trk init --me <actor>`", rule="no-store")
    if not db_path.exists():
        raise missing
    store = Store(directory, _connect(db_path))
    if not _has_schema(store.conn):
        store.close()
        raise missing
    version = int(store.meta("schema_version"))
    if version > SCHEMA_VERSION:
        store.close()
        raise refused(
            "schema-newer", f"store schema {version} is newer than this trk understands ({SCHEMA_VERSION})"
        )
    try:
        if version < SCHEMA_VERSION:
            guard_default(directory, f"migrate from schema {version} to {SCHEMA_VERSION}")
            _migrate(store)
        _ensure_epoch(store)
        if sweep:
            claims.sweep(store)
    except BaseException:
        store.close()
        raise
    return store


def _migrate(store):
    """Run every step up to SCHEMA_VERSION in one transaction; the default actor writes its rows."""
    with store.transaction(actor=store.meta("default_actor")) as tx:
        version = int(store.meta("schema_version"))  # re-read under the lock
        if version < 9:
            _add_client_columns(store.conn)
        while version < SCHEMA_VERSION:
            if version not in MIGRATIONS:
                raise refused(
                    "migration-missing", f"no migration from schema {version} to {version + 1}"
                )
            MIGRATIONS[version](tx)
            version += 1
        store.conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(version),))


def new_epoch():
    """A fresh store epoch: an opaque uuid, minted by init and by every restore."""
    return str(uuid.uuid4())


def _ensure_epoch(store):
    """Give a store from before epochs its first one. It is not a change, so no journal row."""
    if store.meta("epoch") is None:
        with store.write_lock():
            store.conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('epoch', ?)", (new_epoch(),))


def in_agent_session():
    """Whether this process runs inside an agent session: $CLAUDECODE or $TRK_SESSION is set."""
    return bool(os.environ.get("CLAUDECODE") or os.environ.get("TRK_SESSION"))


def resolve_actor(store, explicit, *, guard=True):
    """The writing actor: --as, else $TRK_ACTOR, else the store's default; must be registered.

    In an agent session the default is not used (rule `actor-required`, exit 2): the store's default actor
    is a person, so an unnamed agent write would be recorded as them (tr-82j8x). `guard=False` is for
    callers that read, or that only renew leases by session, where the actor is not an act of anyone's.
    """
    actor = explicit or os.environ.get("TRK_ACTOR")
    if actor is None:
        if guard and in_agent_session():
            raise usage("this write comes from an agent session, so it must name its actor: pass `--as <actor>` or"
                        " set $TRK_ACTOR (`trk actors` lists who is registered)", rule="actor-required")
        actor = store.meta("default_actor")
    return require_actor(store.conn, actor)


def require_actor(conn, actor_id):
    """Refuse an actor id that is not registered (rule `unknown-actor`)."""
    if not conn.execute("SELECT 1 FROM actors WHERE id = ?", (actor_id,)).fetchone():
        raise refused("unknown-actor", f"actor {actor_id!r} is not registered", item=actor_id)
    return actor_id


def resolve_session(explicit):
    """The writing session: --session, else $TRK_SESSION; optional."""
    return explicit or os.environ.get("TRK_SESSION") or None


def backup_failed(path, exc):
    """The refusal for a backup the filesystem would not write to `path`."""
    return refused("backup-failed", f"could not write the backup to {path}: {exc.strerror or exc}")


def backup(store, path):
    """Copy the store to `path` with SQLite's online backup API; returns (resolved path, head seq).

    The copy is one consistent snapshot even while other sessions write. It is written to a temp
    file beside `path` and hard-linked into place, so a failed backup leaves nothing behind and an
    existing file is never overwritten, even one created while the copy ran.
    """
    path = Path(path).expanduser().absolute()
    if path.exists():
        raise refused("backup-exists", f"{path} already exists; backup never overwrites")
    if not path.parent.is_dir():
        raise usage(f"no directory {path.parent} for the backup")
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    except OSError as exc:
        raise backup_failed(path, exc) from exc
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        dest = sqlite3.connect(tmp)
        try:
            store.conn.backup(dest)
            head = _journal_head(dest)
        finally:
            dest.close()
        os.link(tmp, path)
    except FileExistsError:
        raise refused("backup-exists", f"{path} already exists; backup never overwrites") from None
    except sqlite3.Error as exc:
        raise sqlite_failure(exc) from exc
    except OSError as exc:  # the filesystem refused the copy, such as a link it won't make (EPERM, EXDEV, ENOTSUP)
        raise backup_failed(path, exc) from exc
    finally:
        tmp.unlink(missing_ok=True)
    return path, head


def restore(path):
    """Replace the store with the backup at `path` and rotate the epoch; returns (path, head, epoch).

    The backup's bytes are read and staged in memory, where the copy gets a fresh epoch; SQLite
    never opens the backup file, so it is never changed. The staged copy then lands in the live
    store in one step of SQLite's online backup API, which holds SQLite's write lock for the whole
    copy and writes through the live WAL, so the copy and the new epoch arrive together. A backup
    carries its store's old epoch, so without the rotation a feed consumer could not tell the
    journal had rewound. No journal row is written: head is the backup's.
    """
    path = Path(path).expanduser().absolute()
    directory = store_dir()
    db_path = directory / DB_NAME
    if not path.is_file():
        raise not_found(f"no backup at {path}", rule="no-backup")
    if db_path.exists() and path.samefile(db_path):
        raise usage(f"{path} is the live store; restore from a backup")
    guard_default(directory, "restore over")
    staged = _stage(path)
    try:
        epoch = new_epoch()
        staged.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('epoch', ?)", (epoch,))
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise refused("restore-failed", f"could not create the store dir {directory}: {exc.strerror or exc}") from exc
        live = Store(directory, _connect(db_path))
        try:
            staged.backup(live.conn)
            with live.write_lock():  # trk's lock, so no writer's feed.head lands between the read and the rewrite
                head = _journal_head(live.conn)
                write_head(directory, head)
        finally:
            live.close()
    except sqlite3.Error as exc:
        raise sqlite_failure(exc) from exc
    finally:
        staged.close()
    return path, head, epoch


def _journal_head(conn):
    return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM journal").fetchone()[0]


def _stage(path):
    """An in-memory copy of the backup at `path`, from its bytes alone. Refuses a backup with a
    -wal file beside it (rule `backup-has-wal`: a file copy of a live store, not `trk backup`'s
    output), a file that is not a store (rule `not-a-store`) and a newer schema (`schema-newer`)."""
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise refused("backup-has-wal", f"{wal} holds changes not in {path}; restore from a file `trk backup` wrote")
    try:
        image = bytearray(path.read_bytes())
    except OSError as exc:
        raise refused("restore-failed", f"could not read {path}: {exc.strerror or exc}") from exc
    if image[18:20] == b"\x02\x02":  # WAL mode in the header; an in-memory database must be in rollback mode
        image[18:20] = b"\x01\x01"
    not_a_store = f"{path} is not a trk store"
    if not image:  # deserialize refuses an empty image; an empty file has no schema anyway
        raise refused("not-a-store", not_a_store)
    staged = sqlite3.connect(":memory:", isolation_level=None)
    try:
        try:
            staged.deserialize(bytes(image))
            row = staged.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone() if _has_schema(staged) else None
        except sqlite3.DatabaseError as exc:
            if "not a database" in str(exc) or "malformed" in str(exc):
                raise refused("not-a-store", not_a_store) from None
            raise sqlite_failure(exc) from exc
        if row is None or not str(row[0]).isdigit():
            raise refused("not-a-store", not_a_store)
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise refused(
                "schema-newer", f"backup schema {version} is newer than this trk understands ({SCHEMA_VERSION})"
            )
    except BaseException:
        staged.close()
        raise
    return staged
