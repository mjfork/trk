"""tr-b4jmn: `trk --version` names the package version, the git commit of the source tree it runs from, whether that
tree is the installed one, and the store schema it speaks, without opening a store.

$UV_TOOL_DIR points at a fake `uv tool` dir whose trk env names which source tree counts as installed, as in
test_foreign_build."""

import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import version as dist_version
from pathlib import Path

import pytest

import trk as package
from feed_contract import validator
from test_foreign_build import install
from trk import store

SOURCE = Path(package.__file__).resolve().parent.parent  # the src/ dir this test's trk imports from


def head(tree):
    return subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uv-tools"))
    return tmp_path / "uv-tools"


def test_the_installed_tree_reports_installed(trk, tools, trk_home):
    install(tools, SOURCE)

    result = trk("--version", "--json")

    assert result.code == 0, result.err
    assert result.json() == {"schema": 1, "version": dist_version("trk"), "commit": head(SOURCE), "installed": True,
                             "store_schema": store.SCHEMA_VERSION}
    assert not trk_home.exists()


def test_a_tree_that_is_not_installed_reports_not_installed(trk, tools, tmp_path):
    install(tools, tmp_path / "elsewhere" / "src")

    reply = trk("--version", "--json").json()

    assert reply["installed"] is False
    assert reply["commit"] == head(SOURCE)


def test_text_names_version_commit_install_and_store_schema(trk, tools, tmp_path):
    install(tools, tmp_path / "elsewhere" / "src")

    result = trk("--version")

    assert result.code == 0, result.err
    assert result.out.splitlines() == [
        f"trk {dist_version('trk')}",
        f"commit: {head(SOURCE)}",
        f"installed: no (runs from {SOURCE})",
        f"store schema: {store.SCHEMA_VERSION}",
    ]


def test_installed_text(trk, tools):
    install(tools, SOURCE)

    assert f"installed: yes ({SOURCE})" in trk("--version").out.splitlines()


def test_without_git_the_commit_is_null(trk, tools, monkeypatch):
    monkeypatch.setenv("PATH", "")

    result = trk("--version", "--json")

    assert result.code == 0, result.err
    assert result.json()["commit"] is None
    assert "commit: unknown" in trk("--version").out.splitlines()


def test_version_takes_only_json(trk):
    result = trk("--version", "show", "tr-zzzzz", "--json")

    assert result.code == 2
    assert result.error()["code"] == "usage"


def test_needs_no_store_even_at_the_refused_default(trk, tools, monkeypatch, tmp_path):
    """No store is resolved, so $TRK_TEST's refusal of the default path never fires."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TRK_HOME")

    assert trk("--version", "--json").code == 0
    assert not store.default_dir().exists()


def test_a_worktree_reports_its_own_commit(tmp_path, tools):
    """A second checkout of trk: its own HEAD, and not installed."""
    tree = tmp_path / "worktree"
    shutil.copytree(SOURCE / "trk", tree / "src" / "trk", ignore=shutil.ignore_patterns("__pycache__"))
    git = ["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "worktree"], check=True)
    install(tools, SOURCE)

    done = subprocess.run(
        [sys.executable, "-c", "import sys; from trk.cli import main; sys.exit(main())", "--version", "--json"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONPATH": str(tree / "src"), "UV_TOOL_DIR": str(tools)},
    )

    assert done.returncode == 0, done.stderr
    reply = json.loads(done.stdout)
    assert reply["commit"] == head(tree) != head(SOURCE)
    assert reply["installed"] is False


def test_version_validates_against_the_contract(trk, tools):
    install(tools, SOURCE)
    reply = trk("--version", "--json").json()

    def errors(value):
        return list(validator("version").iter_errors(value))

    assert errors(reply) == []
    assert errors({**reply, "commit": None, "future": 1}) == []
    assert errors({**reply, "installed": "yes"}) != []
    assert errors({**reply, "commit": "main"}) != []
    assert errors({key: value for key, value in reply.items() if key != "store_schema"}) != []


def test_version_is_not_a_flag_of_a_verb(trk):
    assert trk("--vers", "show", "tr-zzzzz", "--json").code == 2
