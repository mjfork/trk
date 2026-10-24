"""Page comments routed to an item's owner (tr-yd424, spec 04 Comments as answers), and what an agent does
with them: `trk show` lists the item's routed comments nobody has acknowledged, `trk comment reply` answers one
in its thread through the page outbox, and `trk comment ack` takes it off the list.

A routed comment is a person's root comment on an item page outside an ask box. The pull journals it as
`comment.routed` (item = the page's item, owner and addressee = its effective owner, actor = the commenter;
data slug, handle, comment_id, excerpt). On a Needs-you page the row has no item and `data.page` names the
page (`needs-you:<actor>`); its addressee is the page's person. `comment.acknowledged` (actor = who
acknowledged it; data comment_id, and ask when the point became one) closes it. The comment's own text is
kept in `page_comments.body`.
"""

import json

from . import asks, clock, items
from .errors import refused

ROUTED, ACKNOWLEDGED = "comment.routed", "comment.acknowledged"


def route(tx, item_id, slug, comment, author, owner):
    """Journal `comment.routed` for a comment on `item_id`'s page (or on the Needs-you page `item_id`, whose
    person is `owner`), as `author`."""
    on_item = not item_id.startswith("needs-you:")
    data = {"slug": slug, "handle": comment.get("handle"), "comment_id": comment["id"],
            "excerpt": asks.excerpt(comment.get("comment") or "")}
    if not on_item:
        data["page"] = item_id
    fields = items.feed_fields(tx.conn, item_id) if on_item else {}
    tx.journal(ROUTED, fields, data=data, addressee=owner, actor=author)


def _routed(conn, item_id=None, comment_id=None):
    sql = ("SELECT j.seq, j.item, j.actor, j.at, j.addressee, j.data FROM journal j WHERE j.change = ?"
           + (" AND j.item = ?" if item_id else "") + (" AND json_extract(j.data, '$.comment_id') = ?" if comment_id else "")
           + " AND NOT EXISTS (SELECT 1 FROM journal a WHERE a.change = ? AND "
             "json_extract(a.data, '$.comment_id') = json_extract(j.data, '$.comment_id')) ORDER BY j.seq")
    args = [ROUTED, *([item_id] if item_id else []), *([comment_id] if comment_id else []), ACKNOWLEDGED]
    return conn.execute(sql, args).fetchall()


def unacknowledged(conn, item_id):
    """The item's routed comments nobody has acknowledged, oldest first: {comment, author, handle, at, body}."""
    found = []
    for row in _routed(conn, item_id):
        data = json.loads(row["data"])
        text = conn.execute("SELECT body FROM page_comments WHERE comment_id = ?", (data["comment_id"],)).fetchone()
        found.append({"comment": data["comment_id"], "author": row["actor"], "handle": data["handle"],
                      "at": row["at"], "body": (text["body"] if text and text["body"] is not None else data["excerpt"])})
    return found


def _open(conn, comment_id):
    """The routed, unacknowledged comment `comment_id`: its journal row and its page's slug."""
    rows = _routed(conn, comment_id=comment_id)
    if not rows:
        known = conn.execute("SELECT 1 FROM journal WHERE change = ? AND json_extract(data, '$.comment_id') = ?",
                             (ROUTED, comment_id)).fetchone()
        if known:
            raise refused("comment-acknowledged", f"comment {comment_id} is already acknowledged", item=None)
        raise refused("no-routed-comment", f"no routed comment {comment_id}: `trk show <item> --json` lists them",
                      item=None)
    return rows[0], json.loads(rows[0]["data"])["slug"]


def reply(tx, comment_id, body):
    """`trk comment reply`: queue `body` in the routed comment's thread through the page outbox. The thread
    stays open. Returns the comment id."""
    from . import sections

    sections.check_body("reply", body)
    _row, slug = _open(tx.conn, comment_id)
    tx.conn.execute("INSERT INTO page_replies (slug, comment_id, body, resolve, created_at) VALUES (?, ?, ?, 0, ?)",
                    (slug, comment_id, body, clock.stamp(tx.now)))
    return comment_id


def acknowledge(tx, comment_id, ask=None):
    """`trk comment ack`: take the comment off `show`'s list, journaled `comment.acknowledged` by the actor;
    `ask` names the ask that took the point on, when it became one. Returns the comment id."""
    row, _slug = _open(tx.conn, comment_id)
    data = {"comment_id": comment_id}
    if ask:
        items.get(tx.conn, ask)
        data["ask"] = ask
    fields = items.feed_fields(tx.conn, row["item"]) if row["item"] else {}
    tx.journal(ACKNOWLEDGED, fields, data=data)
    return comment_id
