"""tr-az21r: only the installed trk migrates or creates the default store.

Every test here points $HOME at a temp dir and drops $TRK_HOME and $TRK_TEST, so "the default
store" is <tmp>/.local/share/trk. $UV_TOOL_DIR points at a fake `uv tool` dir whose trk env
names which source tree counts as installed.
"""

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import trk as package
from gated import DEST
from trk import store

FIXTURE = Path(__file__).parent / "fixtures" / "schema1"
OPENED_AT = "2026-10-10T14:30:00.000Z"
SOURCE = Path(package.__file__).resolve().parent.parent  # the src/ dir this test's trk imports from


def install(tool_dir, source):
    """A fake editable `uv tool install` of trk whose .pth names `source`."""
    site = tool_dir / "trk" / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    (site / "_editable_impl_trk.pth").write_text(f"# editable\n{source}\n")
    return site


def digest(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


def schema_version(directory):
    conn = sqlite3.connect(directory / "store.db")
    try:
        return conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uv-tools"))
    monkeypatch.delenv("TRK_HOME")
    monkeypatch.delenv("TRK_TEST")
    monkeypatch.setenv("TRK_NOW", OPENED_AT)
    assert store.default_dir() == home / ".local" / "share" / "trk"
    return home


@pytest.fixture
def default_v1(home):
    """A schema 1 store at the (temp) default location."""
    directory = store.default_dir()
    directory.mkdir(parents=True)
    for name in ("store.db", "feed.head"):
        shutil.copyfile(FIXTURE / name, directory / name)
    return directory


def test_a_build_that_is_not_installed_refuses_to_migrate_the_default_store(trk, default_v1, tmp_path):
    install(tmp_path / "uv-tools", tmp_path / "elsewhere" / "src")
    before = digest(default_v1)

    result = trk("changes", "--since", "0", "--json")

    assert result.code == 1
    assert result.error()["rule"] == "foreign-build"
    assert str(SOURCE) in result.error()["message"]
    assert digest(default_v1) == before


def test_with_no_installed_trk_every_build_is_foreign(trk, default_v1):
    before = digest(default_v1)

    result = trk("changes", "--since", "0", "--json")

    assert result.code == 1
    assert result.error()["rule"] == "foreign-build"
    assert digest(default_v1) == before


@pytest.mark.parametrize("relative", [False, True])
def test_the_installed_build_migrates_the_default_store(trk, default_v1, tmp_path, relative):
    site = install(tmp_path / "uv-tools", SOURCE)
    if relative:  # site reads a relative .pth line against site-packages, not the working dir
        (site / "_editable_impl_trk.pth").write_text(f"{os.path.relpath(SOURCE, site)}\n")

    result = trk("changes", "--since", "0", "--json")

    assert result.code == 0, result.err
    assert schema_version(default_v1) == str(store.SCHEMA_VERSION)


def test_a_foreign_build_opens_a_default_store_at_its_own_schema(trk, home, tmp_path, monkeypatch):
    install(tmp_path / "uv-tools", SOURCE)
    assert trk("init", "--me", "michael").code == 0
    install(tmp_path / "other-tools", tmp_path / "elsewhere" / "src")
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "other-tools"))

    assert trk("new", "program", "Tracker v1", *DEST).code == 0


def test_a_foreign_build_refuses_to_create_the_default_store(trk, home):
    result = trk("init", "--me", "michael", "--json")

    assert result.code == 1
    assert result.error()["rule"] == "foreign-build"
    assert not (store.default_dir() / "store.db").exists()


def test_a_foreign_build_still_migrates_a_store_under_trk_home(trk, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for name in ("store.db", "feed.head"):
        shutil.copyfile(FIXTURE / name, scratch / name)
    monkeypatch.setenv("TRK_HOME", str(scratch))
    monkeypatch.setenv("TRK_TEST", "1")
    monkeypatch.setenv("TRK_NOW", OPENED_AT)

    assert trk("changes", "--since", "0", "--json").code == 0
    assert schema_version(scratch) == str(store.SCHEMA_VERSION)


def run_from(source, *argv):
    """Run the CLI in a fresh interpreter that imports trk from `source`."""
    env = {**os.environ, "PYTHONPATH": str(source)}
    code = "import sys; from trk.cli import main; sys.exit(main(sys.argv[1:]))"
    return subprocess.run([sys.executable, "-c", code, *argv], capture_output=True, text=True, env=env, check=False)


def test_a_second_checkout_refuses_the_default_store_and_leaves_it_unchanged(default_v1, tmp_path):
    install(tmp_path / "uv-tools", SOURCE)
    second = tmp_path / "second-checkout" / "src"
    shutil.copytree(SOURCE / "trk", second / "trk", ignore=shutil.ignore_patterns("__pycache__"))
    before = digest(default_v1)

    done = run_from(second, "changes", "--since", "0", "--json")

    assert done.returncode == 1, done.stderr
    assert '"foreign-build"' in done.stderr
    assert str(second) in done.stderr
    assert digest(default_v1) == before

    installed = run_from(SOURCE, "changes", "--since", "0", "--json")
    assert installed.returncode == 0, installed.stderr
    assert schema_version(default_v1) == str(store.SCHEMA_VERSION)


def test_a_foreign_build_refuses_to_restore_over_the_default_store(trk, home, tmp_path, monkeypatch):
    install(tmp_path / "uv-tools", SOURCE)
    assert trk("init", "--me", "michael").code == 0
    backup = tmp_path / "b.db"
    assert trk("backup", str(backup)).code == 0
    install(tmp_path / "other-tools", tmp_path / "elsewhere" / "src")
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "other-tools"))
    before = digest(store.default_dir())

    result = trk("restore", str(backup), "--json")

    assert result.code == 1
    assert result.error()["rule"] == "foreign-build"
    assert digest(store.default_dir()) == before
