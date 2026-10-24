"""Item ids: <prefix>-<5 chars> drawn from lowercase Crockford base32."""

import re
import secrets

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
LENGTH = 5
LONG_LENGTH = 6
COLLISIONS_BEFORE_LONG = 3

PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
_CROCKFORD_READS = str.maketrans({"i": "1", "l": "1", "o": "0", "u": "v"})


def new_id(prefix, taken, draw=secrets.choice):
    """Draw a fresh id; `taken(id)` says whether it already exists.

    On a collision, draw again; after three collisions in one creation, draw 6 chars.
    """
    collisions = 0
    while True:
        length = LENGTH if collisions < COLLISIONS_BEFORE_LONG else LONG_LENGTH
        candidate = f"{prefix}-" + "".join(draw(ALPHABET) for _ in range(length))
        if not taken(candidate):
            return candidate
        collisions += 1


def normalize(raw):
    """Read an input id the Crockford way: case-insensitive, `i l o u` as `1 1 0 v`.

    Only the part after the prefix is remapped (prefixes are plain lowercase slugs).
    There is no prefix-abbreviation matching.
    """
    text = raw.strip().lower()
    prefix, dash, chars = text.rpartition("-")
    if not dash:
        return text
    return f"{prefix}-{chars.translate(_CROCKFORD_READS)}"
