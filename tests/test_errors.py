import json

from gated import DEST


def test_usage_error_exits_2_with_json_on_stderr(trk):
    trk("init", "--me", "michael")
    result = trk("new", "program", "--json", *DEST)
    assert result.code == 2
    assert result.out == ""
    assert result.error()["code"] == "usage"


def test_unknown_verb_is_a_usage_error(trk):
    assert trk("frobnicate").code == 2


def test_empty_title_is_a_usage_error(trk):
    trk("init", "--me", "michael")
    assert trk("new", "program", "  ", *DEST).code == 2


def test_human_errors_are_one_line_on_stderr(trk):
    trk("init", "--me", "michael")
    result = trk("init", "--me", "michael")
    assert result.code == 1
    assert result.out == ""
    assert len(result.err.strip().splitlines()) == 1
    assert "store-exists" in result.err


def test_json_error_shape(trk):
    trk("init", "--me", "michael")
    error = json.loads(trk("init", "--me", "michael", "--json").err)
    assert set(error) == {"error"}
    assert set(error["error"]) == {"code", "rule", "item", "message"}


def test_commands_before_init_say_no_store(trk):
    result = trk("show", "tr-aaaaa", "--json")
    assert result.code == 3
    assert result.error()["rule"] == "no-store"
