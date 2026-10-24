"""The journal is append-only at the SQLite level, carries every feed row field, and feed.head tracks it."""

import json
import sqlite3

import pytest
from gated import DEST

FEED_FIELDS = {
    "seq", "at", "change", "item", "kind", "form", "title", "parent", "root", "depth",
    "actor", "session", "owner", "status_old", "status_new", "data", "addressee", "client",
}


def journal(trk_home):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM journal ORDER BY seq")]
    finally:
        conn.close()


def journal_head(trk_home):
    return max(row["seq"] for row in journal(trk_home))


def feed_head(trk_home):
    return int((trk_home / "feed.head").read_text())


@pytest.mark.parametrize("statement", ["DELETE FROM journal", "UPDATE journal SET change = 'x'"])
def test_journal_refuses_delete_and_update(trk, trk_home, statement):  # AC 8
    trk("init", "--me", "michael")
    conn = sqlite3.connect(trk_home / "store.db")
    with pytest.raises(sqlite3.IntegrityError, match="journal is append-only"):
        conn.execute(statement)
    conn.close()


def test_init_journals_the_seeded_actors_the_default_owner_and_the_seeded_roles(trk, trk_home):  # AC 1; spec 02
    trk("init", "--me", "michael", "--display", "Michael Fork")
    rows = journal(trk_home)
    assert [(r["change"], r["item"], r["actor"]) for r in rows] == [
        ("actor.added", None, "michael"),
        ("actor.added", None, "michael"),
        ("config.changed", None, "michael"),
        ("role.set", None, "michael"),
        ("role.set", None, "michael"),
        ("actor.added", None, "michael"),
        ("role.set", None, "michael"),
    ]
    assert [json.loads(r["data"]) for r in rows] == [
        {"id": "michael", "kind": "human", "display": "Michael Fork"},
        {"id": "herdr", "kind": "agent", "display": "herdr"},
        {"key": "default-owner", "old": None, "new": "michael"},
        {"role": "product", "old": None, "new": "michael"},
        {"role": "engineering", "old": None, "new": "michael"},
        {"id": "publisher", "kind": "agent", "display": "publisher"},
        {"role": "publisher", "old": None, "new": "michael"},
    ]


def test_item_created_row_carries_every_feed_field(trk, trk_home, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-10T21:52:03.123Z")
    trk("init", "--me", "michael")
    item_id = trk("new", "program", "Tracker v1", "--session", "s-1", *DEST).out.strip()

    [row] = [r for r in journal(trk_home) if r["change"] == "item.created"]
    assert set(row) == FEED_FIELDS
    assert {k: v for k, v in row.items() if k != "seq"} == {
        "at": "2026-10-10T21:52:03.123Z",
        "change": "item.created",
        "item": item_id,
        "kind": "program",
        "form": None,
        "title": "Tracker v1",
        "parent": None,
        "root": item_id,
        "depth": 0,
        "actor": "michael",
        "session": "s-1",
        "owner": "michael",
        "status_old": None,
        "status_new": "open",
        "data": json.dumps(
            {"jira_url": None, "owner": "michael", "rank": "V", "repos": [], "sections": [], "subtype": None, "urgent": False,
             "stage": None, "criteria": ["ac-1"]},
            sort_keys=True,
        ),
        "addressee": None,
        "client": None,
    }


def test_feed_head_equals_journal_head_after_every_write(trk, trk_home):
    trk("init", "--me", "michael")
    assert feed_head(trk_home) == journal_head(trk_home)
    for title in ("A", "B", "C"):
        trk("new", "program", title, *DEST)
        assert feed_head(trk_home) == journal_head(trk_home)
    assert not (trk_home / "feed.head.tmp").exists()


def test_seq_strictly_increases(trk, trk_home):
    trk("init", "--me", "michael")
    for title in ("A", "B"):
        trk("new", "program", title, *DEST)
    seqs = [r["seq"] for r in journal(trk_home)]
    assert seqs == sorted(set(seqs))


def test_top_level_programs_are_ranked_in_creation_order(trk):
    trk("init", "--me", "michael")
    ranks = [trk("new", "program", t, "--json", *DEST).json()["item"]["rank"] for t in ("A", "B", "C")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 3
