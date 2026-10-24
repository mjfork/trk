"""The store's clock: UTC with milliseconds, overridable by $TRK_NOW only under $TRK_TEST=1."""

import os
from datetime import UTC, datetime

from .errors import usage


def now():
    override = os.environ.get("TRK_NOW")
    if override and os.environ.get("TRK_TEST") == "1":
        try:
            parsed = datetime.fromisoformat(override)
        except ValueError:
            raise usage(f"$TRK_NOW is not an ISO time: {override!r}") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return datetime.now(UTC)


def stamp(moment):
    """Format a moment as the store and the feed spell it: 2026-10-10T21:52:03.123Z."""
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse(text):
    """A stamp read back as a moment."""
    return datetime.fromisoformat(text)
