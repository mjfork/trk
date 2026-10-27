"""Needs-you pages: everything waiting on one person, across every root (Page publisher spec: Needs-you pages).

A person's page is a `pages` row keyed `needs-you:<actor>`, published, diffed, marked and pulled like an
item's page. It is not owned by any root and is never deleted: with nothing waiting it says so.

- **Waiting on you** (`waiting`): every open ask whose role resolves to the person now, in `trk waiting
  --me` order. Each `ask-<id>` box is the very element the program pages show (`render._Page.ask_box`), so a
  comment on it answers the ask as a comment on any other page does; the root program sits beside it.
- **Blocked on you** (`blocked`): every open page-bearing item that is blocked (an open blocks edge or an
  open child ask) by something the person holds: an open ask resolving to them, or an open decision or
  ticket whose effective owner they are. One row per item, `blocked-<id>`, in tree order.

Loaded only by the page modules, like `render`.
"""

from . import actors, asks, clock, items, ready, render, stages
from .render import _attr, _e, stamp

PREFIX = render.NEEDS_YOU
KIND = "needs-you"
LEVEL = 1_000_000  # after every item page in a flush pass: the page links theirs
HEADING = "Waiting on you"  # the waiting box's heading, which the pull's pointer at the boxes names


def key(actor_id):
    return PREFIX + actor_id


def is_key(page_key):
    return isinstance(page_key, str) and page_key.startswith(PREFIX)


def actor_of(page_key):
    return page_key[len(PREFIX):]


def people(conn):
    """The actors who have a Needs-you page, by id: every human an open ask resolves to, the default owner
    when human, and everyone who has one already (a page is kept with nothing waiting)."""
    found = {asks.resolve(conn, row) for row in asks.waiting(conn)}
    found.add(conn.execute("SELECT value FROM meta WHERE key = 'default_owner'").fetchone()[0])
    humans = {row["id"] for row in conn.execute("SELECT id FROM actors WHERE kind = 'human'")}
    kept = {actor_of(row[0]) for row in conn.execute("SELECT item_id FROM pages WHERE item_id LIKE ?", (PREFIX + "%",))}
    return sorted((found & humans) | kept)


def page_keys(conn):
    """The keys of the person pages that have a `pages` row."""
    return [row[0] for row in conn.execute("SELECT item_id FROM pages WHERE item_id LIKE ? ORDER BY item_id",
                                           (PREFIX + "%",))]


def title(conn, actor_id):
    return f"Needs you: {actors.display(conn, actor_id)}"


def entry(conn, actor_id):
    """The stand-in item `pages.page_rows` lists for a person's page."""
    return {"id": key(actor_id), "title": title(conn, actor_id), "kind": KIND, "form": None}


def resolve_key(conn, raw):
    """`needs-you:<actor>` for a registered actor (exit 3 for an unknown one), else None."""
    if not is_key(raw):
        return None
    actors.get(conn, actor_of(raw))
    return raw


def waiting(conn, actor_id, now=None):
    return asks.waiting(conn, me=actor_id, now=now)


def blocked(conn, actor_id):
    """[(item row, [blocker rows])] for every open page-bearing item blocked by something the person holds,
    in tree order; blockers in `why_not` order."""
    found = []
    for row, _level in items.outline(conn):
        if render.page_kind(row) is None:
            continue
        rules = ready.status(conn, row)["why_not"]
        ids = [rule.split(":", 1)[1] for rule in rules if rule.startswith(("blocked-by:", "open-ask:"))]
        holding = [blocker for blocker in (items.get(conn, i) for i in ids) if _holds(conn, blocker, actor_id)]
        if holding:
            found.append((row, holding))
    return found


def _holds(conn, row, actor_id):
    """Whether an open blocker waits on the person: an ask resolving to them (a held one waits on no one yet),
    or a decision or ticket they own."""
    if row["status"] != "open":
        return False
    if row["form"] == "ask":
        return asks.held_reason(conn, row) is None and asks.resolve(conn, row) == actor_id
    if row["form"] == "decision" or stages.is_ticket(row):
        return items.hierarchy(conn, row["id"])["effective_owner"] == actor_id
    return False


def addressee_at(conn, row, moment):
    """Who an ask was addressed to at `moment` (a stamp): the addressee of its latest opening, reopening or
    readdressing at or before then; its addressee now when the journal has none."""
    for change in conn.execute("SELECT at, addressee FROM journal WHERE item = ? AND addressee IS NOT NULL AND "
                               "change IN ('ask.opened', 'ask.reopened', 'ask.readdressed') ORDER BY seq DESC",
                               (row["id"],)):
        if clock.stamp(clock.parse(change["at"])) <= moment:
            return change["addressee"]
    return asks.resolve(conn, row)


def render_page(conn, actor_id, urls=None):
    """The person's page as HTML text: a pure function of the store, like every page.

    `urls` replaces the published URLs the page links to (`render.published_urls`), for a caller that
    serves the pages itself rather than publishing them."""
    return _NeedsYou(conn, actor_id, urls).html()


CSS = """
.board.needs-you .root{margin:0 0 2px;font-size:.85rem;color:var(--muted)}
""".strip()


class _NeedsYou(render._Page):
    def __init__(self, conn, actor_id, urls=None):
        self.actor = actors.get(conn, actor_id)["id"]
        self.conn, self.row, self.kind = conn, None, KIND
        self.now = clock.now()  # the urgency lines and their order are as of this moment
        self.urls = render.published_urls(conn) if urls is None else dict(urls)
        self.names = {r["id"]: r["display"] for r in conn.execute("SELECT id, display FROM actors ORDER BY id")}

    def html(self):
        asked, held = waiting(self.conn, self.actor, self.now), blocked(self.conn, self.actor)
        body = [self.head(len(asked), len(held)), self.waiting_box(asked), self.blocked(held)]
        return (
            "<!doctype html>\n"
            '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{_e(title(self.conn, self.actor))}</title>\n<style>{render.CSS}\n{CSS}</style>\n</head>\n"
            f'<body>\n<main class="board {KIND}">\n' + "\n".join(body) + "\n</main>\n</body>\n</html>\n"
        )

    def head(self, waiting_count, blocked_count):
        facts = [("Person", self.who(self.actor)), ("Waiting on you", str(waiting_count)),
                 ("Blocked on you", str(blocked_count))]
        dl = "".join(f"<div><dt>{name}</dt><dd>{value}</dd></div>" for name, value in facts)
        return (f'<header class="head"{_attr("header")}>\n<div class="pills"><span class="pill">needs you</span></div>\n'
                f"<h1>{_e(title(self.conn, self.actor))}</h1>\n<dl class=\"facts\">{dl}</dl>\n"
                "<p class=\"outcome\">Everything waiting on this person, across every program. "
                "Answer an ask by commenting on its box, here or on its program's page.</p>\n</header>")

    def root_line(self, item_id):
        root = items.lineage(self.conn, item_id)[-1]
        row = items.get(self.conn, root["id"])
        return f'<p class="root">{_e(items.KIND_OUT[row["kind"]].capitalize())}: {self.link(row["id"], row["title"])}</p>'

    def waiting_box(self, rows):
        if not rows:
            return (f'<section class="waiting none"{_attr("waiting")}>\n<h2>{HEADING}</h2>\n'
                    '<p class="clear">Nothing is waiting on you</p>\n</section>')
        boxes = "\n".join(f"{self.root_line(row['id'])}\n{self.ask_box(row)}" for row in rows)
        return f'<section class="waiting"{_attr("waiting")}>\n<h2>{HEADING}</h2>\n{boxes}\n</section>'

    def blocked(self, found):
        handle, heading = "blocked", "<h2>Blocked on you</h2>"
        if not found:
            return f'<section class="rollup"{_attr(handle)}>\n{heading}\n<p class="none">None</p>\n</section>'
        lines = []
        for row, holding in found:
            root = items.lineage(self.conn, row["id"])[-1]["id"]
            program = self.link(root, items.get(self.conn, root)["title"])
            by = "<br>".join(self.blocker(blocker) for blocker in holding)
            urgent = ' <span class="flag urgent">urgent</span>' if row["urgent"] else ""
            cells = [self.link(row["id"], row["title"]) + urgent, _e(self.kind_label(row)), program, by]
            lines.append(f'<tr{_attr("blocked-" + row["id"])}>' + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        head = "".join(f"<th>{name}</th>" for name in ("Item", "Kind", "Program", "Blocked by"))
        return (f'<section class="rollup"{_attr(handle)}>\n{heading}\n<div class="scroll"><table>\n'
                f"<thead><tr>{head}</tr></thead>\n<tbody>\n" + "\n".join(lines) + "\n</tbody>\n</table></div>\n</section>")

    def blocker(self, row):
        """An ask (it has no page) as its title and role; a decision or ticket linking its page."""
        if row["form"] == "ask":
            return f'ask {self.ask_link(row, _e(row["title"]))} <span class="tag">{_e(row["addressee_role"])}</span> ' \
                   f'<span class="ev">opened {stamp(asks.opened_at(self.conn, row))}</span>'
        return f'{_e(self.kind_label(row))} {self.link(row["id"], row["title"])}'
