import re
from itertools import cycle

from trk import ids
from gated import DEST


def scripted(chars):
    stream = iter(chars)
    return lambda alphabet: next(stream)


def test_id_is_prefix_and_five_crockford_chars():
    assert re.fullmatch(r"tr-[0-9abcdefghjkmnpqrstvwxyz]{5}", ids.new_id("tr", lambda _: False))


def test_collision_redraws():
    taken = {"tr-aaaaa"}
    assert ids.new_id("tr", taken.__contains__, draw=scripted("aaaaa" "bbbbb")) == "tr-bbbbb"


def test_three_collisions_switch_to_six_chars():
    taken = {"tr-aaaaa"}
    draw = scripted("aaaaa" * 3 + "cccccc")
    assert ids.new_id("tr", taken.__contains__, draw=draw) == "tr-cccccc"


def test_six_char_draws_also_redraw_on_collision():
    taken = {"tr-aaaaa", "tr-aaaaaa"}
    draw = scripted("aaaaa" * 3 + "aaaaaa" + "dddddd")
    assert ids.new_id("tr", taken.__contains__, draw=draw) == "tr-dddddd"


def test_input_ids_read_crockford():
    assert ids.normalize("TR-K7M2Q") == "tr-k7m2q"
    assert ids.normalize("tr-ILOUa") == "tr-110va"


def test_show_matches_ids_case_insensitively_with_crockford_reads(trk):
    trk("init", "--me", "michael", "--prefix", "tool")
    item_id = trk("new", "program", "P", *DEST).out.strip()
    assert item_id.startswith("tool-")
    sloppy = item_id.upper().replace("1", "l").replace("0", "O")
    assert trk("show", sloppy, "--json").json()["item"]["id"] == item_id


def test_no_prefix_abbreviation(trk):
    trk("init", "--me", "michael")
    item_id = trk("new", "program", "P", *DEST).out.strip()
    assert trk("show", item_id[:-1]).code == 3
    assert trk("show", item_id.split("-")[1]).code == 3
