"""End-to-end: the `trk` on PATH (uv tool install --editable .) runs against a temp store."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import trk as package
from trk import store
from gated import DEST
from store_note import without_note

TRK = shutil.which("trk")


def run(*argv):
    return without_note(subprocess.run([TRK, *argv], capture_output=True, text=True, env=os.environ.copy(), check=False))


def test_installed_binary_tracks_the_working_tree():
    if store.running_source() not in store.installed_sources():  # the check behind `trk --version`'s installed flag
        pytest.skip(f"{store.running_source()} is not the installed trk's tree (a worktree or second checkout)")
    assert TRK, "trk is not on PATH; run `uv tool install --editable .`"
    probe = subprocess.run(
        [Path(TRK).resolve().parent / "python", "-c", "import trk; print(trk.__file__)"],
        capture_output=True, text=True, check=True,
    )
    assert Path(probe.stdout.strip()).resolve() == Path(package.__file__).resolve()


def test_installed_binary_walks_init_new_show(trk_home):
    assert TRK, "trk is not on PATH; run `uv tool install --editable .`"
    assert run("init", "--me", "michael").returncode == 0
    new = run("new", "program", "Tracker v1", *DEST)
    assert new.returncode == 0
    item_id = new.stdout.strip()

    shown = run("show", item_id, "--json")
    assert shown.returncode == 0
    assert json.loads(shown.stdout)["item"]["title"] == "Tracker v1"

    missing = run("show", "tr-zzzzz", "--json")
    assert missing.returncode == 3
    assert json.loads(missing.stderr)["error"]["code"] == "not-found"
    assert (trk_home / "store.db").exists()
