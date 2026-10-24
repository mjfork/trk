"""tr-82j8x: an agent session's write names its actor (Michael's ruling tr-shdmq, 2026-10-22: "Refuse the
write when it comes from an agent session; your own terminal keeps working as you."). The signal is
$CLAUDECODE or $TRK_SESSION set; a plain shell keeps the store's default actor."""

import pytest

from gated import DEST
from test_asks import journal


@pytest.fixture
def store(trk):
    trk("init", "--me", "michael")
    trk("actor", "add", "worker", "--kind", "agent")
    return trk


def program(trk, *argv):
    return trk("new", "program", "P", *DEST, *argv, "--json")


@pytest.fixture(params=[("CLAUDECODE", "1"), ("TRK_SESSION", "s-1")], ids=["claudecode", "trk-session"])
def in_agent_session(request, monkeypatch):
    monkeypatch.setenv(*request.param)


def test_a_write_without_an_actor_is_refused_and_writes_nothing(store, trk_home, in_agent_session):
    before = len(journal(trk_home))
    result = program(store)
    assert result.code == 2
    error = result.error()
    assert error["rule"] == "actor-required"
    assert "--as" in error["message"] and "trk actors" in error["message"]
    assert len(journal(trk_home)) == before


def test_as_or_trk_actor_names_the_actor(store, trk_home, in_agent_session, monkeypatch):
    assert program(store, "--as", "worker").code == 0
    monkeypatch.setenv("TRK_ACTOR", "worker")
    assert program(store).code == 0
    assert {row["actor"] for row in journal(trk_home)[-2:]} == {"worker"}


def test_reads_are_never_refused(store, in_agent_session):
    assert store("waiting", "--json").code == 0
    assert store("ready", "--json").code == 0
    assert store("tree", "--json").code == 0


def test_claim_renew_by_the_session_hook_still_works(store, in_agent_session):
    assert store("claim", "renew", "--session", "s-1", "--json").code == 0


def test_a_plain_shell_keeps_the_default_actor(store, trk_home):
    assert program(store).code == 0
    assert journal(trk_home)[-1]["actor"] == "michael"
