"""Sibling order through `new --first/--last/--before/--after`, and re-keying a crowded spot."""

import json
import sqlite3

import pytest

from trk import ranks
from gated import DEST, new_flags


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
    return trk


def new(trk, *argv):
    result = trk("new", *argv, *new_flags(argv), "--json")
    assert result.code == 0, result.err
    return result.json()["item"]["id"]


def order(trk, parent):
    return [c["title"] for c in trk("show", parent, "--json").json()["item"]["children"]]


def moved_rows(trk_home):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM journal WHERE change = 'item.moved' ORDER BY seq")]
    finally:
        conn.close()


def test_placement_flags_order_siblings(store):
    p = new(store, "program", "P")
    b = new(store, "project", "B", "--parent", p)  # --last is the default
    new(store, "project", "D", "--parent", p, "--last")
    new(store, "project", "A", "--parent", p, "--first")
    new(store, "project", "C", "--parent", p, "--after", b)
    new(store, "project", "Bb", "--parent", p, "--before", b)
    assert order(store, p) == ["A", "Bb", "B", "C", "D"]


def test_top_level_items_are_ranked_among_themselves(store):
    second = new(store, "program", "Second")
    new(store, "project", "First", "--before", second)
    new(store, "work-item", "Third", "--form", "self-contained")
    tree = store("tree", "--json").json()["items"]
    assert [i["title"] for i in tree] == ["First", "Second", "Third"]


@pytest.mark.parametrize("flag", ["--before", "--after"])
def test_anchor_must_share_the_parent(store, flag):
    p = new(store, "program", "P")
    other = new(store, "program", "Q")
    child_of_q = new(store, "project", "J", "--parent", other)
    result = store("new", "project", "X", "--parent", p, flag, child_of_q, "--json")
    assert result.code == 1
    assert result.error()["rule"] == "not-sibling" and result.error()["item"] == child_of_q
    # Without --parent the item is top-level, so the anchor must be top-level too.
    assert store("new", "program", "Y", flag, child_of_q, "--json", *DEST).error()["rule"] == "not-sibling"


def test_unknown_anchor_exits_3(store):
    assert store("new", "program", "Y", "--after", "tr-zzzzz", "--json", *DEST).code == 3


def test_placement_flags_are_exclusive(store):
    assert store("new", "program", "Y", "--first", "--last", *DEST).code == 2


def test_crowding_one_spot_rekeys_the_parents_children(store, trk_home):
    p = new(store, "program", "P")
    a = new(store, "project", "A", "--parent", p)
    new(store, "project", "Z", "--parent", p)
    inserted = []
    while not moved_rows(trk_home):
        assert len(inserted) < 400, "never re-keyed"
        inserted.append(new(store, "project", f"N{len(inserted)}", "--parent", p, "--after", a))

    # Each insert goes right after A, so the newest comes first.
    expected = ["A"] + [f"N{i}" for i in reversed(range(len(inserted)))] + ["Z"]
    assert order(store, p) == expected

    children = store("tree", p, "--json").json()["items"][1:]
    ranks_now = {c["id"]: c["rank"] for c in children}
    assert all(len(r) <= ranks.MAX_LENGTH for r in ranks_now.values())
    assert max(len(r) for r in ranks_now.values()) <= 3  # evenly re-keyed, not just trimmed

    rows = moved_rows(trk_home)
    rekeyed = {r["item"] for r in rows}
    assert rekeyed == {c["id"] for c in children} - {inserted[-1]}
    for row in rows:
        data = json.loads(row["data"])
        spot = {"parent": p, "root": p, "depth": 1}
        assert set(data) == {"old", "new", "rekey"} and data["rekey"] is True
        assert data["new"] == {**spot, "rank": ranks_now[row["item"]]}
        assert data["old"]["rank"] != data["new"]["rank"]
        assert {k: v for k, v in data["old"].items() if k != "rank"} == spot
        assert (row["parent"], row["root"], row["depth"]) == (p, p, 1)
        assert row["status_old"] is None and row["status_new"] is None

    # The re-keyed spot takes more inserts without trouble.
    new(store, "project", "Later", "--parent", p, "--after", a)
    assert order(store, p)[:2] == ["A", "Later"]
