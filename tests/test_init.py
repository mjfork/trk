from gated import DEST


def test_init_registers_default_human_and_herdr_agent(trk, trk_home):
    assert trk("init", "--me", "michael").code == 0
    assert (trk_home / "store.db").exists()

    program = trk("new", "program", "P", "--json", *DEST).json()["item"]
    assert program["status"] == "open"


def test_second_init_is_refused(trk):
    trk("init", "--me", "michael")
    result = trk("init", "--me", "michael", "--json")
    assert result.code == 1
    assert result.error()["rule"] == "store-exists"
