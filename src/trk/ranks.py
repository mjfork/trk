"""Fractional-index rank keys: base-62 digit strings compared bytewise.

A key never ends in "0", so there is always room below it. Appending (`--last`) and prepending
(`--first`) bump one digit, so a long list grows a char only every ~30 keys; inserting between two
keys takes their midpoint, which grows a char every ~6 inserts at one spot. A key past MAX_LENGTH
is the caller's cue to re-key the siblings with `spread`.
"""

DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(DIGITS)
MID = DIGITS[BASE // 2]
MAX_LENGTH = 32


def key_between(lo, hi):
    """A key strictly between `lo` and `hi`; None means unbounded on that side."""
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError(f"rank {lo!r} is not below {hi!r}")
    if hi is None:
        return MID if lo is None else _increment(lo)
    if lo is None:
        return _decrement(hi)
    return _midpoint(lo, hi)


def _increment(key):
    """Bump the first digit that can go up, dropping what follows it."""
    for i, char in enumerate(key):
        if char != DIGITS[-1]:
            return key[:i] + DIGITS[DIGITS.index(char) + 1]
    return key + MID


def _decrement(key):
    """Bump the first digit that can go down without becoming a trailing "0"."""
    for i, char in enumerate(key):
        if DIGITS.index(char) > 1:
            return key[:i] + DIGITS[DIGITS.index(char) - 1]
    return _midpoint("", key)


def _midpoint(lo, hi):
    """The midpoint of two digit strings, `lo` < `hi` (hi None: unbounded); "" is the bottom."""
    if hi is not None:
        n = 0
        while (lo[n] if n < len(lo) else DIGITS[0]) == hi[n]:
            n += 1
        if n:
            return hi[:n] + _midpoint(lo[n:], hi[n:])
    digit_lo = DIGITS.index(lo[0]) if lo else 0
    digit_hi = DIGITS.index(hi[0]) if hi is not None else BASE
    if digit_hi - digit_lo > 1:
        return DIGITS[(digit_lo + digit_hi + 1) // 2]
    if hi is not None and len(hi) > 1:
        return hi[0]
    return DIGITS[digit_lo] + _midpoint(lo[1:], None)


def spread(count):
    """`count` short keys, evenly spaced, in ascending order, with room around each."""
    length = 1
    while BASE**length < (count + 1) * BASE:
        length += 1
    step = BASE**length / (count + 1)
    return [_encode(round(step * (i + 1)), length) for i in range(count)]


def _encode(value, length):
    digits = []
    for _ in range(length):
        value, digit = divmod(value, BASE)
        digits.append(DIGITS[digit])
    return "".join(reversed(digits)).rstrip(DIGITS[0])
