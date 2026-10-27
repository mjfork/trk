"""The one check that the `trk` on PATH is the commit the acceptance files are about.

`test_acceptance`, `test_asks_acceptance`, `test_gates_acceptance` and `test_hardening` drive the binary
rather than the source, so their expectations are about a commit. A PATH trk from another commit answers
for that one instead: `trk init` registering `worker` passed on a machine whose installed trk predated it
and failed on a box whose PATH trk was built from the commit under test. Those files now skip with the
reason when the binary cannot answer for this checkout, which keeps a wrong binary from being tested but
makes the whole family quiet. A release run sets `TRK_PATH_MUST_MATCH=1` so the mismatch fails here, by
name, instead of hiding in a skip count.
"""

import pytest

from installed_trk import MUST_MATCH, STALE


def test_the_trk_on_path_answers_for_this_checkout():
    if not MUST_MATCH:
        pytest.skip("set TRK_PATH_MUST_MATCH=1 to require it, as a release run does")
    assert not STALE, STALE
