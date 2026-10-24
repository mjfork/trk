"""Rank keys: base-62 fractional indexes compared bytewise."""

import pytest

from trk import ranks


def test_first_key_is_the_middle_digit():
    assert ranks.key_between(None, None) == "V"


@pytest.mark.parametrize(
    "lo, hi",
    [("V", None), (None, "V"), ("V", "W"), ("V", "VV"), ("z", None), (None, "1"), ("01", "1"), ("Vz", "W")],
)
def test_key_between_sorts_strictly_between(lo, hi):
    key = ranks.key_between(lo, hi)
    assert (lo is None or lo < key) and (hi is None or key < hi)
    assert not key.endswith("0")


def test_appending_grows_slowly():
    key = None
    for _ in range(300):
        nxt = ranks.key_between(key, None)
        assert key is None or key < nxt
        key = nxt
    assert len(key) <= 11


def test_repeated_inserts_at_one_spot_stay_ordered_and_grow():
    lo, hi = "V", "W"
    for _ in range(200):
        hi_new = ranks.key_between(lo, hi)
        assert lo < hi_new < hi
        hi = hi_new
    assert len(hi) > ranks.MAX_LENGTH


@pytest.mark.parametrize("count", [1, 2, 61, 62, 500])
def test_spread_gives_short_evenly_ordered_keys(count):
    keys = ranks.spread(count)
    assert len(keys) == count
    assert keys == sorted(keys) and len(set(keys)) == count
    assert all(k and not k.endswith("0") and len(k) <= 4 for k in keys)
    # Room is left around every key.
    assert ranks.key_between(None, keys[0]) and ranks.key_between(keys[-1], None)
