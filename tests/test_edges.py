"""`trk link` / `trk unlink`, supersede and duplicate closes, and edges in `show`."""

import json
import sqlite3

import pytest
from gated import DEST, prove_close


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
    return trk


def new(trk, title, *argv):
    result = trk("new", "work-item", title, "--form", "decision", "--subtype", "task", *argv, "--json")
    assert result.code == 0, result.err
    return result.json()["item"]["id"]


def journal(trk_home):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM journal ORDER BY seq")]
    finally:
        conn.close()
    for row in rows:
        row["data"] = json.loads(row["data"])
    return rows


def edge_rows(trk_home, after=0):
    return [(r["change"], r["data"]["src"], r["data"]["type"], r["data"]["dst"])
            for r in journal(trk_home)[after:] if r["change"].startswith("edge.")]


def stored(trk_home):
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        return set(conn.execute("SELECT src, type, dst FROM edges"))
    finally:
        conn.close()


@pytest.fixture
def abc(store):
    return new(store, "A"), new(store, "B"), new(store, "C")


def link(trk, a, edge_type, b, *argv):
    return trk("link", a, edge_type, b, *argv)


@pytest.mark.parametrize("edge_type", ["blocks", "relates", "supersedes"])
def test_link_adds_an_edge_and_journals_edge_added(store, trk_home, abc, edge_type):
    a, b, _ = abc
    result = link(store, a, edge_type, b, "--session", "s-1")
    assert result.code == 0, result.err
    src, dst = (min(a, b), max(a, b)) if edge_type == "relates" else (a, b)
    assert stored(trk_home) == {(src, edge_type, dst)}
    row = journal(trk_home)[-1]
    assert (row["change"], row["item"], row["session"]) == ("edge.added", src, "s-1")
    assert row["data"] == {"type": edge_type, "src": src, "dst": dst}
    assert row["title"] == ("A" if src == a else "B")


def test_link_prints_the_edge_or_its_json(store, abc):
    a, b, _ = abc
    assert link(store, a, "blocks", b).out.strip() == f"{a} blocks {b}"
    result = link(store, b, "blocks", abc[2], "--json")
    assert result.json() == {"schema": 1, "edge": {"src": b, "type": "blocks", "dst": abc[2]}}


def test_link_matches_ids_case_insensitively(store, trk_home, abc):
    a, b, _ = abc
    assert link(store, a.upper(), "blocks", b.upper()).code == 0
    assert stored(trk_home) == {(a, "blocks", b)}


def test_relates_is_stored_once_whichever_way_it_is_given(store, trk_home, abc):
    a, b, _ = abc
    assert link(store, b, "relates", a).code == 0
    before = len(journal(trk_home))
    assert link(store, a, "relates", b).code == 0
    assert stored(trk_home) == {(min(a, b), "relates", max(a, b))}
    assert len(journal(trk_home)) == before


def test_linking_an_existing_edge_is_a_no_op(store, trk_home, abc):
    a, b, _ = abc
    link(store, a, "blocks", b)
    before = len(journal(trk_home))
    result = link(store, a, "blocks", b)
    assert result.code == 0, result.err
    assert len(journal(trk_home)) == before


@pytest.mark.parametrize("edge_type", ["blocks", "relates", "supersedes"])
def test_self_links_are_refused(store, trk_home, abc, edge_type):
    a = abc[0]
    result = link(store, a, edge_type, a, "--json")
    assert result.code == 1
    assert (result.error()["rule"], result.error()["item"]) == ("self-link", a)
    assert stored(trk_home) == set()


def test_link_to_an_unknown_item_exits_3(store, abc):
    assert link(store, abc[0], "blocks", "tr-zzzzz").code == 3
    assert link(store, "tr-zzzzz", "blocks", abc[0]).code == 3


def test_link_with_an_unknown_type_is_a_usage_error(store, abc):
    assert link(store, abc[0], "parent", abc[1]).code == 2


def test_ac3_a_blocks_cycle_is_refused(store, trk_home, abc):
    a, b, c = abc
    assert link(store, a, "blocks", b).code == 0
    assert link(store, b, "blocks", c).code == 0
    before = len(journal(trk_home))
    result = link(store, c, "blocks", a, "--json")
    assert result.code == 1
    assert result.error()["rule"] == "cycle"
    assert result.error()["item"] == c
    assert len(journal(trk_home)) == before
    assert (c, "blocks", a) not in stored(trk_home)


def test_two_way_blocks_is_a_cycle(store, abc):
    a, b, _ = abc
    link(store, a, "blocks", b)
    assert link(store, b, "blocks", a).code == 1


def test_only_blocks_edges_make_cycles(store, abc):
    a, b, _ = abc
    link(store, a, "supersedes", b)
    link(store, a, "relates", b)
    assert link(store, b, "blocks", a).code == 0
    assert link(store, b, "supersedes", a).code == 0


def test_edges_may_cross_the_tree(store, trk_home):
    p = store("new", "program", "P", "--json", *DEST).json()["item"]["id"]
    q = store("new", "program", "Q", "--json", *DEST).json()["item"]["id"]
    x = new(store, "X", "--parent", p)
    assert link(store, x, "blocks", q).code == 0
    assert link(store, p, "relates", x).code == 0


def test_unlink_removes_an_edge_and_journals_edge_removed(store, trk_home, abc):
    a, b, _ = abc
    link(store, a, "blocks", b)
    result = store("unlink", a, "blocks", b, "--session", "s-2")
    assert result.code == 0, result.err
    assert result.out.strip() == f"{a} blocks {b}"
    assert stored(trk_home) == set()
    row = journal(trk_home)[-1]
    assert (row["change"], row["item"], row["session"]) == ("edge.removed", a, "s-2")
    assert row["data"] == {"type": "blocks", "src": a, "dst": b}


def test_unlink_relates_works_either_way(store, trk_home, abc):
    a, b, _ = abc
    link(store, a, "relates", b)
    assert store("unlink", b, "relates", a).code == 0
    assert stored(trk_home) == set()


def test_unlink_of_a_missing_edge_is_a_no_op(store, trk_home, abc):
    a, b, _ = abc
    link(store, b, "blocks", a)
    before = len(journal(trk_home))
    assert store("unlink", a, "blocks", b).code == 0
    assert len(journal(trk_home)) == before
    assert stored(trk_home) == {(b, "blocks", a)}


def test_unlink_with_an_unknown_item_exits_3(store, abc):
    assert store("unlink", abc[0], "blocks", "tr-zzzzz").code == 3


def test_closed_items_can_be_linked(store, abc):
    a, b, _ = abc
    prove_close(store, a)
    assert store("close", a, "--resolution", "done", "--outcome", "Done.").code == 0
    assert link(store, a, "blocks", b).code == 0


# Supersede and duplicate closes.


def close(trk, item_id, *argv, outcome="Replaced."):
    if argv[:2] == ("--resolution", "done"):
        prove_close(trk, item_id)
    return trk("close", item_id, *argv, "--outcome", outcome, "--json")


def test_ac6_superseding_moves_what_the_item_blocked_onto_its_replacement(store, trk_home):
    d, d2, x = new(store, "D"), new(store, "D2"), new(store, "X")
    link(store, d, "blocks", x)
    before = len(journal(trk_home))
    result = close(store, d, "--resolution", "superseded", "--by", d2)
    assert result.code == 0, result.err
    assert stored(trk_home) == {(d2, "supersedes", d), (d2, "blocks", x)}
    rows = journal(trk_home)[before:]
    assert [(r["change"], r["item"]) for r in rows] == [
        ("item.closed", d), ("edge.added", d2), ("edge.removed", d), ("edge.added", d2),
    ]
    assert rows[0]["data"] == {"resolution": "superseded", "outcome": "Replaced.", "by": d2}
    assert edge_rows(trk_home, before) == [
        ("edge.added", d2, "supersedes", d),
        ("edge.removed", d, "blocks", x),
        ("edge.added", d2, "blocks", x),
    ]
    d2_edges = store("show", d2, "--json").json()["item"]["edges"]
    assert {(e["dir"], e["type"], e["id"]) for e in d2_edges} == {("out", "supersedes", d), ("out", "blocks", x)}


def test_supersede_keeps_an_existing_supersedes_edge_and_skips_blocks_the_replacement_has(store, trk_home):
    d, r, x, y = new(store, "D"), new(store, "R"), new(store, "X"), new(store, "Y")
    link(store, r, "supersedes", d)
    link(store, d, "blocks", x)
    link(store, d, "blocks", y)
    link(store, r, "blocks", x)
    before = len(journal(trk_home))
    assert close(store, d, "--resolution", "superseded", "--by", r).code == 0
    assert stored(trk_home) == {(r, "supersedes", d), (r, "blocks", x), (r, "blocks", y)}
    assert edge_rows(trk_home, before) == [
        ("edge.removed", d, "blocks", x),
        ("edge.removed", d, "blocks", y),
        ("edge.added", r, "blocks", y),
    ]


def test_supersede_leaves_incoming_blocks_and_other_edges_alone(store, trk_home):
    d, r, w, z = new(store, "D"), new(store, "R"), new(store, "W"), new(store, "Z")
    link(store, w, "blocks", d)
    link(store, d, "relates", z)
    assert close(store, d, "--resolution", "superseded", "--by", r).code == 0
    assert stored(trk_home) == {(w, "blocks", d), (min(d, z), "relates", max(d, z)), (r, "supersedes", d)}


def test_supersede_refuses_the_whole_close_if_a_move_makes_a_cycle(store, trk_home):
    d, r, x = new(store, "D"), new(store, "R"), new(store, "X")
    link(store, d, "blocks", x)
    link(store, x, "blocks", r)
    before = (len(journal(trk_home)), stored(trk_home))
    result = close(store, d, "--resolution", "superseded", "--by", r)
    assert result.code == 1
    assert result.error()["rule"] == "cycle"
    assert (len(journal(trk_home)), stored(trk_home)) == before
    assert store("show", d, "--json").json()["item"]["status"] == "open"


def test_supersede_refuses_moving_a_blocks_edge_onto_its_own_target(store, trk_home):
    d, r = new(store, "D"), new(store, "R")
    link(store, d, "blocks", r)
    result = close(store, d, "--resolution", "superseded", "--by", r)
    assert result.code == 1
    assert result.error()["rule"] == "cycle"


def test_supersede_moves_every_outgoing_blocks_edge_in_link_order(store, trk_home):
    d, r, x, y, z = (new(store, t) for t in "DRXYZ")
    link(store, d, "blocks", y)
    link(store, d, "blocks", x)
    link(store, x, "blocks", z)
    before = len(journal(trk_home))
    assert close(store, d, "--resolution", "superseded", "--by", r).code == 0
    assert edge_rows(trk_home, before)[1:] == [
        ("edge.removed", d, "blocks", y), ("edge.added", r, "blocks", y),
        ("edge.removed", d, "blocks", x), ("edge.added", r, "blocks", x),
    ]


def test_superseded_needs_by_and_by_needs_superseded(store):
    d, r = new(store, "D"), new(store, "R")
    result = close(store, d, "--resolution", "superseded")
    assert result.code == 2 and "--by" in result.error()["message"]
    assert close(store, d, "--resolution", "done", "--by", r).code == 2
    assert close(store, d, "--resolution", "superseded", "--by", r, "--of", r).code == 2


def test_superseded_by_itself_is_a_self_link(store):
    d = new(store, "D")
    result = close(store, d, "--resolution", "superseded", "--by", d)
    assert (result.code, result.error()["rule"]) == (1, "self-link")


def test_superseded_by_an_unknown_item_exits_3(store):
    d = new(store, "D")
    assert close(store, d, "--resolution", "superseded", "--by", "tr-zzzzz").code == 3
    assert store("show", d, "--json").json()["item"]["status"] == "open"


def test_duplicate_adds_a_relates_edge_and_carries_of(store, trk_home):
    d, x = new(store, "D"), new(store, "X")
    before = len(journal(trk_home))
    result = close(store, d, "--resolution", "duplicate", "--of", x.upper())
    assert result.code == 0, result.err
    assert stored(trk_home) == {(min(d, x), "relates", max(d, x))}
    rows = journal(trk_home)[before:]
    assert rows[0]["change"] == "item.closed"
    assert rows[0]["data"] == {"resolution": "duplicate", "outcome": "Replaced.", "of": x}
    assert edge_rows(trk_home, before) == [("edge.added", min(d, x), "relates", max(d, x))]


def test_duplicate_of_an_already_related_item_adds_no_edge(store, trk_home):
    d, x = new(store, "D"), new(store, "X")
    link(store, x, "relates", d)
    before = len(journal(trk_home))
    assert close(store, d, "--resolution", "duplicate", "--of", x).code == 0
    assert edge_rows(trk_home, before) == []


def test_duplicate_needs_of_and_of_needs_duplicate(store):
    d, x = new(store, "D"), new(store, "X")
    result = close(store, d, "--resolution", "duplicate")
    assert result.code == 2 and "--of" in result.error()["message"]
    assert close(store, d, "--resolution", "wont-do", "--of", x).code == 2


def test_reopening_a_superseded_item_leaves_the_moved_edges_moved(store, trk_home):
    d, d2, x = new(store, "D"), new(store, "D2"), new(store, "X")
    link(store, d, "blocks", x)
    close(store, d, "--resolution", "superseded", "--by", d2)
    before = len(journal(trk_home))
    result = store("reopen", d)
    assert result.code == 0, result.err
    assert stored(trk_home) == {(d2, "supersedes", d), (d2, "blocks", x)}
    assert edge_rows(trk_home, before) == []
    reopened = journal(trk_home)[-1]
    assert reopened["data"] == {"resolution": "superseded"}


# Edges in show.


def test_show_lists_edges_in_and_out_with_the_other_ends_title_and_status(store, abc):
    a, b, c = abc
    link(store, a, "blocks", b)
    link(store, c, "supersedes", b)
    link(store, b, "relates", c)
    prove_close(store, c)
    store("close", c, "--resolution", "done", "--outcome", "Done.")
    edges = store("show", b, "--json").json()["item"]["edges"]
    assert sorted((e["dir"], e["type"], e["id"], e["title"], e["status"]) for e in edges) == sorted([
        ("in", "blocks", a, "A", "open"),
        ("in", "supersedes", c, "C", "closed"),
        ("out", "relates", c, "C", "closed"),
    ])


def test_relates_shows_as_out_at_both_ends(store, abc):
    a, b, _ = abc
    link(store, a, "relates", b)
    for here, there in ((a, b), (b, a)):
        edges = store("show", here, "--json").json()["item"]["edges"]
        assert [(e["dir"], e["type"], e["id"]) for e in edges] == [("out", "relates", there)]


def test_an_item_without_edges_shows_an_empty_list(store, abc):
    assert store("show", abc[0], "--json").json()["item"]["edges"] == []


def test_human_show_reads_each_edge_from_this_item(store, abc):
    a, b, c = abc
    link(store, a, "blocks", b)
    link(store, b, "supersedes", c)
    link(store, b, "relates", c)
    out = store("show", b).out
    assert f"blocked by {a}  A" in out
    assert f"supersedes {c}  C" in out
    assert f"relates {c}  C" in out
    assert f"blocks {c}" not in out
    assert "superseded by" in store("show", c).out


def test_tree_json_items_carry_edges(store, abc):
    a, b, _ = abc
    link(store, a, "blocks", b)
    items = store("tree", "--json").json()["items"]
    assert {item["id"]: len(item["edges"]) for item in items} == {a: 1, b: 1, abc[2]: 0}
