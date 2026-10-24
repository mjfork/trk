"""Urgency scores for open asks (tr-qwwsc): held_up x cache factor x urgent multiplier, computed on read.

The clock is `$TRK_NOW`: asks are opened at chosen moments, then read at NOW."""

import re
from datetime import datetime, timedelta

import pytest

from feed_contract import validator
from test_asks import ask, decision, new
from trk import asks, needs_you, render, store

NOW = datetime.fromisoformat("2026-10-22T12:00:00+00:00")


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture
def store_(trk, monkeypatch, tmp_path):
    monkeypatch.setenv("TRK_NOW", stamp(NOW - timedelta(days=1)))
    trk("init", "--me", "michael")
    trk("actor", "add", "worker", "--kind", "agent")
    path = tmp_path / "question.md"
    path.write_text("Which?\n")
    trk.question = f"@{path}"
    return trk


def open_ask(trk, monkeypatch, parent, title, minutes_ago, *extra):
    """An ask on `parent` opened `minutes_ago` before NOW, to the parent's owner (michael)."""
    monkeypatch.setenv("TRK_NOW", stamp(NOW - timedelta(minutes=minutes_ago)))
    return ask(trk, parent, "parent", title, "--question", trk.question, *extra, "--as", "worker")["id"]


def read_at_now(monkeypatch):
    monkeypatch.setenv("TRK_NOW", stamp(NOW))


def waiting(trk):
    result = trk("waiting", "--all", "--json")
    assert result.code == 0, result.err
    return result.json()["asks"]


def by_id(listed):
    return {entry["id"]: entry["urgency"] for entry in listed}


# --- the curve ----------------------------------------------------------------------------------


def test_the_cache_factor_rises_to_the_peak_near_the_window_and_falls_back_to_one():
    assert asks.CACHE_WINDOW_MINUTES == 60 and asks.CACHE_PEAK == 10
    assert asks.cache_factor(0) == 1
    assert asks.cache_factor(30) == pytest.approx(3.25)
    assert asks.cache_factor(59) == pytest.approx(1 + 9 * (59 / 60) ** 2)
    assert asks.cache_factor(59) > 9.7
    assert asks.cache_factor(60) == 1
    assert asks.cache_factor(600) == 1


# --- ac-1: the ordering and the multiplier ---------------------------------------------------------


def test_a_warm_ask_at_55m_outranks_the_same_ask_at_5m_and_at_90m(store_, monkeypatch):
    parents = {age: decision(store_, f"D{age}") for age in (5, 55, 90)}
    opened = {age: open_ask(store_, monkeypatch, parent, f"A{age}", age) for age, parent in parents.items()}
    read_at_now(monkeypatch)

    listed = waiting(store_)
    assert [a["id"] for a in listed] == [opened[55], opened[5], opened[90]]
    scores = by_id(listed)
    assert scores[opened[55]] == {**scores[opened[55]], "score": 8.6, "held_up": 1, "minutes": 55, "cache_warm": True}
    assert scores[opened[5]]["score"] == 1.1
    assert scores[opened[90]] == {**scores[opened[90]], "score": 1.0, "minutes": 90, "cache_warm": False}


def test_held_up_3_outranks_held_up_1_at_equal_age(store_, monkeypatch):
    big = decision(store_, "Big")
    for title in ("T1", "T2"):
        blocked = decision(store_, title)
        assert store_("link", big, "blocks", blocked).code == 0
    small = decision(store_, "Small")
    a_small = open_ask(store_, monkeypatch, small, "small", 20)
    a_big = open_ask(store_, monkeypatch, big, "big", 20)
    read_at_now(monkeypatch)

    listed = waiting(store_)
    assert [a["id"] for a in listed] == [a_big, a_small]
    assert by_id(listed)[a_big]["held_up"] == 3
    assert by_id(listed)[a_big]["score"] == pytest.approx(3 * by_id(listed)[a_small]["score"], abs=0.1)
    assert by_id(listed)[a_small]["held_up"] == 1


def test_urgent_multiplies_by_ten_whether_the_ask_or_its_parent_is_urgent(store_, monkeypatch):
    plain, urgent_ask, urgent_parent = (decision(store_, name) for name in ("plain", "ua", "up"))
    assert store_("edit", urgent_parent, "--urgent").code == 0
    a_plain = open_ask(store_, monkeypatch, plain, "plain", 24)
    a_ask = open_ask(store_, monkeypatch, urgent_ask, "ask", 24, "--urgent")
    a_parent = open_ask(store_, monkeypatch, urgent_parent, "parent", 24)
    read_at_now(monkeypatch)

    scores = by_id(waiting(store_))
    assert (scores[a_plain]["base"], scores[a_ask]["base"], scores[a_parent]["base"]) == (1, 10, 10)
    assert scores[a_plain]["score"] == 2.4 and scores[a_plain]["urgent"] is False  # 1 x (1 + 9 x 0.4^2)
    assert scores[a_ask]["score"] == 24.4 and scores[a_ask]["urgent"] is True
    assert scores[a_parent]["score"] == 24.4 and scores[a_parent]["urgent"] is True


def test_an_urgent_ask_no_longer_outranks_everything(store_, monkeypatch):
    """Urgency is a x10 factor, not a lexicographic first key: a cold urgent ask (10) loses to a warm,
    non-urgent ask holding up many items."""
    root = decision(store_, "Root")
    for index in range(4):
        assert store_("link", root, "blocks", decision(store_, f"T{index}")).code == 0
    quiet = decision(store_, "Quiet")
    cold = open_ask(store_, monkeypatch, quiet, "cold urgent", 300, "--urgent")
    warm = open_ask(store_, monkeypatch, root, "warm wide", 59)
    read_at_now(monkeypatch)

    listed = waiting(store_)
    assert [a["id"] for a in listed] == [warm, cold]
    assert by_id(listed)[warm]["score"] == pytest.approx(5 * (1 + 9 * (59 / 60) ** 2), abs=0.1)
    assert by_id(listed)[cold]["score"] == 10.0


def test_waiting_all_json_carries_urgency_with_base_for_every_open_ask_in_one_call(store_, monkeypatch):
    big = decision(store_, "Big")
    for title in ("T1", "T2"):
        assert store_("link", big, "blocks", decision(store_, title)).code == 0
    opened = [open_ask(store_, monkeypatch, big, "big", 10), open_ask(store_, monkeypatch, decision(store_, "S"), "small", 200)]
    read_at_now(monkeypatch)
    listed = waiting(store_)
    assert sorted(a["id"] for a in listed) == sorted(opened)
    assert all(set(a["urgency"]) >= {"score", "base", "held_up", "minutes", "cache_warm"} for a in listed)
    assert by_id(listed)[opened[0]]["base"] == 3 and by_id(listed)[opened[1]]["base"] == 1


def test_equal_scores_tie_break_by_id(store_, monkeypatch):
    first, second = decision(store_, "one"), decision(store_, "two")
    opened = [open_ask(store_, monkeypatch, first, "a", 10), open_ask(store_, monkeypatch, second, "b", 10)]
    read_at_now(monkeypatch)
    assert [a["id"] for a in waiting(store_)] == sorted(opened)


# --- ac-1: held_up counts distinct open non-ask items ---------------------------------------------------


def test_blocks_diamonds_and_chains_count_each_item_once(store_, monkeypatch):
    """P blocks C and D, C blocks X and D blocks X (a diamond), and X sits under a second program. P, C, D and
    X are held up, each once. (`link` refuses a blocks cycle, so the walk's cycle guard cannot be reached here.)"""
    parent = new(store_, "program", "P")
    c = decision(store_, "C", "--parent", parent)
    d = decision(store_, "D", "--parent", parent)
    other = new(store_, "program", "Other")
    x = decision(store_, "X", "--parent", other)  # a cross-program edge counts
    for src, dst in ((parent, c), (parent, d), (c, x), (d, x)):
        result = store_("link", src, "blocks", dst)
        assert result.code == 0, result.err
    a = open_ask(store_, monkeypatch, parent, "q", 10)
    read_at_now(monkeypatch)

    assert by_id(waiting(store_))[a]["held_up"] == 4


def test_the_parents_containment_children_are_not_held_up(store_, monkeypatch):
    """An open ask keeps only its parent off the frontier (ready.py); the parent's children stay ready, and so
    do the children of what the parent blocks."""
    parent = decision(store_, "Parent")
    child = decision(store_, "Child", "--parent", parent)
    blocked = decision(store_, "Blocked")
    blocked_child = decision(store_, "Blocked child", "--parent", blocked)
    assert store_("link", parent, "blocks", blocked).code == 0
    a = open_ask(store_, monkeypatch, parent, "q", 10)
    read_at_now(monkeypatch)

    assert by_id(waiting(store_))[a]["held_up"] == 2  # Parent and Blocked; neither Child nor Blocked child
    assert store_("ready", "--json").code == 0
    ready = {item["id"] for item in store_("ready", "--json").json()["items"]}
    assert child in ready and blocked_child in ready and parent not in ready and blocked not in ready


def test_closed_items_and_other_asks_are_not_held_up_and_closed_items_do_not_carry_the_hold(store_, monkeypatch):
    parent = decision(store_, "Parent")
    open_child = decision(store_, "open")
    closed = decision(store_, "closed")
    behind_closed = decision(store_, "behind")
    for dst in (open_child, closed):
        assert store_("link", parent, "blocks", dst).code == 0
    assert store_("link", closed, "blocks", behind_closed).code == 0
    assert store_("close", closed, "--resolution", "wont-do", "--outcome", "Not needed.").code == 0
    open_ask(store_, monkeypatch, open_child, "sibling ask", 5)  # an ask is not an item held up
    a = open_ask(store_, monkeypatch, parent, "q", 10)
    read_at_now(monkeypatch)

    assert by_id(waiting(store_))[a]["held_up"] == 2  # Parent and the open child; not the closed one or what it blocked


# --- the clock: opened, reopened, readdressed ------------------------------------------------------------


def causes(ask_id):
    """The causes of an ask's `ask.readdressed` rows, oldest first."""
    st = store.open_store()
    try:
        return [row[0] for row in st.conn.execute(
            "SELECT json_extract(data, '$.cause') FROM journal WHERE item = ? AND change = 'ask.readdressed' ORDER BY seq",
            (ask_id,))]
    finally:
        st.close()


def reroute_at(monkeypatch, minutes_ago):
    monkeypatch.setenv("TRK_NOW", stamp(NOW - timedelta(minutes=minutes_ago)))


def test_minutes_restart_when_the_ask_is_readdressed_by_trk_readdress(store_, monkeypatch):
    store_("roles", "set", "product", "michael")
    parent = decision(store_, "D")
    a = open_ask(store_, monkeypatch, parent, "q", 90)
    reroute_at(monkeypatch, 40)
    assert store_("readdress", a, "--to", "product", "--as", "worker").code == 0
    read_at_now(monkeypatch)
    assert causes(a) == ["readdress"]
    assert by_id(waiting(store_))[a]["minutes"] == 40
    assert by_id(waiting(store_))[a]["cache_warm"] is True


def test_minutes_restart_when_the_ask_is_reopened(store_, monkeypatch):
    parent = decision(store_, "D")
    a = open_ask(store_, monkeypatch, parent, "q", 120)
    reroute_at(monkeypatch, 100)
    assert store_("answer", a, "--outcome", "Yes.", "--answer", store_.question, "--as", "michael").code == 0
    reroute_at(monkeypatch, 25)
    assert store_("reopen", a, "--reason", "Unsettled.", "--as", "worker").code == 0
    read_at_now(monkeypatch)
    assert by_id(waiting(store_))[a]["minutes"] == 25


def test_a_role_set_reroute_does_not_restart_the_wait(store_, monkeypatch):
    store_("actor", "add", "kim", "--kind", "human")
    store_("roles", "set", "product", "michael")
    parent = decision(store_, "D")
    monkeypatch.setenv("TRK_NOW", stamp(NOW - timedelta(minutes=90)))
    a = ask(store_, parent, "product", "q", "--question", store_.question, "--as", "worker")["id"]
    reroute_at(monkeypatch, 40)
    assert store_("roles", "set", "product", "kim").code == 0
    read_at_now(monkeypatch)
    assert causes(a) == ["role-set"]
    assert by_id(waiting(store_))[a]["minutes"] == 90
    assert by_id(waiting(store_))[a]["cache_warm"] is False


def test_an_owner_change_reroute_does_not_restart_the_wait(store_, monkeypatch):
    store_("actor", "add", "kim", "--kind", "human")
    parent = decision(store_, "D")
    a = open_ask(store_, monkeypatch, parent, "q", 90)
    reroute_at(monkeypatch, 40)
    assert store_("edit", parent, "--owner", "kim").code == 0
    read_at_now(monkeypatch)
    assert causes(a) == ["owner-changed"]
    assert by_id(waiting(store_))[a]["minutes"] == 90
    assert by_id(waiting(store_))[a]["cache_warm"] is False


def test_a_move_reroute_does_not_restart_the_wait(store_, monkeypatch):
    store_("actor", "add", "kim", "--kind", "human")
    home, away = new(store_, "program", "Home"), new(store_, "program", "Away")
    assert store_("edit", away, "--owner", "kim").code == 0
    parent = decision(store_, "D", "--parent", home)
    a = open_ask(store_, monkeypatch, parent, "q", 90)
    reroute_at(monkeypatch, 40)
    assert store_("move", parent, "--parent", away).code == 0
    read_at_now(monkeypatch)
    assert causes(a) == ["moved"]
    assert by_id(waiting(store_))[a]["minutes"] == 90


# --- the reason line -----------------------------------------------------------------------------------


@pytest.mark.parametrize("minutes_ago, urgent, expected", [
    (52, True, "score 77.6 · holds up 1 · waiting 52m (cache warm, 8m left) · urgent"),
    (24, False, "score 2.4 · holds up 1 · waiting 24m (cache warm, 36m left)"),
    (180, False, "score 1.0 · holds up 1 · waiting 3h (cache cold)"),
    (60, False, "score 1.0 · holds up 1 · waiting 1h (cache cold)"),
    (3000, False, "score 1.0 · holds up 1 · waiting 2d (cache cold)"),
])
def test_the_reason_line(store_, monkeypatch, minutes_ago, urgent, expected):
    parent = decision(store_, "D")
    a = open_ask(store_, monkeypatch, parent, "q", minutes_ago, *(["--urgent"] if urgent else []))
    read_at_now(monkeypatch)
    assert by_id(waiting(store_))[a]["reason"] == expected


@pytest.mark.parametrize("minutes_ago, expected", [
    (52, "score 7.8 · holds up 1 · waiting 52m (cache warm, 8m left, as of 12:00Z)"),
    (180, "score 1.0 · holds up 1 · waiting 3h (cache cold, as of 12:00Z)"),
])
def test_a_pages_reason_line_carries_the_render_time_and_the_json_line_does_not(store_, monkeypatch, minutes_ago, expected):
    a = open_ask(store_, monkeypatch, decision(store_, "D"), "q", minutes_ago)
    read_at_now(monkeypatch)
    st = store.open_store()
    try:
        row = st.conn.execute("SELECT * FROM items WHERE id = ?", (a,)).fetchone()
        assert asks.urgency(st.conn, row, NOW, as_of=True)["reason"] == expected
        assert "as of" not in asks.urgency(st.conn, row, NOW)["reason"]
    finally:
        st.close()


# --- ac-2: text, json and pages agree ------------------------------------------------------------------


@pytest.fixture
def fixture_store(store_, monkeypatch):
    """Three asks on one program and its decisions, in an order the old ordering (urgent, then oldest) gets
    wrong: the oldest is cold, the newest is urgent but young, and the middle one is warm and wide."""
    program = new(store_, "program", "P")
    cold_parent = decision(store_, "Cold", "--parent", program)
    wide_parent = decision(store_, "Wide", "--parent", program)
    for index in range(2):
        assert store_("link", wide_parent, "blocks", decision(store_, f"W{index}", "--parent", program)).code == 0
    young_parent = decision(store_, "Young", "--parent", program)
    made = {
        "cold": open_ask(store_, monkeypatch, cold_parent, "cold", 600),
        "wide": open_ask(store_, monkeypatch, wide_parent, "wide", 48),
        "young": open_ask(store_, monkeypatch, young_parent, "young", 3, "--urgent"),
    }
    read_at_now(monkeypatch)
    return {"program": program, **made}


def test_text_json_and_the_pages_give_one_order_and_one_reason_line(store_, fixture_store):
    program = fixture_store["program"]
    listed = waiting(store_)
    expected_order = [entry["id"] for entry in listed]
    assert expected_order == [fixture_store["wide"], fixture_store["young"], fixture_store["cold"]]
    reasons = [entry["urgency"]["reason"] for entry in listed]
    assert reasons[0].startswith("score 20.3 · holds up 3 · waiting 48m (cache warm, 12m left)")

    assert validator("waiting").is_valid({"schema": 1, "asks": listed})

    text = store_("waiting", "--all")
    assert text.code == 0, text.err
    lines = text.out.splitlines()
    assert [line.split()[0] for line in lines] == expected_order
    assert all(line.endswith(reason) for line, reason in zip(lines, reasons))

    st = store.open_store()
    try:
        program_page = render.render(st.conn, program)
        person_page = needs_you.render_page(st.conn, "michael")
    finally:
        st.close()
    for page in (program_page, person_page):
        assert re.findall(r'data-comment-id="ask-([^"]+)"', page) == expected_order
        assert re.findall(r"<dt>Urgency</dt><dd>(.*?)</dd>", page) == [with_render_time(reason) for reason in reasons]


def with_render_time(reason):
    """A page's Urgency line: `trk waiting`'s reason with the render time ended into the cache note."""
    return re.sub(r"\)( · urgent)?$", r", as of 12:00Z)\1", reason)


def test_the_json_urgency_key_is_additive_and_optional_in_the_contract(store_, fixture_store):
    reply = {"schema": 1, "asks": waiting(store_)}
    assert validator("waiting").is_valid(reply)
    without = {"schema": 1, "asks": [{k: v for k, v in entry.items() if k != "urgency"} for entry in reply["asks"]]}
    assert validator("waiting").is_valid(without), "an older producer's reply still validates"
    broken = {"schema": 1, "asks": [{**reply["asks"][0], "urgency": {**reply["asks"][0]["urgency"], "score": "high"}}]}
    assert not validator("waiting").is_valid(broken)
