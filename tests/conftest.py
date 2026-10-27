import json

import pytest

from feed_contract import assert_feed_valid
from trk import spawn
from trk.cli import main


@pytest.fixture(autouse=True)
def trk_home(tmp_path, monkeypatch, request):
    """Every test gets its own store dir; no test can reach the live store.

    Afterwards, every journal row the test produced must be contract-valid (mark a test
    `feed_unchecked` only if it plants invalid rows on purpose).
    """
    home = tmp_path / "trk"
    monkeypatch.setenv("TRK_HOME", str(home))
    monkeypatch.setenv("TRK_TEST", "1")
    monkeypatch.setenv("TRK_GH", str(tmp_path / "no-gh-installed"))  # `gh` is never the real one; see test_blast_stale_base
    monkeypatch.setenv("TRK_AZ", str(tmp_path / "no-az-installed"))  # nor `az`; see test_blast_azure
    for var in ("TRK_ACTOR", "TRK_SESSION", "TRK_NOW", "TRK_BUSY_TIMEOUT_MS", "CLAUDECODE"):  # the suite runs inside agent sessions too
        monkeypatch.delenv(var, raising=False)
    yield home
    if not request.node.get_closest_marker("feed_unchecked"):
        assert_feed_valid(home)


@pytest.fixture(autouse=True)
def spawned(monkeypatch):
    """The store dirs a write would start a detached flusher for: recorded, never started, so no
    test leaves a `trk pages flush --wait` running behind it."""
    calls = []
    monkeypatch.setattr(spawn, "launch", calls.append)
    return calls


class Result:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err

    def json(self):
        return json.loads(self.out)

    def error(self):
        return json.loads(self.err)["error"]


@pytest.fixture
def trk(capsys):
    """Run the CLI through main(argv); returns exit code and captured output."""

    def run(*argv):
        code = main(list(argv))
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return run
