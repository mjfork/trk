"""`trk ready`: the frontier, and the `ready` / `why_not` / `blocked` facts `show` and `tree` share."""

import sqlite3

import pytest

from gated import accept, new_flags, prove_close


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
    return trk


def new(trk, kind, title, *argv):
    result = trk("new", kind, title, *argv, *new_flags((kind, title, *argv)), "--json")
    assert result.code == 0, result.err
    return result.json()["item"]["id"]


def decision(trk, title, *argv):
    return new(trk, "work-item", title, "--form", "decision", "--subtype", "task", *argv)


def ticket(trk, title, *argv):
    return new(trk, "work-item", title, "--form", "spec-bound", *argv)


def ask(trk, tmp_path, parent, title, role="product"):
    """An ask from the agent `worker` (registered on first use), so it never addresses its asker."""
    trk("actor", "add", "worker", "--kind", "agent")
    question = tmp_path / "question.md"
    question.write_text("Which way?\n")
    result = trk("ask", parent, "--to", role, title, "--question", f"@{question}", "--as", "worker", "--json")
    assert result.code == 0, result.err
    return result.json()["item"]["id"]


def ready(trk, *argv):
    result = trk("ready", *argv, "--json")
    assert result.code == 0, result.err
    return [item["id"] for item in result.json()["items"]]


def why_not(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]["why_not"]


def close(trk, item_id, resolution="done", *argv):
    if resolution == "done":
        prove_close(trk, item_id)
    result = trk("close", item_id, "--resolution", resolution, "--outcome", "Done with it.", *argv)
    assert result.code == 0, result.err


def sql(trk_home, statement, params=()):
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def all_ids(trk_home):
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        return [r[0] for r in conn.execute("SELECT id FROM items ORDER BY rowid")]
    finally:
        conn.close()


def assert_show_agrees(trk, trk_home):
    """For every item in the store, `show` says ready exactly when `ready` returns it."""
    frontier = set(ready(trk))
    for item_id in all_ids(trk_home):
        item = trk("show", item_id, "--json").json()["item"]
        assert item["ready"] == (item_id in frontier), (item_id, item["why_not"])
        assert item["ready"] == (item["why_not"] == []), (item_id, item["why_not"])
        assert item["blocked"] == any(rule.startswith(("blocked-by:", "open-ask:")) for rule in item["why_not"])
        assert item["in_progress"] == ("claimed" in item["why_not"])


# --- AC 4 and AC 6 ---------------------------------------------------------


def test_ac4_urgent_first_then_tree_order_and_a_cleared_blocker(store, trk_home):
    p = new(store, "program", "P")
    j = new(store, "project", "J", "--parent", p, "--last")
    d = decision(store, "D", "--parent", p, "--last")
    e = decision(store, "E", "--parent", p, "--last")
    u = decision(store, "U", "--parent", p, "--last", "--urgent")
    assert store("link", e, "blocks", d).code == 0
    assert ready(store, "--root", p) == [u, j, e]
    assert why_not(store, d) == [f"blocked-by:{e}"]
    assert_show_agrees(store, trk_home)
    close(store, e, "wont-do")
    assert ready(store, "--root", p) == [u, j, d]
    assert_show_agrees(store, trk_home)


def test_ac6_after_a_supersede_the_blocked_item_waits_on_the_replacement(store, trk_home):
    d = decision(store, "D")
    d2 = decision(store, "D2")
    x = decision(store, "X")
    store("link", d, "blocks", x)
    close(store, d, "superseded", "--by", d2)
    assert why_not(store, x) == [f"blocked-by:{d2}"]
    assert_show_agrees(store, trk_home)


# --- the rules ---------------------------------------------------------------


def test_programs_are_never_ready(store, trk_home):
    p = new(store, "program", "P")
    assert why_not(store, p) == ["kind-never"]
    assert ready(store) == []


def test_projects_are_ready_only_while_draft(store, trk_home):
    j = new(store, "project", "J")
    assert ready(store) == [j]
    accept(store, j)
    assert why_not(store, j) == ["spec-not-draft"]
    assert ready(store) == []
    assert_show_agrees(store, trk_home)


def test_closed_items_are_not_ready(store, trk_home):
    d = decision(store, "D")
    close(store, d)
    assert why_not(store, d) == ["closed"]
    assert_show_agrees(store, trk_home)


def test_a_live_claim_makes_an_item_not_ready(store, trk_home):
    d = decision(store, "D")
    sql(trk_home, "INSERT INTO claims (item_id, session, actor, lease_minutes, taken_at, lease_until)"
                  " VALUES (?, 's-1', 'michael', 60, '2026-10-10T00:00:00.000Z', '2999-01-01T00:00:00.000Z')", (d,))
    assert why_not(store, d) == ["claimed"]
    assert store("show", d, "--json").json()["item"]["in_progress"] is True
    assert_show_agrees(store, trk_home)


def test_an_ended_claim_does_not_count(store, trk_home):
    d = decision(store, "D")
    sql(trk_home, "INSERT INTO claims (item_id, session, actor, lease_minutes, taken_at, lease_until, ended_at, \"end\")"
                  " VALUES (?, 's-1', 'michael', 60, '2026-10-10T00:00:00.000Z', '2026-10-10T01:00:00.000Z',"
                  " '2026-10-10T00:30:00.000Z', 'released')", (d,))
    assert ready(store) == [d]


def test_an_open_child_ask_makes_its_parent_not_ready(store, trk_home, tmp_path):
    t = new(store, "work-item", "T", "--form", "self-contained")
    a = ask(store, tmp_path, t, "Which way?")
    assert why_not(store, t) == [f"open-ask:{a}"]
    assert why_not(store, a) == ["kind-never"]
    assert store("show", t, "--json").json()["item"]["blocked"] is True
    assert_show_agrees(store, trk_home)
    sql(trk_home, "UPDATE items SET status = 'closed', resolution = 'done', outcome = 'Left.' WHERE id = ?", (a,))
    assert why_not(store, t) == []


def test_every_open_blocker_is_named_in_link_order(store, trk_home):
    a, b, c = decision(store, "A"), decision(store, "B"), decision(store, "C")
    store("link", b, "blocks", c)
    store("link", a, "blocks", c)
    assert why_not(store, c) == [f"blocked-by:{b}", f"blocked-by:{a}"]
    close(store, b)
    assert why_not(store, c) == [f"blocked-by:{a}"]


def test_only_blocks_edges_block(store, trk_home):
    a, b = decision(store, "A"), decision(store, "B")
    store("link", a, "relates", b)
    store("link", a, "supersedes", b)
    assert set(ready(store)) == {a, b}


def test_a_blocked_item_reports_every_rule_that_applies(store, trk_home):
    a, d = decision(store, "A"), decision(store, "D")
    store("link", a, "blocks", d)
    close(store, d)
    assert why_not(store, d) == ["closed", f"blocked-by:{a}"]


def test_ancestors_blockers_do_not_block_children(store, trk_home):
    a = decision(store, "A")
    t = new(store, "work-item", "T", "--form", "self-contained")
    child = decision(store, "child", "--parent", t)
    store("link", a, "blocks", t)
    assert child in ready(store)
    assert_show_agrees(store, trk_home)


# --- golden scenarios (spec Testing decisions) -------------------------------


def test_golden_nesting_children_of_not_ready_items_are_still_walked(store, trk_home):
    p = new(store, "program", "P")
    sub = new(store, "program", "Sub", "--parent", p)
    j = new(store, "project", "J", "--parent", sub)
    accept(store, j)
    t = ticket(store, "T", "--parent", j)
    d = decision(store, "D", "--parent", t)
    blocker = decision(store, "Blocker", "--parent", p)
    s = new(store, "work-item", "S", "--form", "self-contained", "--parent", p)
    under_s = decision(store, "Under S", "--parent", s)
    store("link", blocker, "blocks", s)
    assert ready(store) == [t, d, blocker, under_s]
    assert_show_agrees(store, trk_home)


def test_golden_an_ask_takes_only_its_direct_parent_off_the_frontier(store, trk_home, tmp_path):
    p = new(store, "program", "P")
    j = new(store, "project", "J", "--parent", p)
    j_decision = decision(store, "Under J", "--parent", j)
    k = new(store, "project", "K", "--parent", p)
    t = ticket(store, "T", "--parent", k)
    under_t = decision(store, "Under T", "--parent", t)
    accept(store, k)
    p_decision = decision(store, "Under P", "--parent", p)
    before = [j, j_decision, t, under_t, p_decision]
    assert ready(store, "--root", p) == before

    on_ticket = ask(store, tmp_path, t, "On the ticket")
    assert ready(store, "--root", p) == [j, j_decision, under_t, p_decision]
    assert why_not(store, t) == [f"open-ask:{on_ticket}"]
    assert why_not(store, k) == ["spec-not-draft"]
    assert_show_agrees(store, trk_home)

    on_project = ask(store, tmp_path, j, "On the project")
    assert ready(store, "--root", p) == [j_decision, under_t, p_decision]
    assert why_not(store, j) == [f"open-ask:{on_project}"]
    assert_show_agrees(store, trk_home)

    on_program = ask(store, tmp_path, p, "On the program")
    assert ready(store, "--root", p) == [j_decision, under_t, p_decision]
    assert why_not(store, p) == [f"open-ask:{on_program}", "kind-never"]
    for asked in (on_ticket, on_project, on_program):
        assert why_not(store, asked) == ["kind-never"]
    assert_show_agrees(store, trk_home)

    for asked in (on_ticket, on_project):
        sql(trk_home, "UPDATE items SET status = 'closed', resolution = 'done', outcome = 'x' WHERE id = ?", (asked,))
    assert ready(store, "--root", p) == before
    assert_show_agrees(store, trk_home)


@pytest.mark.parametrize("resolution", ["done", "wont-do", "superseded", "out-of-scope", "duplicate"])
def test_golden_a_blocker_closed_with_any_resolution_clears(store, trk_home, resolution):
    blocker, blocked, other = decision(store, "Blocker"), decision(store, "Blocked"), decision(store, "Other")
    store("link", blocker, "blocks", blocked)
    assert blocked not in ready(store)
    extra = {"superseded": ("--by", other), "duplicate": ("--of", other)}.get(resolution, ())
    close(store, blocker, resolution, *extra)
    if resolution == "superseded":
        # The blocks edge moved onto the replacement, which is still open.
        assert why_not(store, blocked) == [f"blocked-by:{other}"]
        close(store, other)
    assert blocked in ready(store)
    assert_show_agrees(store, trk_home)


def test_golden_spec_bound_tickets_follow_their_nearest_projects_spec(store, trk_home):
    p = new(store, "program", "P")
    outer = new(store, "project", "Outer", "--parent", p)
    inner = new(store, "project", "Inner", "--parent", outer)
    t_inner = ticket(store, "Under inner", "--parent", inner)
    t_outer = ticket(store, "Under outer", "--parent", outer)
    nested = ticket(store, "Nested", "--parent", t_inner)
    loose = ticket(store, "No project", "--parent", p)
    top = ticket(store, "Top level")
    assert why_not(store, t_inner) == ["spec-not-accepted"]
    assert why_not(store, loose) == ["no-project"]
    assert why_not(store, top) == ["no-project"]
    assert_show_agrees(store, trk_home)

    accept(store, outer)
    assert why_not(store, t_outer) == []
    assert why_not(store, t_inner) == ["spec-not-accepted"]  # nearest project is still draft
    assert_show_agrees(store, trk_home)

    accept(store, inner)
    assert ready(store) == [t_inner, nested, t_outer]
    assert_show_agrees(store, trk_home)

    store("spec", "draft", inner, "--reason", "Rethink.")
    assert why_not(store, nested) == ["spec-not-accepted"]
    assert_show_agrees(store, trk_home)


def test_golden_urgent_ordering_across_subtrees(store, trk_home):
    p1 = new(store, "program", "P1")
    a = decision(store, "A", "--parent", p1)
    b = decision(store, "B", "--parent", p1, "--urgent")
    p2 = new(store, "program", "P2")
    c = decision(store, "C", "--parent", p2, "--urgent")
    d = decision(store, "D", "--parent", p2)
    e = decision(store, "E", "--parent", c)
    new(store, "program", "P0", "--first")
    assert ready(store) == [b, c, a, e, d]
    assert ready(store, "--root", p2) == [c, e, d]
    assert_show_agrees(store, trk_home)


def test_golden_claimed_vs_lapsed_claims(store, trk_home, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-10T12:00:00.000Z")
    p = new(store, "program", "P")
    a = decision(store, "A", "--parent", p)
    b = decision(store, "B", "--parent", p)
    c = decision(store, "C", "--parent", p)
    assert store("claim", a, "--session", "s-1", "--lease", "30m").code == 0
    assert store("claim", b, "--session", "s-2", "--lease", "2h").code == 0
    assert ready(store) == [c]
    assert why_not(store, a) == why_not(store, b) == ["claimed"]
    assert_show_agrees(store, trk_home)

    monkeypatch.setenv("TRK_NOW", "2026-10-10T12:30:00.000Z")  # A's lease ends exactly now
    assert ready(store) == [a, c]
    assert why_not(store, a) == []
    assert why_not(store, b) == ["claimed"]
    assert_show_agrees(store, trk_home)

    monkeypatch.setenv("TRK_NOW", "2026-10-10T14:00:00.000Z")
    assert ready(store) == [a, b, c]
    assert_show_agrees(store, trk_home)


# --- flags ---------------------------------------------------------------------


def test_root_limits_the_walk_to_a_subtree_including_a_non_top_level_root(store, trk_home):
    p = new(store, "program", "P")
    t = new(store, "work-item", "T", "--form", "self-contained", "--parent", p)
    under = decision(store, "Under", "--parent", t)
    decision(store, "Sibling", "--parent", p)
    assert ready(store, "--root", t) == [t, under]


def test_root_unknown_exits_3(store):
    assert store("ready", "--root", "tr-zzzzz").code == 3


def test_owner_filters_on_effective_owner(store, trk_home):
    store("actor", "add", "bob", "--kind", "agent")
    p = new(store, "program", "P", "--owner", "bob")
    inherits = decision(store, "Inherits", "--parent", p)
    own = decision(store, "Own", "--parent", p, "--owner", "michael")
    loose = decision(store, "Loose")  # top-level, so the default owner michael owns it
    assert ready(store, "--owner", "bob") == [inherits]
    assert ready(store, "--owner", "michael") == [own, loose]
    assert loose in ready(store)


def test_owner_must_be_a_registered_actor(store):
    result = store("ready", "--owner", "nobody", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "unknown-actor"


def test_limit_truncates_after_ordering(store, trk_home):
    a = decision(store, "A")
    decision(store, "B")
    c = decision(store, "C", "--urgent")
    assert ready(store, "--limit", "2") == [c, a]


@pytest.mark.parametrize("bad", ["0", "-1", "x"])
def test_limit_must_be_positive(store, bad):
    assert store("ready", "--limit", bad).code == 2


# --- output ----------------------------------------------------------------------


def test_ready_json_uses_the_shared_item_shape(store, trk_home):
    d = decision(store, "D")
    listed = store("ready", "--json").json()
    assert listed["schema"] == 1
    assert listed["items"] == [store("show", d, "--json").json()["item"]]
    assert listed["items"][0]["ready"] is True
    assert listed["items"][0]["why_not"] == []
    assert listed["items"][0]["blocked"] is False


def test_ready_human_lists_one_line_per_item(store, trk_home):
    d = decision(store, "Do it", "--urgent")
    lines = store("ready").out.splitlines()
    assert lines == [f"{d}  work-item/decision (task)  Do it  urgent  @michael"]


def test_ready_with_nothing_ready_prints_nothing(store):
    assert store("ready").out == ""


def test_show_human_says_ready_or_why_not(store, trk_home):
    a, d = decision(store, "A"), decision(store, "D")
    store("link", a, "blocks", d)
    assert "  ready: yes" in store("show", a).out
    shown = store("show", d).out.splitlines()
    assert f"  ready: no (blocked-by:{a})" in shown
    assert "  blocked" in shown


def test_tree_marks_ready_claimed_and_blocked(store, trk_home):
    p = new(store, "program", "P")
    a = decision(store, "A", "--parent", p)
    b = decision(store, "B", "--parent", p)
    c = decision(store, "C", "--parent", p)
    store("link", a, "blocks", b)
    sql(trk_home, "INSERT INTO claims (item_id, session, actor, lease_minutes, taken_at, lease_until)"
                  " VALUES (?, 's-1', 'michael', 60, '2026-10-10T00:00:00.000Z', '2999-01-01T00:00:00.000Z')", (c,))
    lines = store("tree", p).out.splitlines()
    assert lines[0] == f"{p}  program  P  @michael"
    assert lines[1] == f"  {a}  work-item/decision (task)  A  [ready]  @michael"
    assert lines[2] == f"  {b}  work-item/decision (task)  B  [blocked]  @michael"
    assert lines[3] == f"  {c}  work-item/decision (task)  C  [claimed]  @michael"


def test_tree_marker_comes_before_the_owner(store, trk_home):
    d = decision(store, "D", "--owner", "michael")
    assert store("tree").out.splitlines() == [f"{d}  work-item/decision (task)  D  [ready]  @michael"]


def test_tree_json_items_carry_ready_and_why_not(store, trk_home):
    p = new(store, "program", "P")
    items = store("tree", "--json").json()["items"]
    assert items[0]["id"] == p
    assert items[0]["ready"] is False
    assert items[0]["why_not"] == ["kind-never"]
