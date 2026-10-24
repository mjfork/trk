"""Git output trk reads never crashes a command because a file or path is not UTF-8 (tr-ay59z): the rules file,
an ADR and a changed path are decoded leniently, so blast-radius rates the change instead of raising."""

import subprocess

import pytest

from gated import git
from trk import blast, gitrepo

LATIN1_RULES = b"# r\xe9gles caf\xe9\nYELLOW src/**\n"
LATIN1_ADR = b"# D\xe9cision\nScope: lib/**\n"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "R"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "core.quotepath", "false")
    (r / ".trk").mkdir()
    (r / ".trk" / "blast-radius").write_bytes(LATIN1_RULES)
    (r / "docs" / "adr").mkdir(parents=True)
    (r / "docs" / "adr" / "0001-x.md").write_bytes(LATIN1_ADR)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    base = git(r, "rev-parse", "HEAD")
    (r / "src").mkdir()
    (r / "src" / "a.py").write_text("a\n")
    (r / "lib").mkdir()
    (r / "lib" / "b.py").write_text("b\n")
    # a path that is not UTF-8: git stores the raw bytes (Latin-1 "é")
    with open(str(r).encode() + b"/src/caf\xe9.txt", "wb") as handle:
        handle.write(b"x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "change")
    return {"path": str(r), "base": base, "head": git(r, "rev-parse", "HEAD")}


def test_ac1_blast_radius_rates_a_repo_whose_rules_file_and_adr_are_not_utf8(repo):
    gitrepo.forget()
    detail = blast.classify(repo["path"], repo["base"], repo["head"])
    assert detail is not None
    colours = {f["path"]: f["colour"] for f in detail["files"]}
    assert colours["src/a.py"] == "yellow"  # the rules file parsed despite its Latin-1 comment
    assert colours["lib/b.py"] == "yellow"  # the ADR's scope applied
    assert detail["lane"] == "yellow"


def test_the_rules_file_and_the_adr_read_as_text_with_replacement_characters(repo):
    assert "�" in gitrepo.read_file(repo["path"], repo["head"], ".trk/blast-radius")
    assert gitrepo.list_dir(repo["path"], repo["head"], "docs/adr") == ["docs/adr/0001-x.md"]


def test_ac2_changed_files_lists_a_path_that_is_not_utf8_when_quotepath_is_off(repo):
    assert subprocess.run(["git", "-C", repo["path"], "config", "core.quotepath"], capture_output=True,
                          text=True).stdout.strip() == "false"
    names = gitrepo.changed_files(repo["path"], repo["base"], repo["head"])
    assert "src/a.py" in names and any(name.startswith("src/caf") and name.endswith(".txt") for name in names)


# --- the auditor's two findings: git's default quotepath, and a non-UTF-8 ADR name (tr-ay59z ac-3, ac-4) ---

@pytest.fixture
def default_quotepath_repo(tmp_path):
    """A repo with git's default core.quotepath and a `RED src/**` rule; the change adds a valid-UTF-8 non-ASCII
    path (src/café.txt) and a Latin-1 one, and an ADR whose file name is not UTF-8 with `Scope: lib/**`."""
    r = tmp_path / "Q"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / ".trk").mkdir()
    (r / ".trk" / "blast-radius").write_text("RED src/**\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    base = git(r, "rev-parse", "HEAD")
    (r / "src").mkdir()
    (r / "src" / "café.txt").write_text("x\n")
    (r / "lib").mkdir()
    (r / "lib" / "b.py").write_text("b\n")
    (r / "docs" / "adr").mkdir(parents=True)
    root = str(r).encode()
    with open(root + b"/src/caf\xe9-latin.txt", "wb") as handle:
        handle.write(b"y\n")
    with open(root + b"/docs/adr/0001-d\xe9cision.md", "wb") as handle:
        handle.write(b"Scope: lib/**\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "change")
    return {"path": str(r), "base": base, "head": git(r, "rev-parse", "HEAD")}


def test_ac3_a_non_ascii_and_a_non_utf8_path_both_rate_red_under_the_default_quotepath(default_quotepath_repo):
    repo = default_quotepath_repo
    assert subprocess.run(["git", "-C", repo["path"], "config", "core.quotepath"], capture_output=True,
                          text=True).stdout.strip() == ""  # git's default: names are quoted without -z
    gitrepo.forget()
    detail = blast.classify(repo["path"], repo["base"], repo["head"])
    colours = {f["path"]: f["colour"] for f in detail["files"]}
    assert colours["src/café.txt"] == "red"
    assert colours["src/caf�-latin.txt"] == "red"
    assert detail["lane"] == "red"
    assert not any("\\" in path for path in colours)  # no octal escapes


def test_ac4_an_adr_with_a_non_utf8_name_still_applies_its_scope(default_quotepath_repo):
    repo = default_quotepath_repo
    scopes = blast.adr_scopes(repo["path"], repo["head"])
    assert scopes == [("docs/adr/0001-d�cision.md", ["lib/**"])]
    detail = blast.classify(repo["path"], repo["base"], repo["head"])
    lib = next(f for f in detail["files"] if f["path"] == "lib/b.py")
    assert lib["colour"] == "yellow" and lib["adr"] == "docs/adr/0001-d�cision.md"
