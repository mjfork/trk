"""Leased claims: claim, claim --next, renew, release, sweep, claims, and close ending a claim."""

import json
import sqlite3

import pytest

from feed_contract import feed_validator
from gated import accept, new_flags, resolve

T0 = "2026-10-10T12:00:00.000Z"


@pytest.fixture
def store(trk, monkeypatch):
    monkeypatch.setenv("TRK_NOW", T0)
    trk("init", "--me", "michael")
    return trk


def at(monkeypatch, moment):
    monkeypatch.setenv("TRK_NOW", moment)


def new(trk, kind, title, *argv):
    result = trk("new", kind, title, *argv, *new_flags((kind, title, *argv)), "--json")
    assert result.code == 0, result.err
    return result.json()["item"]["id"]


def decision(trk, title, *argv):
    return new(trk, "work-item", title, "--form", "decision", "--subtype", "task", *argv)


def claim(trk, *argv):
    return trk("claim", *argv, "--json")


def show(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


def rows(trk_home, change=None):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        found = [dict(r) for r in conn.execute("SELECT * FROM journal ORDER BY seq")]
    finally:
        conn.close()
    for row in found:
        row["data"] = json.loads(row["data"])
    return [r for r in found if change is None or r["change"] == change]


def sql(trk_home, statement, params=()):
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def live_claims(trk, *argv):
    result = trk("claims", *argv, "--json")
    assert result.code == 0, result.err
    return result.json()["claims"]


# --- claim <id> -------------------------------------------------------------


def test_claim_takes_a_lease_of_the_default_length(store, trk_home):
    d = decision(store, "D")
    result = claim(store, d, "--session", "s-1")
    assert result.code == 0, result.err
    assert result.json()["claimed"]["id"] == d
    assert result.json()["claimed"]["claim"] == {
        "session": "s-1", "actor": "michael", "lease_until": "2026-10-10T13:00:00.000Z", "taken_at": T0, "scope": "build",
    }
    [taken] = rows(trk_home, "claim.taken")
    assert taken["item"] == d and taken["session"] == "s-1" and taken["actor"] == "michael"
    assert taken["data"] == {"claim_actor": "michael", "lease_until": "2026-10-10T13:00:00.000Z", "scope": "build", "forced": False}


def test_claim_prints_the_id_without_json(store):
    d = decision(store, "D")
    result = store("claim", d, "--session", "s-1")
    assert (result.code, result.out) == (0, f"{d}\n")


@pytest.mark.parametrize("lease, until", [("45m", "12:45"), ("2h", "14:00"), ("90", "13:30"), ("1h30m", "13:30")])
def test_claim_lease_durations(store, lease, until):
    d = decision(store, "D")
    result = claim(store, d, "--session", "s-1", "--lease", lease)
    assert result.json()["claimed"]["claim"]["lease_until"] == f"2026-10-10T{until}:00.000Z"


@pytest.mark.parametrize("lease", ["0m", "abc", "-5m", "m", "5x", ""])
def test_claim_refuses_a_bad_lease(store, lease):
    d = decision(store, "D")
    assert claim(store, d, "--session", "s-1", "--lease", lease).code == 2


def test_claim_needs_a_session(store):
    d = decision(store, "D")
    assert claim(store, d).code == 2


def test_claim_takes_the_session_from_the_environment(store, monkeypatch):
    d = decision(store, "D")
    monkeypatch.setenv("TRK_SESSION", "s-env")
    monkeypatch.setenv("TRK_ACTOR", "michael")  # a session in the env is an agent session: name the actor
    assert claim(store, d).json()["claimed"]["claim"]["session"] == "s-env"


def test_claim_as_another_actor_records_the_claimant_and_keeps_the_owner(store, trk_home):
    store("actor", "add", "builder", "--kind", "agent")
    d = decision(store, "D", "--owner", "michael")
    result = claim(store, d, "--session", "s-1", "--as", "builder")
    assert result.json()["claimed"]["claim"]["actor"] == "builder"
    assert result.json()["claimed"]["owner"] == "michael"
    [taken] = rows(trk_home, "claim.taken")
    assert taken["actor"] == "builder" and taken["data"]["claim_actor"] == "builder"
    assert rows(trk_home, "owner.changed") == []


def test_claim_unknown_item_exits_3(store):
    assert claim(store, "tr-zzzzz", "--session", "s-1").code == 3


def test_another_sessions_live_claim_refuses_with_exit_4(store):
    d = decision(store, "D")
    claim(store, d, "--session", "s-1")
    result = claim(store, d, "--session", "s-2")
    assert result.code == 4
    assert result.error()["rule"] == "claimed" and result.error()["item"] == d
    assert claim(store, d, "--session", "s-2", "--force").code == 4


def test_the_same_session_claiming_again_renews(store, trk_home, monkeypatch):
    d = decision(store, "D")
    claim(store, d, "--session", "s-1")
    at(monkeypatch, "2026-10-10T12:10:00.000Z")
    result = claim(store, d, "--session", "s-1", "--lease", "30m")
    assert result.code == 0, result.err
    assert result.json()["claimed"]["claim"]["lease_until"] == "2026-10-10T12:40:00.000Z"
    assert result.json()["claimed"]["claim"]["taken_at"] == T0
    [renewed] = rows(trk_home, "claim.renewed")
    assert renewed["data"] == {"claim_actor": "michael", "lease_until": "2026-10-10T12:40:00.000Z", "scope": "build"}
    assert len(live_claims(store)) == 1


def test_closed_items_are_unclaimable(store):
    d = decision(store, "D")
    resolve(store, d)
    store("close", d, "--resolution", "done", "--outcome", "Done.")
    for extra in ((), ("--force",)):
        result = claim(store, d, "--session", "s-1", *extra)
        assert (result.code, result.error()["rule"], result.error()["item"]) == (1, "unclaimable", d)


def test_asks_are_unclaimable_even_with_force(store, trk_home):
    t = new(store, "work-item", "T", "--form", "self-contained")
    sql(trk_home, "INSERT INTO items (id, kind, form, title, parent_id, rank, created_at, updated_at)"
                  " VALUES ('tr-ask01', 'work_item', 'ask', 'Q?', ?, 'V', ?, ?)", (t, T0, T0))
    for extra in ((), ("--force",)):
        result = claim(store, "tr-ask01", "--session", "s-1", *extra)
        assert (result.code, result.error()["rule"]) == (1, "unclaimable")


def test_programs_need_force(store, trk_home):
    p = new(store, "program", "P")
    result = claim(store, p, "--session", "s-1")
    assert (result.code, result.error()["rule"]) == (1, "unclaimable")
    assert "--force" in result.error()["message"]
    forced = claim(store, p, "--session", "s-1", "--force")
    assert forced.code == 0, forced.err
    assert rows(trk_home, "claim.taken")[0]["data"]["forced"] is True


def test_a_not_ready_item_is_refused_with_its_why_not_rules(store):
    blocker = decision(store, "B")
    d = decision(store, "D")
    store("link", blocker, "blocks", d)
    result = claim(store, d, "--session", "s-1")
    assert (result.code, result.error()["rule"], result.error()["item"]) == (1, "not-ready", d)
    assert f"blocked-by:{blocker}" in result.error()["message"]


def test_force_does_not_override_not_ready(store, trk_home):
    blocker = decision(store, "B")
    d = decision(store, "D")
    store("link", blocker, "blocks", d)
    project = new(store, "project", "J")
    accept(store, project)
    for item, rule in ((d, f"blocked-by:{blocker}"), (project, None)):
        result = claim(store, item, "--session", "s-1", "--force")
        assert (result.code, result.error()["rule"], result.error()["item"]) == (1, "not-ready", item)
        assert "--force" not in result.error()["message"]
        assert rule is None or rule in result.error()["message"]
    assert rows(trk_home, "claim.taken") == []


def test_force_on_a_ready_item_is_not_recorded_as_forced(store, trk_home):
    d = decision(store, "D")
    assert claim(store, d, "--session", "s-1", "--force").code == 0
    assert rows(trk_home, "claim.taken")[0]["data"]["forced"] is False


def test_a_claim_shows_on_the_item(store):
    d = decision(store, "D")
    claim(store, d, "--session", "s-1")
    item = show(store, d)
    assert item["in_progress"] is True and item["why_not"] == ["claimed"]
    assert item["claim"]["session"] == "s-1"
    human = store("show", d).out
    assert "  claimed: michael, session s-1, lease until 2026-10-10T13:00:00.000Z" in human.splitlines()
    assert show(store, decision(store, "E"))["claim"] is None


def test_tree_marks_the_claimed_item(store):
    p = new(store, "program", "P")
    d = decision(store, "D", "--parent", p)
    claim(store, d, "--session", "s-1")
    assert store("tree", p).out.splitlines()[1].endswith("[claimed]  @michael")


# --- claim --next -------------------------------------------------------------


def test_claim_next_claims_the_first_ready_item(store):
    p = new(store, "program", "P")
    a = decision(store, "A", "--parent", p)
    u = decision(store, "U", "--parent", p, "--urgent")
    result = claim(store, "--next", "--session", "s-1")
    assert result.code == 0, result.err
    assert result.json()["claimed"]["id"] == u
    assert claim(store, "--next", "--session", "s-2").json()["claimed"]["id"] == a
    plain = store("claim", "--next", "--session", "s-3")
    assert (plain.code, plain.out) == (0, "")


def test_claim_next_with_nothing_ready_prints_null(store):
    result = claim(store, "--next", "--session", "s-1")
    assert (result.code, result.json()) == (0, {"schema": 1, "claimed": None})


def test_claim_next_honours_root_and_owner(store):
    store("actor", "add", "bob", "--kind", "human")
    p = new(store, "program", "P")
    q = new(store, "program", "Q")
    decision(store, "A", "--parent", p)
    b = decision(store, "B", "--parent", q, "--owner", "bob")
    c = decision(store, "C", "--parent", q)
    assert claim(store, "--next", "--root", q, "--owner", "bob", "--session", "s-1").json()["claimed"]["id"] == b
    assert claim(store, "--next", "--root", q, "--session", "s-1").json()["claimed"]["id"] == c


def test_claim_next_and_an_id_are_exclusive(store):
    d = decision(store, "D")
    assert claim(store, d, "--next", "--session", "s-1").code == 2
    assert claim(store, "--session", "s-1").code == 2


# --- claim renew ----------------------------------------------------------------


def test_renew_with_no_claims_prints_no_text_and_writes_nothing(store, trk_home):
    before = rows(trk_home)
    result = store("claim", "renew", "--session", "s-unknown")
    assert (result.code, result.out, result.err) == (0, "", "")
    assert store("claim", "renew", "--session", "s-unknown", "--json").json() == {
        "schema": 1, "renewed": [], "not_due": []}
    assert rows(trk_home) == before


def test_renew_writes_only_claims_past_half_their_lease(store, trk_home, monkeypatch):
    d = decision(store, "D")
    e = decision(store, "E")
    claim(store, d, "--session", "s-1")
    claim(store, e, "--session", "s-1", "--lease", "2h")
    at(monkeypatch, "2026-10-10T12:29:00.000Z")
    store("claim", "renew", "--session", "s-1")
    assert rows(trk_home, "claim.renewed") == []
    at(monkeypatch, "2026-10-10T12:31:00.000Z")
    result = store("claim", "renew", "--session", "s-1")
    assert result.code == 0
    [renewed] = rows(trk_home, "claim.renewed")
    assert renewed["item"] == d and renewed["session"] == "s-1"
    assert renewed["data"] == {"claim_actor": "michael", "lease_until": "2026-10-10T13:31:00.000Z", "scope": "build"}
    leases = {c["item"]: c["lease_until"] for c in live_claims(store)}
    assert leases == {d: "2026-10-10T13:31:00.000Z", e: "2026-10-10T14:00:00.000Z"}


def test_ac1_renew_says_which_claims_it_renewed_and_which_were_not_due(store, monkeypatch):
    d = decision(store, "D")
    e = decision(store, "E")
    claim(store, d, "--session", "s-1")
    claim(store, e, "--session", "s-1", "--lease", "2h")
    at(monkeypatch, "2026-10-10T12:31:00.000Z")
    result = store("claim", "renew", "--session", "s-1", "--json")
    assert result.json() == {
        "schema": 1,
        "renewed": [{"item": d, "lease_until": "2026-10-10T13:31:00.000Z", "lease_minutes": 60}],
        "not_due": [{"item": e, "lease_until": "2026-10-10T14:00:00.000Z", "lease_minutes": 120}]}
    at(monkeypatch, "2026-10-10T12:32:00.000Z")
    text = store("claim", "renew", "--session", "s-1").out
    assert text == (f"not due {d} until 2026-10-10T13:31:00.000Z (60m lease)\n"
                    f"not due {e} until 2026-10-10T14:00:00.000Z (120m lease)\n")
    at(monkeypatch, "2026-10-10T13:35:00.000Z")
    assert store("claim", "renew", "--session", "s-1").out == (
        f"renewed {e} until 2026-10-10T15:35:00.000Z (120m lease)\n")


def test_ac2_renew_with_a_lease_extends_every_live_claim_even_when_not_due(store, monkeypatch):
    d = decision(store, "D")
    e = decision(store, "E")
    claim(store, d, "--session", "s-1")
    claim(store, e, "--session", "s-1", "--lease", "2h")
    claim(store, decision(store, "F"), "--session", "s-2")
    at(monkeypatch, "2026-10-10T12:10:00.000Z")
    result = store("claim", "renew", "--session", "s-1", "--lease", "3h", "--json").json()
    assert result["not_due"] == []
    assert {c["item"]: (c["lease_until"], c["lease_minutes"]) for c in result["renewed"]} == {
        d: ("2026-10-10T15:10:00.000Z", 180), e: ("2026-10-10T15:10:00.000Z", 180)}
    stored = {c["item"]: c["lease_until"] for c in live_claims(store)}
    assert stored[d] == stored[e] == "2026-10-10T15:10:00.000Z"
    assert len([c for c in live_claims(store) if c["lease_until"] == "2026-10-10T13:00:00.000Z"]) == 1  # s-2's


def test_renew_lease_becomes_the_stored_length(store, trk_home, monkeypatch):
    d = decision(store, "D")
    claim(store, d, "--session", "s-1")
    at(monkeypatch, "2026-10-10T12:40:00.000Z")
    store("claim", "renew", "--session", "s-1", "--lease", "10m")
    assert live_claims(store)[0]["lease_until"] == "2026-10-10T12:50:00.000Z"
    at(monkeypatch, "2026-10-10T12:44:00.000Z")
    store("claim", "renew", "--session", "s-1")
    assert live_claims(store)[0]["lease_until"] == "2026-10-10T12:50:00.000Z"  # 6 of 10 minutes left
    at(monkeypatch, "2026-10-10T12:46:00.000Z")
    store("claim", "renew", "--session", "s-1")
    assert live_claims(store)[0]["lease_until"] == "2026-10-10T12:56:00.000Z"


def test_renew_never_revives_a_lapsed_lease(store, trk_home, monkeypatch):
    d = decision(store, "D")
    claim(store, d, "--session", "s-1")
    at(monkeypatch, "2026-10-10T13:00:00.000Z")
    store("claim", "renew", "--session", "s-1")
    assert live_claims(store) == []
    assert rows(trk_home, "claim.renewed") == []
    assert len(rows(trk_home, "claim.expired")) == 1


def test_renew_leaves_other_sessions_alone(store, trk_home, monkeypatch):
    d = decision(store, "D")
    claim(store, d, "--session", "s-1")
    at(monkeypatch, "2026-10-10T12:50:00.000Z")
    store("claim", "renew", "--session", "s-2")
    assert rows(trk_home, "claim.renewed") == []


def test_herdr_renews_an_idle_sessions_claims_on_its_behalf(store, trk_home, monkeypatch):
    """ADR 0002 point 6 (amended 2026-10-12): herdr renews, as itself, the claims of a session it shows idle,
    done or blocked. The row names herdr as actor and the claimant's session; nothing else changes."""
    d = decision(store, "D")
    e = decision(store, "E")
    f = decision(store, "F")
    for item, session in ((d, "s-1"), (e, "s-1"), (f, "s-2")):
        assert claim(store, item, "--session", session, "--as", "worker").code == 0
    at(monkeypatch, "2026-10-10T12:31:00.000Z")
    result = store("claim", "renew", "--session", "s-1", "--as", "herdr")
    assert (result.code, result.err) == (0, "")
    renewed = rows(trk_home, "claim.renewed")
    assert {r["item"] for r in renewed} == {d, e}
    for r in renewed:
        assert (r["actor"], r["session"]) == ("herdr", "s-1")
        assert r["data"] == {"claim_actor": "worker", "lease_until": "2026-10-10T13:31:00.000Z", "scope": "build"}
    leases = {c["item"]: (c["session"], c["actor"], c["lease_until"]) for c in live_claims(store)}
    assert leases == {
        d: ("s-1", "worker", "2026-10-10T13:31:00.000Z"),
        e: ("s-1", "worker", "2026-10-10T13:31:00.000Z"),
        f: ("s-2", "worker", "2026-10-10T13:00:00.000Z"),
    }
    feed = store("changes", "--since", "0", "--json").json()
    assert not list(feed_validator().iter_errors(feed))
    assert [(c["item"], c["actor"], c["session"]) for c in feed["changes"] if c["change"] == "claim.renewed"] == [
        (r["item"], "herdr", "s-1") for r in renewed
    ]


# --- claim release -------------------------------------------------------------


def test_release_all_of_a_sessions_claims_as_another_actor(store, trk_home):
    d = decision(store, "D")
    e = decision(store, "E")
    f = decision(store, "F")
    claim(store, d, "--session", "s-1")
    claim(store, e, "--session", "s-1")
    claim(store, f, "--session", "s-2")
    result = store("claim", "release", "--session", "s-1", "--as", "herdr")
    assert result.code == 0, result.err
    released = rows(trk_home, "claim.released")
    assert [r["item"] for r in released] == [d, e]
    assert all(r["actor"] == "herdr" and r["session"] == "s-1" for r in released)
    assert released[0]["data"] == {"claim_actor": "michael", "lease_until": "2026-10-10T13:00:00.000Z", "scope": "build"}
    assert [c["item"] for c in live_claims(store)] == [f]
    assert show(store, d)["ready"] is True


def test_release_one(store, trk_home):
    d = decision(store, "D")
    e = decision(store, "E")
    claim(store, d, "--session", "s-1")
    claim(store, e, "--session", "s-1")
    result = store("claim", "release", "--session", "s-1", e.upper(), "--json")
    assert result.code == 0, result.err
    assert result.json() == {"schema": 1, "released": [e]}
    assert [c["item"] for c in live_claims(store)] == [d]


def test_release_of_nothing_is_a_no_op(store, trk_home):
    d = decision(store, "D")
    claim(store, d, "--session", "s-1")
    assert store("claim", "release", "--session", "s-2").code == 0
    assert store("claim", "release", "--session", "s-2", d).code == 0
    assert rows(trk_home, "claim.released") == []
    assert store("claim", "release", "--session", "s-1", "tr-zzzzz").code == 3


# --- sweep ----------------------------------------------------------------------


def test_ac5_a_lapsed_lease_is_swept_by_any_command(store, trk_home, monkeypatch):
    d = decision(store, "D")
    assert claim(store, d, "--session", "s-1").code == 0
    assert claim(store, d, "--session", "s-2").code == 4
    at(monkeypatch, "2026-10-10T13:00:00.000Z")
    store("actors", "--json")  # any command, a read included
    feed = store("changes", "--since", "0", "--json").json()["changes"]
    [expired] = [r for r in feed if r["change"] == "claim.expired"]
    assert expired["at"] == "2026-10-10T13:00:00.000Z"
    assert (expired["item"], expired["session"], expired["actor"]) == (d, "s-1", "michael")
    assert expired["data"] == {"claim_actor": "michael", "lease_until": "2026-10-10T13:00:00.000Z", "scope": "build"}
    assert claim(store, d, "--session", "s-2").code == 0


def test_an_expired_row_carries_the_claimant_not_the_runner(store, trk_home, monkeypatch):
    store("actor", "add", "builder", "--kind", "agent")
    d = decision(store, "D")
    claim(store, d, "--session", "s-1", "--as", "builder")
    at(monkeypatch, "2026-10-10T14:00:00.000Z")
    store("show", d)
    [expired] = rows(trk_home, "claim.expired")
    assert expired["actor"] == "builder" and expired["at"] == "2026-10-10T13:00:00.000Z"


def test_a_sweep_that_finds_nothing_takes_no_write_lock(store, trk_home):
    d = decision(store, "D")
    conn = sqlite3.connect(trk_home / "store.db", isolation_level=None, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")  # hold the write lock
        result = store("show", d, "--json")
        assert result.code == 0, result.err
    finally:
        conn.execute("ROLLBACK")
        conn.close()


def test_explicit_sweep_records_the_runner(store, trk_home, monkeypatch):
    d = decision(store, "D")
    e = decision(store, "E")
    claim(store, d, "--session", "s-1")
    claim(store, e, "--session", "s-1", "--lease", "2h")
    at(monkeypatch, "2026-10-10T13:30:00.000Z")
    result = store("sweep", "--as", "herdr", "--json")
    assert (result.code, result.json()) == (0, {"schema": 1, "expired": [d]})
    [expired] = rows(trk_home, "claim.expired")
    assert expired["actor"] == "herdr" and expired["at"] == "2026-10-10T13:00:00.000Z"
    assert store("sweep").out == ""


def test_an_ended_lease_is_marked_expired(store, trk_home, monkeypatch):
    d = decision(store, "D")
    claim(store, d, "--session", "s-1")
    at(monkeypatch, "2026-10-10T15:00:00.000Z")
    store("sweep")
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        assert conn.execute('SELECT ended_at, "end" FROM claims').fetchone() == ("2026-10-10T13:00:00.000Z", "expired")
    finally:
        conn.close()


# --- claims ----------------------------------------------------------------------


def test_claims_lists_live_claims_filtered(store, monkeypatch):
    d = decision(store, "D")
    e = decision(store, "E")
    f = decision(store, "F")
    claim(store, d, "--session", "s-1")
    claim(store, e, "--session", "s-2")
    claim(store, f, "--session", "s-1")
    store("claim", "release", "--session", "s-1", f)
    result = store("claims", "--json")
    assert result.json() == {"schema": 1, "claims": [
        {"session": "s-1", "actor": "michael", "item": d, "lease_until": "2026-10-10T13:00:00.000Z", "taken_at": T0,
         "scope": "build"},
        {"session": "s-2", "actor": "michael", "item": e, "lease_until": "2026-10-10T13:00:00.000Z", "taken_at": T0,
         "scope": "build"},
    ]}
    assert [c["item"] for c in live_claims(store, "--session", "s-2")] == [e]
    assert [c["item"] for c in live_claims(store, "--item", d.upper())] == [d]
    assert live_claims(store, "--item", f) == []
    assert store("claims", "--item", "tr-zzzzz", "--json").code == 3
    assert store("claims").out.splitlines() == [
        f"{d}  s-1  michael  2026-10-10T13:00:00.000Z",
        f"{e}  s-2  michael  2026-10-10T13:00:00.000Z",
    ]


# --- close ends the claim --------------------------------------------------------


def test_closing_an_item_releases_its_claim(store, trk_home):
    store("actor", "add", "builder", "--kind", "agent")
    d = decision(store, "D")
    claim(store, d, "--session", "s-1", "--as", "builder")
    resolve(store, d)
    assert store("close", d, "--resolution", "done", "--outcome", "Done.").code == 0
    [released] = rows(trk_home, "claim.released")
    assert (released["item"], released["session"], released["actor"]) == (d, "s-1", "michael")
    assert released["data"] == {"claim_actor": "builder", "lease_until": "2026-10-10T13:00:00.000Z", "scope": "build", "reason": "closed"}
    assert live_claims(store) == []


def test_ac6_a_supersede_releases_the_claim_on_the_replaced_item(store, trk_home, monkeypatch):
    d = decision(store, "D")
    claim(store, d, "--session", "s-1")
    at(monkeypatch, "2026-10-10T13:00:00.000Z")
    assert claim(store, d, "--session", "s-2").code == 0
    d2 = decision(store, "D2")
    x = decision(store, "X")
    store("link", d, "blocks", x)
    assert store("close", d, "--resolution", "superseded", "--by", d2, "--outcome", "Replaced by D2.").code == 0
    assert show(store, x)["why_not"] == [f"blocked-by:{d2}"]
    [released] = rows(trk_home, "claim.released")
    assert (released["item"], released["session"]) == (d, "s-2")
    assert released["data"]["reason"] == "closed"
    closed_seq = rows(trk_home, "item.closed")[0]["seq"]
    assert [r["change"] for r in rows(trk_home) if r["seq"] >= closed_seq][:2] == ["item.closed", "claim.released"]


def test_renew_without_an_actor_is_written_by_the_claims_own_actor(store, trk_home, monkeypatch):
    """tr-82j8x ledger-5: the session hook runs `claim renew` with no --as; its rows are not the default actor's."""
    d = decision(store, "D")
    claim(store, d, "--session", "s-1", "--as", "worker")
    at(monkeypatch, "2026-10-10T12:31:00.000Z")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert store("claim", "renew", "--session", "s-1").code == 0
    [renewed] = rows(trk_home, "claim.renewed")
    assert (renewed["actor"], renewed["data"]["claim_actor"]) == ("worker", "worker")


def test_renew_with_as_is_written_by_that_actor(store, trk_home, monkeypatch):
    d = decision(store, "D")
    claim(store, d, "--session", "s-1", "--as", "worker")
    at(monkeypatch, "2026-10-10T12:31:00.000Z")
    assert store("claim", "renew", "--session", "s-1", "--as", "michael").code == 0
    [renewed] = rows(trk_home, "claim.renewed")
    assert (renewed["actor"], renewed["data"]["claim_actor"]) == ("michael", "worker")
