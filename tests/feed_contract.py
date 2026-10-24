"""Checks against the JSON Schemas in contract/: the feed, shared by the suite-wide fixture and the contract tests,
and the read verbs' responses."""

import functools
import json
import sqlite3
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

from trk import feed

CONTRACT = Path(__file__).resolve().parents[1] / "contract"


@functools.cache
def _registry():
    """Every contract file under its $id, so one file's $ref to another (item.schema.json) resolves."""
    resources = []
    for path in sorted(CONTRACT.glob("*.schema.json")):
        schema = json.loads(path.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@functools.cache
def validator(name):
    """A validator for contract/<name>.schema.json."""
    schema = json.loads((CONTRACT / f"{name}.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema, registry=_registry())


def feed_validator():
    return validator("changes")


def assert_feed_valid(home):
    """Every journal row a test caused, replayed through the feed, validates against the contract."""
    db = home / "store.db"
    if not db.exists():
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'journal'").fetchone():
            return
        response = feed.changes(conn, 0, limit=None)
    finally:
        conn.close()
    errors = sorted(feed_validator().iter_errors(response), key=lambda e: list(e.absolute_path))
    assert not errors, "feed rows break contract/changes.schema.json:\n" + "\n".join(
        f"  {'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors
    )
