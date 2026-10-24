"""The docs stay true: cli.md matches `--help`, the README quickstart runs, and the package names no
machine-specific path."""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def load_generator():
    spec = importlib.util.spec_from_file_location("gen_cli_docs", REPO / "scripts" / "gen_cli_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shim(tmp_path):
    """A `trk` on PATH that runs this checkout's source."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trk = bin_dir / "trk"
    trk.write_text(f'#!/usr/bin/env bash\nPYTHONPATH={REPO / "src"} exec {sys.executable} -m trk "$@"\n')
    trk.chmod(0o755)
    return bin_dir


def test_cli_md_is_current():
    generated = load_generator().render()
    assert (REPO / "docs" / "cli.md").read_text() == generated, "run: PYTHONPATH=src python scripts/gen_cli_docs.py"


def test_cli_md_matches_each_verbs_real_help(tmp_path):
    bin_dir = shim(tmp_path)
    env = {**os.environ, "COLUMNS": "80", "PATH": f"{bin_dir}:{os.environ['PATH']}", "TRK_HOME": str(tmp_path / "s"),
           "TRK_TEST": "1"}
    text = (REPO / "docs" / "cli.md").read_text()
    sections = dict(re.findall(r"^## trk ([^\n]+)\n\n```text\n(.*?)\n```", text, re.S | re.M))
    gen = load_generator()
    sys.path.insert(0, str(REPO / "src"))
    from trk.cli import build_parser

    verbs = [" ".join(words) for words, _ in gen.verbs(build_parser())]
    assert verbs and set(sections) == set(verbs)
    for verb in verbs:
        done = subprocess.run(["trk", *verb.split(), "--help"], env=env, capture_output=True, text=True, check=False)
        assert done.returncode == 0, verb
        assert sections[verb] == done.stdout.rstrip(), verb


def test_no_machine_specific_paths_in_the_package():
    needle = "/ho" + "me/"  # spelled apart so this file does not match itself
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout.split(b"\0")
    hits = []
    for name in filter(None, listed):
        path = REPO / name.decode()
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" not in data and needle.encode() in data:
            hits.append(name.decode())
    assert hits == []


def quickstart_blocks():
    text = (REPO / "README.md").read_text()
    return re.findall(r"^```bash quickstart\n(.*?)^```", text, re.S | re.M)


def test_readme_quickstart_runs_on_a_fresh_store(tmp_path):
    blocks = quickstart_blocks()
    assert len(blocks) >= 5
    script = tmp_path / "quickstart.sh"
    script.write_text("set -euo pipefail\n" + "\n".join(blocks))
    work = tmp_path / "work"
    work.mkdir()
    env = {**os.environ, "PATH": f"{shim(tmp_path)}:{os.environ['PATH']}", "TMPDIR": str(tmp_path), "TRK_TEST": "1"}
    for var in ("TRK_HOME", "TRK_SESSION", "TRK_ACTOR"):
        env.pop(var, None)
    done = subprocess.run(["bash", str(script)], cwd=work, env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert "closed (done)" in done.stdout


def test_readme_table_of_contents_links_every_h2():
    text = (REPO / "README.md").read_text()
    toc = text.split("## Contents", 1)[1].split("\n## ", 1)[0]
    for heading in re.findall(r"^## (.+)$", text, re.M):
        if heading == "Contents":
            continue
        slug = re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
        assert f"(#{slug})" in toc, heading
