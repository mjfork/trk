"""Readdressing (spec 02: Readdressing, Roles' `roles set`, Feed rows' `ask.readdressed`): the explicit
`trk readdress` verb, and the implicit rows every write that may change what an open ask resolves to
writes, checked by the spec's resolution table test."""

import pytest

from test_asks import ask, decision, journal, new, refusal, show


@pytest.fixture
def store(trk):
    """michael (default owner), kim (human) holding product, bot (agent) holding engineering, and two
    agents: asker, who opens every ask, and worker."""
    trk("init", "--me", "michael")
    for actor, kind in (("kim", "human"), ("bot", "agent"), ("asker", "agent")):   # worker: trk init
        assert trk("actor", "add", actor, "--kind", kind).code == 0
    assert trk("roles", "set", "product", "kim").code == 0
    assert trk("roles", "set", "engineering", "bot").code == 0
    return trk


@pytest.fixture
def question(tmp_path):
    path = tmp_path / "question.md"
    path.write_text("Which?\n")
    return f"@{path}"


def readdressed(rows):
    return [row for row in rows if row["change"] == "ask.readdressed"]


def after(trk_home, seq):
    return [row for row in journal(trk_home) if row["seq"] > seq]


def head(trk_home):
    return journal(trk_home)[-1]["seq"]


# --- the resolution table -------------------------------------------------------------


@pytest.fixture
def table(store, question):
    """P (michael) > J (inherits) > T (own owner worker) and U (inherits); K (own owner bot) under P;
    Top-level decision Top owned by kim. Every ask is opened by asker:

    - prod, eng: function roles, under T
    - nested: `parent` under the nested ticket T (J's effective owner)
    - top: `parent` under the top-level Top (Top's own owner)
    - inherit: `parent` under U, which inherits its owner
    """
    p = new(store, "program", "P")
    j = new(store, "project", "J", "--parent", p)
    k = new(store, "project", "K", "--parent", p, "--owner", "bot")
    t = decision(store, "T", "--parent", j, "--owner", "worker")
    u = decision(store, "U", "--parent", j)
    top = decision(store, "Top", "--owner", "kim")

    def opened(parent, role, title):
        return ask(store, parent, role, title, "--question", question, "--as", "asker")["id"]

    asks = {
        "prod": opened(t, "product", "Prod?"),
        "eng": opened(t, "engineering", "Eng?"),
        "nested": opened(t, "parent", "Nested?"),
        "top": opened(top, "parent", "Top?"),
        "inherit": opened(u, "parent", "Inherit?"),
    }
    return {"items": {"p": p, "j": j, "k": k, "t": t, "u": u, "top": top}, "asks": asks}


def resolved(trk, table):
    return {name: show(trk, ask_id)["addressee"] for name, ask_id in table["asks"].items()}


BASELINE = {"prod": "kim", "eng": "bot", "nested": "michael", "top": "kim", "inherit": "michael"}


def check_step(trk, trk_home, table, argv, cause, expected, changed):
    """Run one write; the table resolves to `expected` and exactly the `changed` asks got one
    `ask.readdressed` row each (old -> new, `cause`), all after the row that caused them."""
    before = resolved(trk, table)
    seq = head(trk_home)
    result = trk(*argv)
    assert result.code == 0, result.err
    assert resolved(trk, table) == expected
    rows = after(trk_home, seq)
    got = readdressed(rows)
    names = {ask_id: name for name, ask_id in table["asks"].items()}
    assert sorted(names[row["item"]] for row in got) == sorted(changed)
    for row in got:
        name = names[row["item"]]
        assert (row["data"]["old_addressee"], row["addressee"]) == (before[name], expected[name])
        assert row["data"]["cause"] == cause
        assert row["data"]["self_addressed"] is False
    causing = [row for row in rows if row["change"] != "ask.readdressed"]
    assert rows[:len(causing)] == causing, "every readdressed row follows the rows that caused it"
    return rows


def test_the_table_resolves_every_case(store, table):
    assert resolved(store, table) == BASELINE


def test_roles_set_reroutes_only_the_asks_on_that_role(store, trk_home, table):
    check_step(store, trk_home, table, ("roles", "set", "product", "michael"), "role-set",
               {**BASELINE, "prod": "michael"}, ["prod"])


def test_roles_set_on_a_role_no_open_ask_uses_writes_no_readdress(store, trk_home, table):
    check_step(store, trk_home, table, ("roles", "set", "design", "kim"), "role-set", BASELINE, [])


def test_edit_owner_on_an_ancestor_reroutes_the_parent_asks_beneath_it(store, trk_home, table):
    p = table["items"]["p"]
    rows = check_step(store, trk_home, table, ("edit", p, "--owner", "kim"), "owner-changed",
                      {**BASELINE, "nested": "kim", "inherit": "kim"}, ["nested", "inherit"])
    assert rows[0]["change"] == "owner.changed"


def test_edit_owner_on_the_blocked_item_itself_changes_nothing_for_a_nested_ask(store, trk_home, table):
    check_step(store, trk_home, table, ("edit", table["items"]["t"], "--owner", "kim"), "owner-changed", BASELINE, [])


def test_edit_owner_on_a_top_level_blocked_item_reroutes_its_parent_ask(store, trk_home, table):
    check_step(store, trk_home, table, ("edit", table["items"]["top"], "--owner", "michael"), "owner-changed",
               {**BASELINE, "top": "michael"}, ["top"])


def test_move_of_an_ancestor_reroutes_the_parent_asks_beneath_it(store, trk_home, table):
    t, k = table["items"]["t"], table["items"]["k"]
    rows = check_step(store, trk_home, table, ("move", t, "--parent", k), "moved",
                      {**BASELINE, "nested": "bot"}, ["nested"])
    assert rows[0]["change"] == "item.moved"


def test_move_top_writes_moved_top_and_no_readdress_when_nothing_resolves_differently(store, trk_home, table):
    rows = check_step(store, trk_home, table, ("move", table["items"]["u"], "--top"), "moved", BASELINE, [])
    assert [row["change"] for row in rows] == ["owner.changed", "item.moved"]


def test_a_lifted_item_keeps_its_parent_asks_where_they_were(store, trk_home, table):
    """With J owned by bot, U lifts with bot as its own owner: `inherit` still resolves to bot, so the
    lift writes no row; re-owning U at the top then reroutes it."""
    store("edit", table["items"]["j"], "--owner", "bot")
    expected = {**BASELINE, "nested": "bot", "inherit": "bot"}
    assert resolved(store, table) == expected
    rows = check_step(store, trk_home, table, ("move", table["items"]["u"], "--top"), "moved", expected, [])
    assert [row["change"] for row in rows] == ["owner.changed", "item.moved"]
    check_step(store, trk_home, table, ("edit", table["items"]["u"], "--owner", "worker"), "owner-changed",
               {**expected, "inherit": "worker"}, ["inherit"])


def test_a_reorder_writes_no_readdress(store, trk_home, table):
    check_step(store, trk_home, table, ("move", table["items"]["u"], "--first"), "moved", BASELINE, [])


def test_a_closed_ask_is_never_readdressed(store, trk_home, table):
    prod = table["asks"]["prod"]
    assert store("close", prod, "--resolution", "duplicate", "--of", table["asks"]["eng"], "--outcome", "Same question.", "--as", "asker").code == 0
    seq = head(trk_home)
    assert store("roles", "set", "product", "michael").code == 0
    assert readdressed(after(trk_home, seq)) == []


def test_an_implicit_readdress_onto_the_asker_is_allowed_and_flagged(store, trk_home, table):
    seq = head(trk_home)
    assert store("roles", "set", "product", "asker").code == 0
    rows = readdressed(after(trk_home, seq))
    assert [row["item"] for row in rows] == [table["asks"]["prod"]]
    assert rows[0]["addressee"] == "asker"
    assert rows[0]["data"]["self_addressed"] is True


def test_implicit_rows_carry_the_feed_fields(store, trk_home, table):
    seq = head(trk_home)
    store("roles", "set", "engineering", "worker")
    (row,) = readdressed(after(trk_home, seq))
    assert row["item"] == table["asks"]["eng"]
    assert (row["kind"], row["form"], row["parent"]) == ("work-item", "ask", table["items"]["t"])
    assert row["owner"] == "worker"  # the wake owner: T's effective owner
    assert (row["status_old"], row["status_new"]) == (None, None)
    assert row["data"] == {"old_addressee": "bot", "old_role": "engineering", "addressee_role": "engineering",
                           "addressee_kind": "agent", "cause": "role-set", "self_addressed": False}


# --- trk readdress --------------------------------------------------------------------


@pytest.fixture
def one(store, question):
    """Program P (michael) > decision D, with asker's ask to product (kim)."""
    p = new(store, "program", "P")
    d = decision(store, "D", "--parent", p)
    a = ask(store, d, "product", "Q?", "--question", question, "--as", "asker")["id"]
    return {"p": p, "d": d, "ask": a}


def test_readdress_changes_the_role_and_writes_one_row(store, trk_home, one):
    seq = head(trk_home)
    result = store("readdress", one["ask"], "--to", "engineering", "--as", "asker", "--json")
    assert result.code == 0, result.err
    item = result.json()["item"]
    assert (item["addressee_role"], item["addressee"], item["addressee_kind"]) == ("engineering", "bot", "agent")
    rows = after(trk_home, seq)
    assert [row["change"] for row in rows] == ["ask.readdressed"]
    row = rows[0]
    assert (row["item"], row["actor"], row["addressee"], row["owner"]) == (one["ask"], "asker", "bot", "michael")
    assert row["data"] == {"old_addressee": "kim", "old_role": "product", "addressee_role": "engineering",
                           "addressee_kind": "agent", "cause": "readdress", "self_addressed": False}


def test_readdress_prints_the_id_without_json(store, one):
    result = store("readdress", one["ask"], "--to", "parent", "--as", "asker")
    assert (result.code, result.out.strip()) == (0, one["ask"])


def test_readdress_to_the_same_role_writes_nothing(store, trk_home, one):
    seq = head(trk_home)
    result = store("readdress", one["ask"], "--to", "product", "--as", "asker", "--json")
    assert result.code == 0, result.err
    assert after(trk_home, seq) == []


def test_readdress_to_another_role_held_by_the_same_actor_still_writes_a_row(store, trk_home, one):
    store("roles", "set", "design", "kim")
    seq = head(trk_home)
    assert store("readdress", one["ask"], "--to", "design", "--as", "asker").code == 0
    (row,) = after(trk_home, seq)
    assert (row["change"], row["data"]["old_addressee"], row["addressee"]) == ("ask.readdressed", "kim", "kim")
    assert (row["data"]["old_role"], row["data"]["addressee_role"]) == ("product", "design")


def test_readdress_refuses_an_unknown_role_and_a_self_addressed_one(store, one):
    assert refusal(store("readdress", one["ask"], "--to", "finance", "--json")) == (1, "unknown-role")
    store("roles", "set", "design", "asker")
    assert refusal(store("readdress", one["ask"], "--to", "design", "--json")) == (1, "self-addressed")


def test_readdress_refuses_what_is_not_an_open_ask(store, one, question):
    assert store("readdress", "zz-nope", "--to", "engineering", "--json").code == 3
    assert refusal(store("readdress", one["d"], "--to", "engineering", "--json")) == (1, "not-ask")
    other = ask(store, one["d"], "product", "Again?", "--question", question, "--as", "asker")["id"]
    assert store("close", one["ask"], "--resolution", "duplicate", "--of", other, "--outcome", "Same question.", "--as", "asker").code == 0
    assert refusal(store("readdress", one["ask"], "--to", "engineering", "--json")) == (1, "closed")


def test_readdress_refusal_names_who_may(store, one):
    result = store("readdress", one["ask"], "--to", "engineering", "--as", "worker", "--json")
    assert refusal(result) == (1, "ask-permission")
    message = result.error()["message"]
    for who in ("asker", "michael", "kim", "human"):
        assert who in message


def test_readdress_permission_is_checked_before_the_role(store, one):
    result = store("readdress", one["ask"], "--to", "finance", "--as", "worker", "--json")
    assert refusal(result) == (1, "ask-permission")


def test_tree_shows_an_asks_role_and_actor_in_place_of_the_owner(store, one):
    lines = store("tree", one["p"]).out.splitlines()
    (line,) = [line for line in lines if line.strip().startswith(one["ask"])]
    assert line.endswith("→ product (kim)")
    assert "@" not in line
    (decision_line,) = [line for line in lines if line.strip().startswith(one["d"])]
    assert decision_line.endswith("@michael")
