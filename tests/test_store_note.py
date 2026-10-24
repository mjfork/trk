"""tr-fe457: a set $TRK_HOME is named on stderr, so a stray export never sends writes to a scratch store silently."""

import json
import sys

import pytest

from trk.cli import main


@pytest.fixture
def process(monkeypatch, capsys):
    """Run main() the way the `trk` script does: argv comes from sys.argv."""

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["trk", *argv])
        code = main()
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def test_set_trk_home_is_named_once_on_stderr(process, trk_home):
    process("init", "--me", "michael")
    code, out, err = process("ready")
    assert code == 0
    assert err == f"trk: store {trk_home} (from $TRK_HOME)\n"


def test_json_stdout_is_unchanged_and_the_line_stays_on_stderr(process, trk_home, monkeypatch):
    process("init", "--me", "michael")
    code, out, err = process("ready", "--json")
    assert code == 0
    assert json.loads(out)["schema"] == 1
    assert err == f"trk: store {trk_home} (from $TRK_HOME)\n"
    monkeypatch.delenv("TRK_HOME")
    monkeypatch.delenv("TRK_TEST")
    monkeypatch.setenv("HOME", str(trk_home.parent / "nohome"))
    assert process("--version", "--json")[2] == ""


def test_a_failure_still_prints_its_error_after_the_line(process, trk_home):
    code, out, err = process("show", "tr-nope1", "--json")
    assert code == 3
    line, _, rest = err.partition("\n")
    assert line == f"trk: store {trk_home} (from $TRK_HOME)"
    assert json.loads(rest)["error"]["code"] == "not-found"


def test_unset_trk_home_prints_nothing_new(process, tmp_path, monkeypatch):
    monkeypatch.delenv("TRK_HOME")
    monkeypatch.delenv("TRK_TEST")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    code, out, err = process("--version")
    assert err == ""


def test_explicit_argv_prints_no_line(trk):
    assert trk("init", "--me", "michael").err == ""
