"""`trk changes`: the herdr contract's read side (spec: Change feed; ADR 0002 points 2-5)."""

import json
import sqlite3

import pytest

from trk import feed
from gated import DEST

FEED_FIELDS = [
    "seq", "at", "change", "item", "kind", "form", "title", "parent", "root", "depth",
    "actor", "session", "owner", "status_old", "status_new", "data", "addressee", "client",
]
INIT_ROWS = ["actor.added", "actor.added", "config.changed", "role.set", "role.set", "actor.added", "role.set"]
I = len(INIT_ROWS)  # rows `init` writes; the first item row is seq I + 1
CREATE_PROGRAM = ["item.created", "criterion.added"]  # a program is created with one destination statement
P = len(CREATE_PROGRAM)


def changes(trk, *argv):
    result = trk("changes", *argv, "--json")
    assert result.code == 0, result.err
    return result.json()


def seqs(response):
    return [row["seq"] for row in response["changes"]]


def append_rows(trk_home, rows):
    """Append raw journal rows, standing in for verbs later tickets build (moves, more items)."""
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        for row in rows:
            row = {"at": "2026-10-10T00:00:00.000Z", "actor": "michael", "data": "{}", **row}
            columns = ", ".join(row)
            marks = ", ".join(f":{name}" for name in row)
            conn.execute(f"INSERT INTO journal ({columns}) VALUES ({marks})", row)
        conn.commit()
    finally:
        conn.close()


def program_row(item_id, title="T"):
    return {"change": "item.updated", "item": item_id, "kind": "program", "title": title, "root": item_id,
            "depth": 0, "data": json.dumps({"fields": ["title"], "old": {"title": "x"}, "new": {"title": title}})}


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")


def test_since_zero_replays_every_row_ascending(trk, store):  # AC 7
    ids = [trk("new", "program", t, *DEST).out.strip() for t in ("A", "B")]
    response = changes(trk, "--since", "0")

    assert list(response) == ["schema", "epoch", "head", "next", "changes"]
    assert response["schema"] == 1
    assert [r["change"] for r in response["changes"]] == [*INIT_ROWS, *CREATE_PROGRAM, *CREATE_PROGRAM]
    assert [r["item"] for r in response["changes"][I:]] == [ids[0]] * P + [ids[1]] * P
    assert seqs(response) == sorted(seqs(response))
    assert response["head"] == response["next"] == seqs(response)[-1]


def test_rows_carry_the_spec_fields_in_order_with_data_as_an_object(trk, store, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-10T21:52:03.123Z")
    item_id = trk("new", "program", "Tracker v1", "--session", "s-1", *DEST).out.strip()
    row = changes(trk, "--since", str(I))["changes"][0]

    assert list(row) == FEED_FIELDS
    assert row == {
        "seq": I + 1, "at": "2026-10-10T21:52:03.123Z", "change": "item.created", "item": item_id,
        "kind": "program", "form": None, "title": "Tracker v1", "parent": None, "root": item_id, "depth": 0,
        "actor": "michael", "session": "s-1", "owner": "michael", "status_old": None, "status_new": "open",
        "data": {"jira_url": None, "owner": "michael", "rank": "V", "repos": [], "sections": [], "subtype": None,
                 "urgent": False, "stage": None, "criteria": ["ac-1"]},
        "addressee": None, "client": None,
    }


def test_rows_without_an_item_have_null_item_fields_and_empty_data_is_an_object(trk, store, trk_home):
    append_rows(trk_home, [{"change": "actor.updated", "data": json.dumps({"id": "michael", "kind": "human", "display": "M"})}])
    rows = changes(trk, "--since", "0")["changes"]
    assert all(r[f] is None for r in rows for f in ("item", "kind", "form", "title", "parent", "root", "depth", "owner"))
    append_rows(trk_home, [{"change": "future.kind", "data": "{}"}])
    assert changes(trk, "--since", str(I + 1))["changes"][0]["data"] == {}


def test_since_is_exclusive(trk, store):
    for title in ("A", "B", "C"):
        trk("new", "program", title, *DEST)
    assert seqs(changes(trk, "--since", str(I + 1))) == list(range(I + 2, I + 3 * P + 1))


def test_since_head_returns_no_rows_and_next_is_head(trk, store):  # AC 7
    trk("new", "program", "A", *DEST)
    head = changes(trk, "--since", "0")["head"]
    response = changes(trk, "--since", str(head))
    assert response["changes"] == []
    assert response["head"] == response["next"] == head


def test_limit_caps_rows_and_next_resumes_without_gaps(trk, store):
    for title in ("A", "B", "C"):
        trk("new", "program", title, *DEST)
    head = I + 3 * P
    first = changes(trk, "--since", str(head - 5), "--limit", "2")
    assert seqs(first) == [head - 4, head - 3]
    assert first["next"] == head - 3 and first["head"] == head
    second = changes(trk, "--since", str(first["next"]), "--limit", "3")
    assert seqs(second) == [head - 2, head - 1, head]
    third = changes(trk, "--since", str(second["next"]), "--limit", "2")
    assert seqs(third) == [] and third["next"] == head


def test_default_limit_is_1000(trk, store, trk_home):
    append_rows(trk_home, [{"change": "future.kind"} for _ in range(1005)])
    response = changes(trk, "--since", "0")
    assert feed.DEFAULT_LIMIT == 1000
    assert len(response["changes"]) == 1000
    assert response["next"] == 1000 and response["head"] == 1005 + I


def test_root_keeps_only_that_roots_rows_and_never_rows_without_an_item(trk, store):  # AC 7
    p = trk("new", "program", "P", *DEST).out.strip()
    q = trk("new", "program", "Q", *DEST).out.strip()
    trk("actor", "add", "kim", "--kind", "human")
    trk("roles", "set", "product", "kim")
    trk("config", "set", "default-owner", "kim")
    response = changes(trk, "--since", "0", "--root", p)
    assert [(r["change"], r["item"]) for r in response["changes"]] == [(change, p) for change in CREATE_PROGRAM]
    assert q not in {r["root"] for r in response["changes"]}
    # The scan reached the end, so next is head even though the other rows were filtered out.
    assert response["next"] == response["head"] == I + 2 * P + 3


def test_root_accepts_an_id_in_any_case(trk, store):
    p = trk("new", "program", "P", *DEST).out.strip()
    assert seqs(changes(trk, "--since", "0", "--root", p.upper())) == list(range(I + 1, I + P + 1))


def test_root_with_limit_sets_next_to_the_last_row_scanned(trk, store, trk_home):
    p = trk("new", "program", "P", *DEST).out.strip()  # seq I + 1 and I + 2
    q = trk("new", "program", "Q", *DEST).out.strip()  # seq I + 3 and I + 4
    append_rows(trk_home, [program_row(q), program_row(p), program_row(q), program_row(p)])  # seq I + 5 to I + 8
    first = changes(trk, "--since", "0", "--root", p, "--limit", "3")
    assert seqs(first) == [I + 1, I + 2, I + 6] and first["next"] == I + 6
    second = changes(trk, "--since", str(first["next"]), "--root", p, "--limit", "2")
    assert seqs(second) == [I + 8] and second["next"] == I + 8
    tail = changes(trk, "--since", str(I + 8), "--root", p)
    assert tail["changes"] == [] and tail["next"] == I + 8


def test_root_includes_item_moved_rows_leaving_that_root(trk, store, trk_home):
    p = trk("new", "program", "P", *DEST).out.strip()
    q = trk("new", "program", "Q", *DEST).out.strip()
    moved = {
        "change": "item.moved", "item": "tr-child", "kind": "project", "title": "C", "parent": q, "root": q,
        "depth": 1,
        "data": json.dumps({"old": {"parent": p, "root": p, "depth": 1, "rank": "V"},
                            "new": {"parent": q, "root": q, "depth": 1, "rank": "V"}}),
    }
    append_rows(trk_home, [moved])
    assert [r["change"] for r in changes(trk, "--since", "0", "--root", p)["changes"]] == [*CREATE_PROGRAM, "item.moved"]
    assert [r["change"] for r in changes(trk, "--since", "0", "--root", q)["changes"]] == [*CREATE_PROGRAM, "item.moved"]


def test_unknown_root_is_not_found(trk, store):
    result = trk("changes", "--since", "0", "--root", "tr-zzzzz", "--json")
    assert result.code == 3
    assert result.error()["code"] == "not-found"


@pytest.mark.parametrize(
    "argv",
    [[], ["--since", "-1"], ["--since", "x"], ["--since", "0", "--limit", "0"], ["--since", "0", "--limit", "-5"]],
)
def test_bad_arguments_are_usage_errors(trk, store, argv):
    result = trk("changes", *argv, "--json")
    assert result.code == 2
    assert result.error()["code"] == "usage"


def test_changes_without_a_store_is_not_found(trk):
    assert trk("changes", "--since", "0").code == 3


def test_plain_output_prints_one_line_per_row(trk, store):
    p = trk("new", "program", "Tracker v1", *DEST).out.strip()
    lines = trk("changes", "--since", str(I)).out.splitlines()
    assert len(lines) == P
    seq, _at, change, item = lines[0].split()[:4]
    assert (seq, change, item) == (str(I + 1), "item.created", p)
    assert "Tracker v1" in lines[0]


def test_changes_takes_no_write_lock(trk, store, trk_home):
    """A reader must not block on (or be blocked by) a writer holding the lock."""
    conn = sqlite3.connect(trk_home / "store.db", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert trk("changes", "--since", "0", "--json").code == 0
    finally:
        conn.execute("ROLLBACK")
        conn.close()
