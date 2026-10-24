"""Waiting on me (spec 02): `trk waiting [--me A | --all] [--root R] [--json]`, with AC 5 as written."""

import pytest

from test_asks import ask, decision, journal, new, refusal

T0 = "2026-10-11T12:00:00.000Z"


@pytest.fixture
def store(trk, monkeypatch):
    """The spec's acceptance store: init --me michael, then kim (human) and worker (agent)."""
    monkeypatch.setenv("TRK_NOW", T0)
    trk("init", "--me", "michael")
    trk("actor", "add", "kim", "--kind", "human", "--display", "Kim")
    trk("actor", "add", "worker", "--kind", "agent")
    return trk


@pytest.fixture
def question(tmp_path):
    path = tmp_path / "question.md"
    path.write_text("Which?\n")
    return f"@{path}"


def waiting(trk, *argv):
    result = trk("waiting", *argv, "--json")
    assert result.code == 0, result.err
    reply = result.json()
    assert reply["schema"] == 1
    return reply["asks"]


def ids(asks):
    return [a["id"] for a in asks]


def at(monkeypatch, moment):
    monkeypatch.setenv("TRK_NOW", moment)


# --- AC 5, as written -------------------------------------------------------------------


def test_ac5_waiting_follows_roles_set(store, trk_home, question):
    """From AC 3's store: product held by kim, worker's ask under D."""
    store("roles", "set", "product", "kim")
    p = new(store, "program", "P")
    j = new(store, "project", "J", "--parent", p)
    d = decision(store, "D", "--parent", j)
    assert store("claim", d, "--session", "s-1").code == 0
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "worker")["id"]

    assert ids(waiting(store, "--me", "kim")) == [a]
    assert store("roles", "set", "product", "michael").code == 0
    row = journal(trk_home)[-1]
    assert (row["change"], row["item"], row["data"]["cause"]) == ("ask.readdressed", a, "role-set")
    assert (row["data"]["old_addressee"], row["addressee"]) == ("kim", "michael")
    assert waiting(store, "--me", "kim") == []
    assert ids(waiting(store, "--me", "michael")) == [a]


# --- who, which and in what order ---------------------------------------------------------


@pytest.fixture
def world(store, question, monkeypatch):
    """P > D and E, Q (top-level, owned by kim). product -> kim. Asks, oldest first:
    old (D, product), mine (D, parent -> michael), urgent (E, product, urgent, newest), top (Q, product)."""
    store("roles", "set", "product", "kim")
    p = new(store, "program", "P")
    d = decision(store, "D", "--parent", p)
    e = decision(store, "E", "--parent", p)
    q = decision(store, "Q", "--owner", "kim")
    opened = {}
    for moment, name, parent, role, extra in (
        ("2026-10-11T09:00:00.000Z", "old", d, "product", ()),
        ("2026-10-11T10:00:00.000Z", "mine", d, "parent", ()),
        ("2026-10-11T11:00:00.000Z", "top", q, "product", ()),
        ("2026-10-11T11:30:00.000Z", "urgent", e, "product", ("--urgent",)),
    ):
        at(monkeypatch, moment)
        opened[name] = ask(store, parent, role, name, "--question", question, *extra, "--as", "worker")["id"]
    at(monkeypatch, T0)
    return {"p": p, "d": d, "e": e, "q": q, **opened}


def test_waiting_defaults_to_the_writing_actor(store, world):
    assert ids(waiting(store, "--as", "kim")) == [world["urgent"], *sorted([world["old"], world["top"]])]
    assert ids(waiting(store)) == [world["mine"]]  # the default actor, michael


def test_waiting_all_lists_every_open_ask_highest_urgency_score_first_then_by_id(store, world):
    """urgent scores 32.5 (30m warm, x10); the other three are cold at 1.0 and tie, so id decides."""
    assert ids(waiting(store, "--all")) == [world["urgent"], *sorted([world["old"], world["mine"], world["top"]])]


def test_same_moment_asks_order_by_id(store, world, question):
    second = ask(store, world["d"], "product", "second", "--question", question, "--as", "worker")["id"]
    third = ask(store, world["e"], "product", "third", "--question", question, "--as", "worker")["id"]
    assert ids(waiting(store, "--me", "kim"))[1:] == sorted([world["old"], world["top"], second, third])


def test_waiting_root_limits_to_one_subtree(store, world):
    assert ids(waiting(store, "--all", "--root", world["p"])) == [world["urgent"], *sorted([world["old"], world["mine"]])]
    assert ids(waiting(store, "--me", "kim", "--root", world["d"])) == [world["old"]]
    assert ids(waiting(store, "--me", "kim", "--root", world["q"])) == [world["top"]]
    assert ids(waiting(store, "--all", "--root", world["old"])) == [world["old"]]


def test_closed_asks_leave_the_list(store, world):
    assert store("close", world["old"], "--resolution", "duplicate", "--of", world["urgent"], "--outcome", "Same question.", "--as", "worker").code == 0
    assert world["old"] not in ids(waiting(store, "--all"))


def test_waiting_json_is_the_item_plus_parent_and_age(store, world):
    (first, *_) = waiting(store, "--me", "kim")
    assert first["id"] == world["urgent"]
    assert (first["form"], first["addressee_role"], first["addressee"], first["asker"]) == ("ask", "product", "kim", "worker")
    assert first["urgent"] is True
    assert first["parent"] == {"id": world["e"], "title": "E", "kind": "work-item", "form": "decision"}
    assert first["age_seconds"] == 30 * 60


def test_waiting_text_is_one_line_per_ask(store, world):
    result = store("waiting", "--me", "kim")
    assert result.code == 0, result.err
    lines = result.out.splitlines()
    cold = sorted([
        (world["old"], f"{world['old']}  product (kim)  {world['d']} D  old  score 1.0 · holds up 1 · waiting 3h (cache cold)"),
        (world["top"], f"{world['top']}  product (kim)  {world['q']} Q  top  score 1.0 · holds up 1 · waiting 1h (cache cold)"),
    ])
    assert lines == [
        f"{world['urgent']}  ! product (kim)  {world['e']} E  urgent  "
        "score 32.5 · holds up 1 · waiting 30m (cache warm, 30m left) · urgent",
        *(line for _, line in cold),
    ]


def test_waiting_text_is_empty_when_nothing_waits(store, world):
    result = store("waiting", "--me", "worker")
    assert (result.code, result.out) == (0, "")


def test_waiting_refuses_an_unknown_actor_and_me_with_all(store, world):
    assert refusal(store("waiting", "--me", "nobody", "--json")) == (1, "unknown-actor")
    assert store("waiting", "--me", "kim", "--all").code == 2
    assert store("waiting", "--root", "zz-nope0", "--all", "--json").code == 3
