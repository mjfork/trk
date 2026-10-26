"""UP-0016: `trk guidance <skill>` prints a site's own guidance for a trk skill, and each skill tells the agent to
run it. The file is site config: trk only prints it."""

import json
from pathlib import Path

import pytest

from feed_contract import validator

REPO = Path(__file__).resolve().parent.parent
SKILLS = ("trk", "trk-drive-program", "trk-create-program")


@pytest.fixture
def gdir(tmp_path, monkeypatch, trk_home):
    d = tmp_path / "g"
    monkeypatch.setenv("TRK_GUIDANCE_DIR", str(d))
    return d


def test_no_file_prints_nothing_and_exits_0(trk, gdir):
    r = trk("guidance", "trk")
    assert r.code == 0 and r.out == ""
    reply = trk("guidance", "trk", "--json").json()
    assert reply["present"] is False and reply["text"] is None and reply["path"] == str(gdir / "trk.md")
    assert not list(validator("guidance").iter_errors(reply))


def test_a_file_is_printed_byte_for_byte(trk, gdir):
    gdir.mkdir()
    text = "# Site rules\n\n- Workers search only inside their worktree. ✓\n"
    (gdir / "trk-drive-program.md").write_text(text, encoding="utf-8")
    r = trk("guidance", "trk-drive-program")
    assert r.code == 0 and r.out == text
    assert trk("guidance", "trk-drive-program", "--path").out == f"{gdir / 'trk-drive-program.md'}\n"
    reply = trk("guidance", "trk-drive-program", "--json").json()
    assert reply["present"] is True and reply["text"] == text and reply["bytes"] == len(text.encode())
    assert not list(validator("guidance").iter_errors(reply))


def test_path_is_printed_whether_or_not_the_file_exists(trk, gdir):
    assert trk("guidance", "trk-create-program", "--path").out == f"{gdir / 'trk-create-program.md'}\n"


def test_the_default_folder_is_under_xdg_config(trk, trk_home, tmp_path, monkeypatch):
    monkeypatch.delenv("TRK_GUIDANCE_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert trk("guidance", "trk", "--path").out == f"{tmp_path / 'cfg/trk/guidance/trk.md'}\n"


def test_an_unknown_skill_exits_2(trk, gdir):
    assert trk("guidance", "nosuch").code == 2
    r = trk("guidance", "nosuch", "--json")
    assert r.code == 2 and r.error()["rule"] == "unknown-skill"


@pytest.mark.parametrize("content, rule", [(b"x" * (64 * 1024 + 1), "guidance-too-big"),
                                           (b"\xff\xfe not utf-8", "guidance-not-utf8")], ids=["too-big", "not-utf8"])
def test_a_bad_file_exits_2(trk, gdir, content, rule):
    gdir.mkdir()
    (gdir / "trk.md").write_bytes(content)
    assert trk("guidance", "trk").code == 2
    r = trk("guidance", "trk", "--json")
    assert r.code == 2 and r.error()["rule"] == rule


def test_a_file_at_the_limit_is_printed(trk, gdir):
    gdir.mkdir()
    (gdir / "trk.md").write_bytes(b"x" * (64 * 1024))
    assert trk("guidance", "trk").code == 0


@pytest.mark.parametrize("skill", SKILLS)
def test_each_skill_tells_the_agent_to_read_its_guidance(skill):
    text = (REPO / "skills" / skill / "SKILL.md").read_text()
    assert text.count(f"trk guidance {skill}") == 1
    assert "**Site guidance.**" in text
