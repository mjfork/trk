"""Sections: an item's body as named parts, addressed by lowercase kebab handles.

The rules and the reads live here; the writes that journal are in items, beside the other item writes.
"""

import re

from . import clock
from .errors import not_found, refused, usage

MAX_BYTES = 256 * 1024

_HANDLE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

# The Item data model's Sections table, keyed by internal (kind, form). `show` lists these first.
EXPECTED = {
    ("program", None): ("destination", "notes", "not-yet-specified"),
    ("project", None): ("problem", "solution", "user-stories", "implementation-decisions", "testing-decisions",
                        "acceptance-criteria", "out-of-scope"),
    ("work_item", "spec_bound"): ("what-to-build", "acceptance-criteria"),
    ("work_item", "self_contained"): ("current-behaviour", "desired-behaviour", "acceptance-criteria"),
    ("work_item", "findings"): ("question", "findings", "acceptance-criteria"),
    ("work_item", "decision"): ("question", "resolution"),
    ("work_item", "ask"): ("question", "answer"),
}


def check_handle(handle):
    if not _HANDLE.fullmatch(handle):
        raise usage(f"section handle {handle!r} is not a lowercase kebab slug (like acceptance-criteria)")


def check_body(handle, body, item=None):
    size = len(body.encode("utf-8"))
    if size > MAX_BYTES:
        raise refused(
            "section-too-large", f"section {handle} is {size} bytes; a section holds at most {MAX_BYTES}", item=item
        )


def check_all(section_bodies):
    """Check [(handle, body)] given at creation: valid handles, none twice, bodies within the limit."""
    handles = [handle for handle, _ in section_bodies]
    for handle in handles:
        check_handle(handle)
    if len(set(handles)) != len(handles):
        raise usage("a section handle is given twice")
    for handle, body in section_bodies:
        check_body(handle, body)
    return handles


def check_written(row, handle):
    """Refuse a write to the item's generated section (rule `generated-section`); returns the item id."""
    from . import criteria

    if handle == criteria.generated_handle(row["kind"], row["form"]):
        raise refused("generated-section", f"{handle} is generated from {row['id']}'s criterion rows; use "
                      "`trk criterion add|edit|retire`", item=row["id"])
    return row["id"]


def _is_generated(row, handle):
    from . import criteria

    return handle == criteria.generated_handle(row["kind"], row["form"])


def body(conn, item_id, handle):
    """The stored body, or None when the item has no such section."""
    row = conn.execute("SELECT body FROM sections WHERE item_id = ? AND handle = ?", (item_id, handle)).fetchone()
    return None if row is None else row["body"]


def put(conn, item_id, handle, text, now):
    """Insert or replace one section. An existing row keeps its rowid, which is its creation order."""
    conn.execute(
        "INSERT INTO sections (item_id, handle, body, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (item_id, handle) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
        (item_id, handle, text, clock.stamp(now)),
    )


def delete(conn, item_id, handle):
    """Remove one section; False if there was none."""
    return conn.execute("DELETE FROM sections WHERE item_id = ? AND handle = ?", (item_id, handle)).rowcount > 0


def read(conn, row, handle):
    """The raw body of one section, or the generated text of a generated one; exit 3 if absent."""
    check_handle(handle)
    item_id = row["id"]
    if _is_generated(row, handle):
        from . import criteria

        text = criteria.generated(conn, item_id)
    else:
        text = body(conn, item_id, handle)
    if text is None:
        raise not_found(f"{item_id} has no section {handle}", item=item_id)
    return text


def listing(conn, row, bodies=True):
    """Expected handles in table order (missing ones with a null body), then extras in creation order."""
    stored = {
        r["handle"]: r["body"]
        for r in conn.execute("SELECT handle, body FROM sections WHERE item_id = ? ORDER BY rowid", (row["id"],))
    }
    expected = EXPECTED.get((row["kind"], row["form"]), ())
    handles = [*expected, *(h for h in stored if h not in expected)]
    result = []
    for handle in handles:
        if _is_generated(row, handle):
            from . import criteria

            generated = criteria.generated(conn, row["id"])
            entry = {"handle": handle, "expected": handle in expected, "present": generated is not None,
                     "generated": True}
            if bodies:
                entry["body"] = generated
            result.append(entry)
            continue
        entry = {"handle": handle, "expected": handle in expected, "present": handle in stored}
        if bodies:
            entry["body"] = stored.get(handle)
        result.append(entry)
    return result
