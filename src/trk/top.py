"""The top-level page: every open root item in the store, where each one stands, and what is in
progress below it (Page publisher spec: The top-level page).

One page per store, a `pages` row keyed `top:`, published, diffed, marked and pulled like an item's page.
It belongs to no root and is never deleted. Michael, 2026-10-26: "i just need a single top level page
showing all the root items in trk and where they are".

- **Roots** (`roots`): every OPEN top-level item in rank order, each linking its page, with its owner,
  tickets done, open asks below, how much is in progress below, and its flags.
- **In progress** (`in-progress`): everything under those roots work has started on, the same box every
  item page shows for its own subtree (`render._Page.in_progress_box`).

A closed root leaves the page, and its subtree with it: the totals and the in-progress box count only
what the roots table lists, so every number on the page describes the work it shows. Michael,
2026-10-26: "can we have top level only show OPEN items". Nothing is lost by it — a closed root keeps
its own page and its trail to here, reachable by its URL and from whatever links it.

It is published first in a flush pass, before any item page, because every page's trail links it: each page
published after it is born with that link, so no second pass is needed to put one in. Its own links to the
roots are plain text until their pages exist, which is why its first publish marks every published page due
at once, itself included; pages that render the same are not written.

It shows no ask boxes: an ask is answered on its item's page or on the addressee's Needs-you page, so
the pull never lists this page among the pages showing an ask, and a comment left on it is routed to
the store's default owner.

Loaded only by the page modules, like `render`.
"""

from . import clock, items, render, stages
from .render import _attr, _e

KEY = render.TOP  # the one page key in `pages`
KIND = "top"
LEVEL = -1  # before every item page in a flush pass, so each page born after it links it from birth
TITLE = render.TOP_LABEL


def is_key(page_key):
    return page_key == KEY


def resolve_key(raw):
    """`top:` when a verb names the top-level page, else None."""
    return KEY if is_key(raw) else None


def title(conn=None):
    return TITLE


def entry(conn):
    """The stand-in item `pages.page_rows` lists for the top-level page."""
    return {"id": KEY, "title": TITLE, "kind": KIND, "form": None}


def roots(conn):
    """Every open top-level item in rank order. A closed root is off this page (see the module docstring)."""
    return [row for row in items.children(conn, None, open_only=True) if render.page_kind(row) is not None]


def render_page(conn):
    """The top-level page as HTML text: a pure function of the store, like every page."""
    return _Top(conn).html()


CSS = """
.board.top .intro{color:var(--muted);margin:.2rem 0 0}
""".strip()


class _Top(render._Page):
    def __init__(self, conn):
        self.conn, self.row, self.kind = conn, None, KIND
        self._below = None  # the open roots and their subtrees, read once (see `below`)
        self.now = clock.now()  # read once, as every render does; nothing on this page shows it
        self.urls = {r["item_id"]: r["url"] for r in conn.execute(
            "SELECT item_id, url FROM pages WHERE slug IS NOT NULL AND url IS NOT NULL ORDER BY item_id")}
        self.names = {r["id"]: r["display"] for r in conn.execute("SELECT id, display FROM actors ORDER BY id")}

    def html(self):
        found = roots(self.conn)
        body = [self.head(found), self.tiles_box(found), self.roots_table(found), self.in_progress_box()]
        return (
            "<!doctype html>\n"
            '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{_e(TITLE)}</title>\n<style>{render.CSS}\n{CSS}</style>\n</head>\n"
            f'<body>\n<main class="board {KIND}">\n' + "\n".join(part for part in body if part)
            + "\n</main>\n</body>\n</html>\n"
        )

    def below(self):
        """Every open root this page lists and everything under them, closed descendants too: the scope of
        the totals and of the in-progress box. Read once per render, so the page cannot count a root its
        table leaves out."""
        if self._below is None:
            found = roots(self.conn)
            self._below = found + [row for root in found for row in self.subtree(root["id"])]
        return self._below

    # --- header and totals ---

    def head(self, found):
        # No render time here: an absolute "Updated" fact would change these bytes on every render, so every
        # flush would rewrite the page. A render stays a pure function of the store (`render`'s docstring).
        facts = [("Top-level items", f"{len(found)} open")]
        people = self.needs_you()
        if people:
            facts.append(("Needs you", ", ".join(people)))
        dl = "".join(f"<div><dt>{name}</dt><dd>{value}</dd></div>" for name, value in facts)
        return (f'<header class="head"{_attr("header")}>\n<div class="pills"><span class="pill">top level</span></div>\n'
                f"<h1>{_e(TITLE)}</h1>\n"
                '<p class="intro">Every open top-level item in this tracker and where it stands. '
                'Follow one to its projects, tickets and open questions.</p>\n'
                f'<dl class="facts">{dl}</dl>\n</header>')

    def tiles_box(self, found):
        rows = self.below()
        tickets = [r for r in rows if stages.is_ticket(r)]
        done = sum(1 for r in tickets if r["resolution"] == "done")
        inner = "\n".join([
            self.tile(len(found), "top-level items"),
            self.tile(sum(1 for r in rows if r["kind"] == "project" and r["status"] == "open"), "projects"),
            self.tile(f"{done}/{len(tickets)}", "tickets done"),
            self.tile(sum(1 for r in rows if r["form"] == "decision" and r["status"] == "open"), "open decisions"),
            self.tile(sum(1 for r in rows if r["form"] == "ask" and r["status"] == "open"), "asks waiting"),
            self.tile(len(render.in_progress(self.conn, rows)), "in progress"),
        ])
        return f'<section class="tiles"{_attr("tiles")}>\n{inner}\n</section>'

    # --- the roots table ---

    def roots_table(self, found):
        return self.table("roots", "Top-level items",
                          ("Item", "Kind", "Owner", "Tickets", "Open asks", "In progress", "Status"),
                          found, self.root_row)

    def root_row(self, row):
        below = self.subtree(row["id"])
        tickets = [r for r in below if stages.is_ticket(r)]
        done = sum(1 for r in tickets if r["resolution"] == "done")
        moving = len(render.in_progress(self.conn, below))
        return [self.link(row["id"], row["title"]), _e(self.kind_label(row)), self.who(self.owner(row)),
                f"{done}/{len(tickets)}" if tickets else "—",
                str(self.open_asks_below(row["id"])), str(moving) if moving else "—", self.status_cell(row)]
