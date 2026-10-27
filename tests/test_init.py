from gated import DEST, ticket as ticket_flags

SESSION = "11111111-2222-3333-4444-555555555555"


def test_init_registers_default_human_and_herdr_agent(trk, trk_home):
    assert trk("init", "--me", "michael").code == 0
    assert (trk_home / "store.db").exists()

    program = trk("new", "program", "P", "--json", *DEST).json()["item"]
    assert program["status"] == "open"


def test_init_registers_the_worker_agent_so_a_fresh_store_takes_its_claim(trk, trk_home):
    """Every skill writes `--as worker`; before this, the first worker claim on a fresh store was refused
    `unknown-actor` and no next-step print reliably prevented it. Michael approved putting it in `trk init`
    at 7002060, 2026-10-27."""
    assert trk("init", "--me", "michael").code == 0

    listed = trk("actors", "--json").json()["actors"]
    assert {"worker", "herdr", "publisher", "michael"} <= {row["id"] for row in listed}
    assert next(row["kind"] for row in listed if row["id"] == "worker") == "agent"

    program = trk("new", "program", "P", "--json", *DEST).json()["item"]["id"]
    project = trk("new", "project", "J", "--parent", program, "--json", *DEST).json()["item"]["id"]
    ticket = trk("new", "work-item", "T", "--form", "self-contained", "--parent", project, "--json",
                 *ticket_flags("self-contained")).json()["item"]["id"]

    claimed = trk("claim", ticket, "--as", "worker", "--session", SESSION, "--json")

    assert claimed.code == 0, claimed.err
    assert [row["actor"] for row in trk("claims", "--json").json()["claims"]] == ["worker"]


def test_second_init_is_refused(trk):
    trk("init", "--me", "michael")
    result = trk("init", "--me", "michael", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "store-exists"
