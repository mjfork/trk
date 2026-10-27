"""tr-b4jmn: `trk --version` names the package version, the git commit of the source tree it runs from, whether that
tree is the installed one, and the store schema it speaks, without opening a store.

$UV_TOOL_DIR points at a fake `uv tool` dir whose trk env names which source tree counts as installed, as in
test_foreign_build."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from importlib.metadata import version as dist_version
from pathlib import Path

import pytest

import trk as package
from feed_contract import validator
from test_foreign_build import install
from trk import store

SOURCE = Path(package.__file__).resolve().parent.parent  # the src/ dir this test's trk imports from
REPO = SOURCE.parent


def head(tree):
    return subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def dirty(tree):
    """Whether `tree`'s checkout has uncommitted changes right now: the truth the report is checked against,
    since the suite runs on a clean tree and on a tree mid-change."""
    done = subprocess.run(["git", "-C", str(tree), "status", "--porcelain"], capture_output=True, text=True,
                          check=True)
    return bool(done.stdout.strip())


RECORDED = "b" * 40  # a sha no checkout here has, so a report carrying it can only come from the record


def copied_package(root):
    """A copy of the trk package at `root`, as an installed copy sits in site-packages: a plain directory
    with no checkout around it."""
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE / "trk", root / "trk", ignore=shutil.ignore_patterns("__pycache__", "_build.py"))
    return root


def build_record(package, commit=RECORDED, flag=True):
    """A stamp like the one a build puts in a wheel, planted in a copied package."""
    (package / "trk" / "_build.py").write_text(f'COMMIT = "{commit}"\nDIRTY = {flag}\n')


def version_json(source, **env):
    """`trk --version --json` from a trk at `source`, run from a directory in no checkout."""
    done = subprocess.run(
        [sys.executable, "-c", "import sys; from trk.cli import main; sys.exit(main())", "--version", "--json"],
        capture_output=True, text=True, check=False, cwd=source.parent,
        env={**os.environ, "PYTHONPATH": str(source), **env})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uv-tools"))
    return tmp_path / "uv-tools"


def test_the_installed_tree_reports_installed(trk, tools, trk_home):
    install(tools, SOURCE)

    result = trk("--version", "--json")

    assert result.code == 0, result.err
    assert result.json() == {"schema": 1, "version": dist_version("trk"), "commit": head(SOURCE),
                             "dirty": dirty(SOURCE), "installed": True, "store_schema": store.SCHEMA_VERSION}
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
        f"commit: {head(SOURCE)}" + (" (dirty)" if dirty(SOURCE) else ""),
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
    assert result.json()["dirty"] is None, "git could not say, so neither does the report"
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


def test_a_copy_outside_a_checkout_reports_the_commit_it_was_built_from(tmp_path, tools):
    """What a `--no-editable` install is: a package directory with no git around it. It answers from the
    record install.sh wrote at build time, which is the one thing an installer can compare with its pin."""
    package = copied_package(tmp_path / "site-packages")
    build_record(package)

    reply = version_json(package, UV_TOOL_DIR=str(tools))

    assert reply["commit"] == RECORDED != head(SOURCE)
    assert reply["dirty"] is True
    assert reply["installed"] is False


def test_a_copy_built_from_a_clean_tree_is_not_dirty(tmp_path, tools):
    package = copied_package(tmp_path / "site-packages")
    build_record(package, flag=False)

    reply = version_json(package, UV_TOOL_DIR=str(tools))

    assert (reply["commit"], reply["dirty"]) == (RECORDED, False)


def test_a_copy_with_no_record_says_it_does_not_know(tmp_path, tools):
    """Built outside a checkout, or by something that is not install.sh: null, never a guess."""
    package = copied_package(tmp_path / "site-packages")

    reply = version_json(package, UV_TOOL_DIR=str(tools))

    assert (reply["commit"], reply["dirty"]) == (None, None)


@pytest.mark.parametrize("body", ['COMMIT = "short"\nDIRTY = False\n', "COMMIT = None\n", "", "DIRTY = True\n"])
def test_a_record_that_names_no_sha_is_ignored(tmp_path, tools, body):
    package = copied_package(tmp_path / "site-packages")
    (package / "trk" / "_build.py").write_text(body)

    assert version_json(package, UV_TOOL_DIR=str(tools))["commit"] is None


def surrounded(tmp_path, with_stamp=True):
    """A copy of the package living *inside* someone else's checkout, as an installed copy does when the
    uv tool directory sits under a `$HOME` that is itself a git repo. Returns (package dir, that repo)."""
    outer = tmp_path / "dotfiles"
    package = copied_package(outer / "lib" / "site-packages")
    if with_stamp:
        build_record(package, flag=False)
    git = ["git", "-C", str(outer), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (outer / "README.md").write_text("someone else's project\n")
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-qm", "an unrelated project"], check=True)
    return package, outer


def test_a_copy_inside_an_unrelated_repo_reports_its_own_stamp(tmp_path, tools):
    """The repo around an installed copy is some other project: its HEAD says nothing about this trk, and
    must not shadow the stamp the wheel carries (found by an audit of 7002060)."""
    package, outer = surrounded(tmp_path)

    reply = version_json(package, UV_TOOL_DIR=str(tools))

    assert reply["commit"] == RECORDED != head(outer), "the surrounding repo's HEAD was reported"
    assert reply["dirty"] is False, "nor its dirty state"


def test_a_copy_inside_an_unrelated_repo_and_no_stamp_knows_nothing(tmp_path, tools):
    package, outer = surrounded(tmp_path, with_stamp=False)

    reply = version_json(package, UV_TOOL_DIR=str(tools))

    assert (reply["commit"], reply["dirty"]) == (None, None), f"it spoke for {head(outer)}"


def test_a_checkout_answers_live_even_with_a_record_in_it(tmp_path, tools):
    """A record left in a tree by an earlier `--no-editable` install never speaks for that tree: the
    working tree's own HEAD does, which is what an editable install must report."""
    tree = tmp_path / "checkout"
    copied_package(tree / "src")
    git = ["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "a tree of its own"], check=True)
    build_record(tree / "src")  # as a `--no-editable` install would leave behind, were it not removed

    reply = version_json(tree / "src", UV_TOOL_DIR=str(tools))

    assert reply["commit"] == head(tree) != RECORDED
    assert reply["dirty"] is True, "the record it was handed is an uncommitted change in that tree"


@pytest.fixture
def buildable(tmp_path):
    """A committed copy of this project at a place the test owns, so a wheel built from it names a sha of
    its own and the result does not depend on whether this checkout happens to be clean."""
    def make(root):
        root.mkdir(parents=True)
        # .gitignore comes along because the build imports hatch_build.py and so leaves a __pycache__ in
        # the tree it builds: without it, every build would look dirty to the hook's `git status`.
        for name in ("pyproject.toml", "LICENSE", "README.md", "hatch_build.py", ".gitignore"):
            shutil.copy(REPO / name, root / name)
        copied_package(root / "src")
        git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "a copy to build"], check=True)
        return root
    return make


def build_wheel(tree, out):
    done = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=tree, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    return next(Path(out).glob("*.whl"))


def stamp_of(wheel):
    """The stamp the build hook put in the wheel, or None when it put none there."""
    with zipfile.ZipFile(wheel) as packed:
        if "trk/_build.py" not in packed.namelist():
            return None
        scope = {}
        exec(packed.read("trk/_build.py").decode(), scope)  # noqa: S102 (a file this test's own build wrote)
    return {"commit": scope.get("COMMIT"), "dirty": scope.get("DIRTY")}


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv builds the wheel")
def test_a_wheel_is_stamped_with_the_commit_it_was_built_from(tmp_path, buildable, tools):
    """What a `--no-editable` install reports: the sha of the checkout the wheel came from, read back with
    no git anywhere near the copy."""
    tree = buildable(tmp_path / "project")

    wheel = build_wheel(tree, tmp_path / "dist")

    assert stamp_of(wheel) == {"commit": head(tree), "dirty": False}
    assert not (tree / "src" / "trk" / "_build.py").exists(), "a build writes nothing into the tree it builds"
    with zipfile.ZipFile(wheel) as packed:
        packed.extractall(tmp_path / "unpacked")
    reply = version_json(tmp_path / "unpacked", UV_TOOL_DIR=str(tools))
    assert (reply["commit"], reply["dirty"], reply["installed"]) == (head(tree), False, False)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv builds the wheel")
def test_a_wheel_built_from_a_changed_tree_is_stamped_dirty(tmp_path, buildable):
    tree = buildable(tmp_path / "project")
    (tree / "src" / "trk" / "cli.py").write_text("# changed after the commit\n")

    assert stamp_of(build_wheel(tree, tmp_path / "dist")) == {"commit": head(tree), "dirty": True}


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv builds the wheel")
def test_a_repo_that_merely_surrounds_the_build_is_not_its_commit(tmp_path, buildable, tools):
    """Built from a subdirectory of someone else's checkout: that checkout's HEAD says nothing about this
    wheel, so it is left unstamped and `trk --version` says the commit is unknown."""
    outer = tmp_path / "outer"
    tree = buildable(outer / "vendor" / "trk")
    shutil.rmtree(tree / ".git")
    git = ["git", "-C", str(outer), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "another project entirely"], check=True)

    wheel = build_wheel(tree, tmp_path / "dist")

    assert stamp_of(wheel) is None
    with zipfile.ZipFile(wheel) as packed:
        packed.extractall(tmp_path / "unpacked")
    assert version_json(tmp_path / "unpacked", UV_TOOL_DIR=str(tools))["commit"] is None


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
