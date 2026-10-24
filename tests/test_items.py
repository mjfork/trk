from gated import DEST


def test_new_program_prints_its_id_and_show_returns_it(trk):
    trk("init", "--me", "michael")
    new = trk("new", "program", "Tracker v1", *DEST)
    assert new.code == 0
    item_id = new.out.strip()

    item = trk("show", item_id, "--json").json()["item"]
    assert item["id"] == item_id
    assert item["kind"] == "program"
    assert item["form"] is None
    assert item["title"] == "Tracker v1"
    assert item["parent"] is None
    assert item["root"] == item_id
    assert item["depth"] == 0
    assert item["status"] == "open"
    assert item["urgent"] is False
    assert item["owner"] == item["effective_owner"] == "michael"  # the default owner
    assert item["repos"] == []

    human = trk("show", item_id)
    assert human.code == 0 and item_id in human.out and "Tracker v1" in human.out


def test_new_json_prints_the_same_item_as_show(trk):
    trk("init", "--me", "michael")
    created = trk("new", "program", "P", "--json", *DEST).json()
    assert created == trk("show", created["item"]["id"], "--json").json()


def test_show_unknown_id_exits_3(trk):
    trk("init", "--me", "michael")
    result = trk("show", "tr-zzzzz", "--json")
    assert result.code == 3
    assert result.error() == {"code": "not-found", "rule": "not-found", "item": "tr-zzzzz", "message": "no item 'tr-zzzzz'"}


def test_timestamps_are_utc_with_milliseconds(trk, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-10T23:52:03.123456+02:00")
    trk("init", "--me", "michael")
    item = trk("new", "program", "P", "--json", *DEST).json()["item"]
    assert item["created_at"] == item["updated_at"] == "2026-10-10T21:52:03.123Z"


def test_trk_now_is_ignored_outside_test_mode(trk, monkeypatch):
    trk("init", "--me", "michael")
    monkeypatch.setenv("TRK_NOW", "2001-01-01T00:00:00Z")
    monkeypatch.setenv("TRK_TEST", "0")
    item = trk("new", "program", "P", "--json", *DEST).json()["item"]
    assert not item["created_at"].startswith("2001")
