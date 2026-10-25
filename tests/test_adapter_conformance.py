"""`python -m trk.conformance`: the protocol checks any page adapter can run, here against the reference
folder adapter (as a child process), the fake adapter, and an adapter that ignores `base_version`."""

import io
import shlex
import subprocess
import sys

from fake_backend import ACTIVITY, FakeBackend
from trk import adapter, conformance, folder


def test_the_folder_adapter_passes(tmp_path):
    out = io.StringIO()
    assert conformance.run(shlex.join([sys.executable, "-m", "trk.folder", "--dir", str(tmp_path)]), out=out), out.getvalue()
    assert "FAIL" not in out.getvalue()


def test_the_fake_adapter_with_comments_and_activity_passes(tmp_path):
    token = tmp_path / "token"
    token.write_text(FakeBackend().token)
    fake = FakeBackend(activity=True)
    fake.grants.add(ACTIVITY)
    out = io.StringIO()
    assert conformance.run(f"fake-adapter --token-file {token}", runner=fake.runner(), out=out), out.getvalue()
    assert "ok    activity" in out.getvalue()


class Overwriting(folder.Backend):
    """Ignores `base_version`: every write wins."""

    def update(self, slug, *, base_version, **kwargs):
        current = self._index()[slug]["version"]
        return super().update(slug, base_version=current, **kwargs)


def test_an_adapter_that_ignores_the_base_version_fails(tmp_path):
    out = io.StringIO()
    runner = adapter.in_process(lambda argv: Overwriting(str(tmp_path)))
    assert not conformance.run("overwriting", runner=runner, out=out)
    assert "FAIL  update on a stale version" in out.getvalue()


def test_the_module_runs_as_a_program(tmp_path):
    done = subprocess.run([sys.executable, "-m", "trk.conformance", "--", sys.executable, "-m", "trk.folder",
                           "--dir", str(tmp_path)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
