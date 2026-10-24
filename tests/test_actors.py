import json
import sqlite3

import pytest
from gated import DEST


def last_row(trk_home):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM journal ORDER BY seq DESC LIMIT 1").fetchone())
    conn.close()
    return row


def test_writes_default_to_the_stores_default_actor(trk, trk_home):
    trk("init", "--me", "michael")
    trk("new", "program", "P", *DEST)
    assert last_row(trk_home)["actor"] == "michael"
    assert last_row(trk_home)["session"] is None


def test_trk_actor_then_as_override(trk, trk_home, monkeypatch):
    trk("init", "--me", "michael")
    monkeypatch.setenv("TRK_ACTOR", "herdr")
    trk("new", "program", "P", *DEST)
    assert last_row(trk_home)["actor"] == "herdr"
    trk("new", "program", "Q", "--as", "michael", *DEST)
    assert last_row(trk_home)["actor"] == "michael"


def test_unregistered_actor_is_refused(trk, trk_home, monkeypatch):
    trk("init", "--me", "michael")
    result = trk("new", "program", "P", "--as", "mallory", "--json", *DEST)
    assert result.code == 1
    assert result.error()["rule"] == "unknown-actor"
    monkeypatch.setenv("TRK_ACTOR", "mallory")
    assert trk("new", "program", "P", *DEST).code == 1


def test_session_from_flag_then_env(trk, trk_home, monkeypatch):
    trk("init", "--me", "michael")
    monkeypatch.setenv("TRK_SESSION", "env-session")
    monkeypatch.setenv("TRK_ACTOR", "michael")  # a session in the env is an agent session: name the actor
    trk("new", "program", "P", *DEST)
    assert last_row(trk_home)["session"] == "env-session"
    trk("new", "program", "Q", "--session", "flag-session", *DEST)
    assert last_row(trk_home)["session"] == "flag-session"


def test_init_prefix_and_display(trk, trk_home):
    trk("init", "--me", "michael", "--display", "Michael", "--prefix", "zz")
    assert trk("new", "program", "P", *DEST).out.startswith("zz-")
    conn = sqlite3.connect(trk_home / "store.db")
    first = json.loads(conn.execute("SELECT data FROM journal ORDER BY seq LIMIT 1").fetchone()[0])
    conn.close()
    assert first["display"] == "Michael"


def test_init_rejects_bad_actor_and_prefix(trk):
    assert trk("init", "--me", "Not A Slug").code == 2
    assert trk("init", "--me", "michael", "--prefix", "t-r").code == 2


def journal(trk_home):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM journal ORDER BY seq")]
    conn.close()
    for row in rows:
        row["data"] = json.loads(row["data"])
    return rows


def test_actor_add_registers_and_journals_with_a_null_item(trk, trk_home):
    trk("init", "--me", "michael")
    result = trk("actor", "add", "builder", "--kind", "agent", "--display", "Builder role", "--session", "s-1")
    assert result.code == 0, result.err
    row = journal(trk_home)[-1]
    assert row["change"] == "actor.added"
    assert (row["item"], row["root"], row["owner"], row["actor"], row["session"]) == (None, None, None, "michael", "s-1")
    assert row["data"] == {"id": "builder", "kind": "agent", "display": "Builder role"}


def test_actors_json_lists_every_actor_in_registration_order(trk):
    trk("init", "--me", "michael", "--display", "Michael")
    trk("actor", "add", "ana", "--kind", "human")
    actors = trk("actors", "--json").json()
    assert actors == {
        "schema": 1,
        "actors": [
            {"id": "michael", "kind": "human", "display": "Michael"},
            {"id": "herdr", "kind": "agent", "display": "herdr"},
            {"id": "publisher", "kind": "agent", "display": "publisher"},
            {"id": "ana", "kind": "human", "display": "ana"},
        ],
    }


def test_actors_human_output_has_one_line_each(trk):
    trk("init", "--me", "michael")
    lines = trk("actors").out.splitlines()
    assert lines == ["michael  human  michael", "herdr  agent  herdr", "publisher  agent  publisher"]


def test_actor_add_refuses_a_duplicate(trk, trk_home):
    trk("init", "--me", "michael")
    before = len(journal(trk_home))
    result = trk("actor", "add", "herdr", "--kind", "agent", "--json")
    assert result.code == 1
    assert (result.error()["rule"], result.error()["item"]) == ("actor-exists", "herdr")
    assert len(journal(trk_home)) == before


@pytest.mark.parametrize(
    "argv",
    [
        ("actor", "add", "Builder", "--kind", "agent"),
        ("actor", "add", "a b", "--kind", "agent"),
        ("actor", "add", "-x", "--kind", "agent"),
        ("actor", "add", "builder"),
        ("actor", "add", "builder", "--kind", "robot"),
        ("actor", "add", "builder", "--kind", "agent", "--display", " "),
        ("actor", "edit", "herdr"),
        ("actor", "edit", "herdr", "--display", ""),
        ("actor", "remove", "herdr"),
    ],
)
def test_actor_misuse_is_a_usage_error(trk, argv):
    trk("init", "--me", "michael")
    result = trk(*argv, "--json")
    assert result.code == 2 and result.error()["code"] == "usage"


def test_actor_edit_changes_display_and_journals_updated(trk, trk_home):
    trk("init", "--me", "michael")
    assert trk("actor", "edit", "herdr", "--display", "herdr plugin").code == 0
    row = journal(trk_home)[-1]
    assert (row["change"], row["item"]) == ("actor.updated", None)
    assert row["data"] == {"id": "herdr", "kind": "agent", "display": "herdr plugin"}
    assert trk("actors", "--json").json()["actors"][1]["display"] == "herdr plugin"


def test_actor_edit_with_the_same_display_writes_no_row(trk, trk_home):
    trk("init", "--me", "michael")
    before = len(journal(trk_home))
    assert trk("actor", "edit", "herdr", "--display", "herdr").code == 0
    assert len(journal(trk_home)) == before


def test_actor_edit_unknown_exits_3(trk):
    trk("init", "--me", "michael")
    result = trk("actor", "edit", "nobody", "--display", "X", "--json")
    assert result.code == 3 and result.error()["item"] == "nobody"


def test_added_actor_can_write(trk, trk_home):
    trk("init", "--me", "michael")
    trk("actor", "add", "builder", "--kind", "agent")
    assert trk("new", "program", "P", "--as", "builder", *DEST).code == 0
    assert journal(trk_home)[-1]["actor"] == "builder"
