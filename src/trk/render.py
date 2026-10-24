"""Pages: the Board template, rendered for one item from a store snapshot as one static HTML document.

A render is a pure function of the store (plus the `pages` table, for links) and the clock read once at its
start, which only the waiting boxes use: an ask's urgency line (`asks.urgency`) and the order it sorts by. No
other relative ages, no random ids, and every query has a total order, so the same store at the same moment
renders the same bytes. Times are absolute UTC. Every handled element carries `data-comment-id="<handle>"`; plain layout
has none. The HTML has inline CSS, no script and no external resources.

Bump RENDER_VERSION with any change to the templates or the CSS: the publisher compares it with
`meta.render_version` and republishes every page once when they differ.
"""

import json
import re

from . import asks, claims, clock, criteria, evidence, gitrepo, handoffs, items, ledger, markdown, ready, sections, stages
from .errors import refused

RENDER_VERSION = 7
WAITING_HEADING = "Waiting on people"  # the waiting box's heading, which the pull's pointer at the boxes names

NEEDS_YOU = "needs-you:"  # a person's page key in `pages`: `needs-you:<actor>` (see `needs_you`)

# Page handles the template itself uses. A written section named like one renders as `s-<handle>`.
RESERVED = frozenset({"header", "handoff", "tiles", "stage", "stage-funnel", "waiting", "projects", "open-decisions",
                      "decisions-so-far", "other-children", "tickets", "children", "evidence", "review"})
RESERVED_PREFIXES = ("child-", "ask-", "ac-", "evidence-", "ledger-", "stage-", "s-")

# The section open by default on each page kind: the one its gate turns on.
OPEN_SECTION = {"program": ("destination",), "project": ("acceptance-criteria",),
                "ticket": ("what-to-build", "desired-behaviour"), "decision": ("question",)}

STAGE_LABEL = {"framed": "Framed", "specd": "Spec'd", "built": "Built", "merged": "Merged", "verified": "Verified"}


def page_kind(row):
    """program, project, ticket or decision; None for an ask, which has no page."""
    if row["kind"] != "work_item":
        return row["kind"]
    if row["form"] == "ask":
        return None
    return "decision" if row["form"] == "decision" else "ticket"


def reserved(handle, kind):
    """Whether a written section's handle is taken by the template on a page of this kind.
    `out-of-scope` is a generated list on program pages only."""
    if handle == "out-of-scope":
        return kind == "program"
    return handle in RESERVED or handle.startswith(RESERVED_PREFIXES)


def page_handle(handle, kind):
    return f"s-{handle}" if reserved(handle, kind) else handle


def render(conn, raw_id):
    """The page of one item as HTML text. Asks have no page (rule `no-page`)."""
    row = items.get(conn, raw_id)
    kind = page_kind(row)
    if kind is None:
        raise refused("no-page", f"{row['id']} is an ask; asks have no page of their own: they show in the waiting "
                                 "boxes and child rows of the pages above them", item=row["id"])
    return _Page(conn, row, kind).html()


def stamp(text):
    """A stored time as a page shows it: 2026-10-11 14:03Z."""
    return _e(f"{text[:10]} {text[11:16]}Z") if text else ""


def _e(text):
    return markdown.escape(text or "")


def _attr(handle):
    return f' data-comment-id="{_e(handle)}"'


_SUBTREE = """
WITH RECURSIVE down (id) AS (
    SELECT id FROM items WHERE parent_id = ?
    UNION ALL
    SELECT items.id FROM items JOIN down ON items.parent_id = down.id
)
SELECT items.* FROM items JOIN down ON items.id = down.id ORDER BY items.id
"""


def _page_url(url):
    """A published page's URL as a link: what `markdown.safe_url` allows, and a `file:` URL, which the
    folder backend gives a page it writes to a directory nobody serves. Only the publisher's own page
    URLs get this; a link written in an item never does."""
    if url and url.strip().lower().startswith("file:///"):
        return url
    return markdown.safe_url(url)


class _Page:
    def __init__(self, conn, row, kind):
        self.conn = conn
        self.row = row
        self.kind = kind
        self.now = clock.now()
        self.urls = {r["item_id"]: r["url"] for r in conn.execute(
            "SELECT item_id, url FROM pages WHERE slug IS NOT NULL AND url IS NOT NULL ORDER BY item_id")}
        self.names = {r["id"]: r["display"] for r in conn.execute("SELECT id, display FROM actors ORDER BY id")}

    # --- small pieces ---

    def who(self, actor_id):
        return _e(self.names.get(actor_id, actor_id)) if actor_id else "—"

    def person(self, actor_id):
        """The actor's name, linking their Needs-you page once that page is published (plain text until then)."""
        url = _page_url(self.urls.get(NEEDS_YOU + actor_id)) if actor_id else None
        return f'<a href="{_e(url)}">{self.who(actor_id)}</a>' if url else self.who(actor_id)

    def needs_you(self):
        """Every published Needs-you page, linked by its person's name, by actor id; empty before any."""
        return [self.person(key[len(NEEDS_YOU):]) for key in self.urls
                if key.startswith(NEEDS_YOU) and _page_url(self.urls[key])]

    def link(self, item_id, title):
        """The item's title, linking its page once that page is published (plain text until then)."""
        url = _page_url(self.urls.get(item_id))
        return f'<a href="{_e(url)}">{_e(title)}</a>' if url else _e(title)

    def page_url(self, item_id):
        """The published URL of the item's page, or of its nearest ancestor's when it has none (an ask has
        no page: it shows on the pages above it); None before any of them is published."""
        for link in items.lineage(self.conn, item_id):
            url = _page_url(self.urls.get(link["id"]))
            if url:
                return url
        return None

    def handle_link(self, item_id, handle, label_html):
        """`label_html` linking `<page URL>#<handle>`: always the full URL, even on the page itself, since
        the viewer's one documented form for a section is `<artifact URL>#<data-comment-id>`. Plain text
        until the page is published."""
        url = self.page_url(item_id)
        return f'<a href="{_e(url)}#{_e(handle)}">{label_html}</a>' if url else label_html

    MENTION = re.compile(r"\bledger-([1-9][0-9]*)\b")

    def ledger_mentions(self, html, ticket_id):
        """`html` with each `ledger-<n>` in its text linked to that row's handle on the ticket's page, for the
        rows that exist; text inside a tag or an existing link is left alone."""
        out, in_link = [], False
        for part in re.split(r"(<[^>]*>)", html):
            if part.startswith("<"):
                in_link = (in_link or part.startswith("<a ")) and not part.startswith("</a")
                out.append(part)
            elif in_link:
                out.append(part)
            else:
                out.append(self.MENTION.sub(lambda m: self.ledger_link(ticket_id, int(m.group(1)), m.group(0)), part))
        return "".join(out)

    def ledger_link(self, ticket_id, n, label_html):
        """`label_html` linking the ticket's ledger row `n` (`<page URL>#ledger-<n>`); as is when no such row."""
        if not self.conn.execute("SELECT 1 FROM ledger WHERE item_id = ? AND n = ?", (ticket_id, n)).fetchone():
            return label_html
        return self.handle_link(ticket_id, f"ledger-{n}", label_html)

    def ask_link(self, row, label_html):
        return self.handle_link(row["id"], "ask-" + row["id"], label_html)

    @staticmethod
    def external(url, label_html):
        """An off-site link in the viewer's form: opens in a new tab."""
        return f'<a href="{_e(url)}" target="_blank" rel="noopener">{label_html}</a>'

    def remote(self, row, sha=None):
        """The GitHub web address (`origin`) of the item's checkout that holds `sha` (the same rule as the gates:
        `gitrepo.place`; the first checkout when none holds it or there is no sha), or None."""
        repo = gitrepo.place(gitrepo.repos_of(self.conn, row["id"]), sha)
        return gitrepo.remote_of(repo) if repo else None

    def commit_link(self, row, sha):
        """The short sha, linking its commit on the repo's GitHub remote when there is one."""
        short = f"<code>{_e(sha[:7])}</code>"
        base = self.remote(row, sha)
        return self.external(f"{base}/commit/{sha}", short) if base else short

    def pr_link(self, row, ref, sha=None):
        """A `pr` row's ref as the viewer links it: a bare number is `<remote>/pull/<n>` on the remote of the repo
        that holds the row's `sha`, a URL stands, and anything else (a branch name, `none`) is text."""
        url = markdown.safe_url(ref)
        if url:
            return self.external(url, _e(url))
        number = ref.lstrip("#") if ref else ""
        base = self.remote(row, sha)
        if number.isdigit() and base:
            return self.external(f"{base}/pull/{number}", f"#{_e(number)}")
        return _e(ref)

    def owner(self, row):
        return items.hierarchy(self.conn, row["id"])["effective_owner"]

    def subtree(self, item_id):
        return self.conn.execute(_SUBTREE, (item_id,)).fetchall()

    def open_asks_below(self, item_id):
        return sum(1 for r in self.subtree(item_id) if r["form"] == "ask" and r["status"] == "open")

    def flags(self, row):
        """Derived flags: closed with its resolution, else blocked, in progress and urgent."""
        if row["status"] == "closed":
            return [("closed", f"closed: {items.RESOLUTION_OUT[row['resolution']]}")]
        facts = ready.status(self.conn, row)
        found = [("blocked", "blocked")] if facts["blocked"] else []
        found += [("progress", "in progress")] if facts["in_progress"] else []
        found += [("urgent", "urgent")] if row["urgent"] else []
        return found

    def pills(self, row):
        """The kind pill (and a project's spec state), then the flags."""
        found = [f'<span class="pill">{_e(self.kind_label(row))}</span>']
        if row["spec_state"]:
            found.append(f'<span class="pill">spec {_e(row["spec_state"])}</span>')
        flags = self.flag_pills(row)
        return " ".join(found + ([flags] if flags else []))

    def flag_pills(self, row):
        return " ".join(f'<span class="flag {cls}">{_e(label)}</span>' for cls, label in self.flags(row))

    def status_cell(self, row):
        """Flags, then a closed row's outcome."""
        pills = self.flag_pills(row)
        if row["status"] == "closed" and row["outcome"]:
            pills += f' <span class="outcome">{_e(row["outcome"])}</span>'
        return pills

    def kind_label(self, row):
        if row["kind"] != "work_item":
            return items.KIND_OUT[row["kind"]]
        parts = ["work item", items.FORM_OUT[row["form"]]] + ([row["subtype"]] if row["subtype"] else [])
        return " · ".join(parts)

    # --- the page ---

    def html(self):
        row = self.row
        body = [self.header(), self.tiles(), self.waiting(), *self.rollups(), *self.sections(), self.evidence(), self.review()]
        return (
            "<!doctype html>\n"
            '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{_e(row['title'])} · {_e(row['id'])}</title>\n<style>{CSS}</style>\n</head>\n"
            f'<body>\n<main class="board {self.kind}">\n' + "\n".join(part for part in body if part)
            + "\n</main>\n</body>\n</html>\n"
        )

    def header(self):
        row = self.row
        chain = items.lineage(self.conn, row["id"])[1:]  # ancestors, nearest first
        crumbs = ""
        if chain:
            links = [self.link(link["id"], items.get(self.conn, link["id"])["title"]) for link in reversed(chain)]
            crumbs = f'<nav class="crumbs">{" <span>›</span> ".join(links)}</nav>\n'
        claim = claims.live(self.conn, row["id"])
        facts = [("Owner", self.who(self.owner(row)))]
        if claim is not None:
            facts.append(("Claimed by", self.who(claim["actor"])))  # never the lease: a renewal changes nothing
        jira = markdown.safe_url(row["jira_url"])
        if jira:
            facts.append(("JIRA", self.external(jira, _e(jira))))
        elif row["jira_url"]:
            facts.append(("JIRA", _e(row["jira_url"])))
        facts += self.head_facts(row)
        facts += [("Id", f"<code>{_e(row['id'])}</code>"), ("Updated", stamp(row["updated_at"]))]
        people = self.needs_you() if row["parent_id"] is None else []
        if people:  # the top-level page links every person's page
            facts.append(("Needs you", ", ".join(people)))
        dl = "".join(f"<div><dt>{name}</dt><dd>{value}</dd></div>" for name, value in facts)
        outcome = (f'<p class="outcome">Outcome: {_e(row["outcome"])}</p>\n'
                   if row["status"] == "closed" and row["outcome"] else "")
        return (f'<header class="head"{_attr("header")}>\n{crumbs}'
                f'<div class="pills">{self.pills(row)}</div>\n'
                f"<h1>{_e(row['title'])}</h1>\n{outcome}<dl class=\"facts\">{dl}</dl>\n{self.handoff()}</header>")

    def head_facts(self, row):
        """A ticket's head PR and head sha, linked. With no GitHub remote there is no PR to open, and the
        header says so: `local branch <name> at <sha>, no PR`."""
        if self.kind != "ticket":
            return []
        latest = self.latest_counting(row, "built", kind="pr")
        if latest is None or not latest["ref"]:
            return []
        ref, sha = latest["ref"], latest["sha"]
        if markdown.safe_url(ref) or (ref.lstrip("#").isdigit() and self.remote(row, sha)):
            facts = [("PR", self.pr_link(row, ref, sha))]
        elif ref == "none":
            facts = [("PR", "none: this ticket changes no repo")]
        else:
            name = re.sub(r"^local branch ", "", ref)
            name = re.sub(r" at [0-9a-f]{7,40}\b.*$", "", name)
            facts = [("PR", f"local branch {_e(name)} at <code>{_e(sha[:7])}</code>, no PR" if sha
                      else f"local branch {_e(name)}, no PR")]
        if sha:
            facts.append(("Head", self.commit_link(row, sha)))
        return facts

    def handoff(self):
        latest = handoffs.summary(self.conn, self.row["id"])
        if latest is None:
            return ""
        return (f'<p class="handoff"{_attr("handoff")}>Handoff {stamp(latest["at"])} by {self.who(latest["actor"])}: '
                f'{_e(latest["first_line"])} — <code>trk handoff show {_e(self.row["id"])}</code></p>\n')

    # --- tiles ---

    def tiles(self):
        inner = {"program": self.program_tiles, "project": self.project_tiles, "ticket": self.ticket_tiles,
                 "decision": self.decision_tiles}[self.kind]()
        return f'<section class="tiles"{_attr("tiles")}>\n{inner}\n</section>'

    @staticmethod
    def tile(value, label):
        return f'<div class="tile"><span class="n">{value}</span><span class="l">{_e(label)}</span></div>'

    def program_tiles(self):
        below = self.subtree(self.row["id"])
        projects = [r for r in below if r["kind"] == "project"]
        tickets = [r for r in below if stages.is_ticket(r)]
        done = sum(1 for r in tickets if r["resolution"] == "done")
        fog = _list_items(self.section_body("not-yet-specified"))
        return "\n".join([
            self.tile(len(projects), "projects"),
            self.tile(sum(1 for r in projects if r["spec_state"] == "accepted"), "specs accepted"),
            self.tile(f"{done}/{len(tickets)}", "tickets done"),
            self.tile(sum(1 for r in below if r["form"] == "decision" and r["status"] == "open"), "open decisions"),
            self.tile(sum(1 for r in below if r["form"] == "ask" and r["status"] == "open"), "asks waiting"),
            self.tile(fog, "fog patches"),
        ])

    def project_tiles(self):
        tickets = [r for r in self.subtree(self.row["id"]) if stages.is_ticket(r)]
        done = sum(1 for r in tickets if r["resolution"] == "done")
        cells = "".join(f'<li><span class="n">{sum(1 for r in tickets if r["stage"] == stage)}</span>'
                        f'<span class="l">{STAGE_LABEL[stage]}</span></li>' for stage in stages.STAGES)
        return (f'<ol class="funnel"{_attr("stage-funnel")}>{cells}</ol>\n'
                + self.tile(f"{done}/{len(tickets)}", "tickets done"))

    def ticket_tiles(self):
        row = self.row
        if row["stage"] is None:  # closed before stages existed
            return self.tile(_e(row["status"]), "status")
        reached = stages.STAGES.index(row["stage"])
        cells = []
        for index, stage in enumerate(stages.STAGES):
            state = "reached" if index < reached else "current" if index == reached else "ahead"
            latest = self.latest_counting(row, stage)
            result = ""
            if latest is not None:
                sha = f" {_e(latest['sha'][:7])}" if latest["sha"] else ""
                result = f'<span class="ev">{_e(latest["verdict"] or latest["kind"])}{sha}</span>'
            cells.append(f'<li class="{state}"{_attr("stage-" + stage)}><span class="l">{STAGE_LABEL[stage]}</span>'
                         f"{result}</li>")
        return f'<ol class="pipeline"{_attr("stage")}>{"".join(cells)}</ol>'

    def decision_tiles(self):
        row = self.row
        tiles = [self.tile(_e(row["subtype"]), "subtype"), self.tile(self.who(self.owner(row)), "owner")]
        if row["status"] == "closed":
            tiles.append(self.tile(_e(row["outcome"]), "outcome"))
        return "\n".join(tiles)

    # --- waiting on people ---

    def waiting(self):
        """Every open ask in the subtree, in `trk waiting`'s order. An ask's box depends only on the ask,
        never on the page, so `ask-<id>` is the same element on every ancestor page."""
        boxes = [self.ask_box(row) for row in asks.waiting(self.conn, root=self.row["id"], now=self.now)]
        cls, inner = ("waiting", "\n".join(boxes)) if boxes else \
            ("waiting none", '<p class="clear">Nothing is waiting on people</p>')
        return f'<section class="{cls}"{_attr("waiting")}>\n<h2>{WAITING_HEADING}</h2>\n{inner}\n</section>'

    def ask_box(self, row):
        parent = items.get(self.conn, row["parent_id"])
        question = sections.body(self.conn, row["id"], "question") or ""
        urgent = ' <span class="flag urgent">urgent</span>' if row["urgent"] else ""
        # The whole question, rendered like an item's sections; the feed and `trk waiting` keep the excerpt.
        detail = f'<div class="question">{self.ledger_mentions(markdown.to_html(question), parent["id"])}</div>\n' \
            if question.strip() and question.strip() != row["title"] else ""
        facts = [("Waiting on", f"{_e(row['addressee_role'])} ({self.person(asks.resolve(self.conn, row))})"),
                 ("Asked by", self.who(row["asker"])), ("Opened", stamp(asks.opened_at(self.conn, row))),
                 ("Urgency", _e(asks.urgency(self.conn, row, self.now, as_of=True)["reason"])),
                 ("On", self.link(parent["id"], parent["title"]))]
        tied = ledger.tied_to(self.conn, row["id"])
        if tied:
            facts.append(("Ledger", self.ledger_link(parent["id"], int(tied.split("-")[1]), _e(tied))))
        dl = "".join(f"<div><dt>{name}</dt><dd>{value}</dd></div>" for name, value in facts)
        cls = "ask urgent" if row["urgent"] else "ask"
        replied = asks.replies(self.conn, row)
        reply = (f'<p class="reply">Reply from {self.who(replied[-1]["actor"])}, {stamp(replied[-1]["at"])}: '
                 f'{_e(replied[-1]["excerpt"])} <em>awaiting confirmation</em></p>\n') if replied else ""
        return (f'<div class="{cls}"{_attr("ask-" + row["id"])}>\n'
                f'<p class="q"><strong>{self.ask_link(row, _e(row["title"]))}</strong>{urgent}</p>\n{detail}{reply}'
                f'<dl class="facts">{dl}</dl>\n<p class="cta">Answer by commenting on this box</p>\n</div>')

    # --- rollup tables of direct children ---

    def rollups(self):
        children = items.children(self.conn, self.row["id"])
        if self.kind == "program":
            groups = {"projects": [], "open-decisions": [], "decisions-so-far": [], "out-of-scope": [],
                      "other-children": []}
            for child in children:
                groups[_program_table(child)].append(child)
            groups["decisions-so-far"].sort(key=lambda r: (r["closed_at"], r["id"]))  # close order
            return [
                self.table("projects", "Projects", ("Project", "Owner", "Spec", "Tickets", "Open asks", "Status"),
                           groups["projects"], self.project_row),
                self.table("open-decisions", "Open decisions", ("Decision", "Subtype", "Owner", "Open asks", "Status"),
                           groups["open-decisions"], self.decision_row),
                self.folded_list("decisions-so-far", "Decisions so far", groups["decisions-so-far"]),
                self.folded_list("out-of-scope", "Out of scope", groups["out-of-scope"]),
                self.table("other-children", "Other children", ("Item", "Kind", "Owner", "Open asks", "Status"),
                           groups["other-children"], self.child_row),
            ]
        if self.kind == "project":
            tickets = [c for c in children if stages.is_ticket(c)]
            others = [c for c in children if not stages.is_ticket(c)]
            return [
                self.table("tickets", "Tickets", ("Ticket", "Owner", "PR", "Stage", "Open asks", "Status"), tickets,
                           self.ticket_row),
                self.table("other-children", "Other children", ("Item", "Kind", "Owner", "Open asks", "Status"),
                           others, self.child_row),
            ]
        return [self.table("children", "Children", ("Item", "Kind", "Owner", "Open asks", "Status"), children,
                           self.child_row)]

    def table(self, handle, title, columns, rows, cells):
        if not rows:
            return f'<section class="rollup"{_attr(handle)}>\n<h2>{title}</h2>\n<p class="none">None</p>\n</section>'
        head = "".join(f"<th>{name}</th>" for name in columns)
        body = "\n".join(self.tr(row, cells(row)) for row in rows)
        return (f'<section class="rollup"{_attr(handle)}>\n<h2>{title}</h2>\n<div class="scroll"><table>\n'
                f"<thead><tr>{head}</tr></thead>\n<tbody>\n{body}\n</tbody>\n</table></div>\n</section>")

    def tr(self, row, cells):
        greyed = ' class="closed"' if row["status"] == "closed" else ""
        return f'<tr{greyed}{_attr("child-" + row["id"])}>' + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"

    def folded_list(self, handle, title, rows):
        if rows:
            entries = "\n".join(f'<li{_attr("child-" + row["id"])}>{self.link(row["id"], row["title"])}'
                                f' — {_e(row["outcome"])}</li>' for row in rows)
            inner = f"<ol>\n{entries}\n</ol>"
        else:
            inner = '<p class="none">None</p>'
        return f'<details class="rollup"{_attr(handle)}>\n<summary><h2>{title}</h2></summary>\n{inner}\n</details>'

    def project_row(self, row):
        tickets = [r for r in self.subtree(row["id"]) if stages.is_ticket(r)]
        done = sum(1 for r in tickets if r["resolution"] == "done")
        meter = f'<meter value="{done}" max="{max(len(tickets), 1)}"></meter> {done}/{len(tickets)}'
        return [self.link(row["id"], row["title"]), self.who(self.owner(row)), _e(row["spec_state"]), meter,
                str(self.open_asks_below(row["id"])), self.status_cell(row)]

    def decision_row(self, row):
        return [self.link(row["id"], row["title"]), _e(row["subtype"]), self.who(self.owner(row)),
                str(self.open_asks_below(row["id"])), self.status_cell(row)]

    def ticket_row(self, row):
        title = self.link(row["id"], row["title"])
        if row["form"] == "self_contained":
            title += ' <span class="tag">self-contained</span>'
        return [title, self.who(self.owner(row)), self.pr(row),
                STAGE_LABEL.get(row["stage"], "—"), str(self.open_asks_below(row["id"])), self.status_cell(row)]

    def child_row(self, row):
        if row["form"] == "ask":
            addressee = asks.resolve(self.conn, row)
            who = f"{_e(row['addressee_role'])} ({self.who(addressee)})"
        else:
            who = self.who(self.owner(row))
        title = self.ask_link(row, _e(row["title"])) if row["form"] == "ask" else self.link(row["id"], row["title"])
        return [title, _e(self.kind_label(row)), who, str(self.open_asks_below(row["id"])), self.status_cell(row)]

    def pr(self, row):
        """The head `pr` row's ref (the latest counting one), linked when it is a URL."""
        latest = self.latest_counting(row, "built", kind="pr")
        if latest is None or not latest["ref"]:
            return "—"
        return self.pr_link(row, latest["ref"], latest["sha"])

    # --- evidence ---

    def weighed(self, row):
        """The item's evidence rows in seq order, each weighed by Stage gates (`counts`, `reason`)."""
        return evidence.weigh(self.conn, row).rows

    def latest_counting(self, row, gate, kind=None):
        """The latest row for a gate (and of a kind) that counts, as Stage gates weighs it; None if none does."""
        found = [r for r in self.weighed(row) if r["counts"] and r["gate"] == gate and kind in (None, r["kind"])]
        return found[-1] if found else None

    def evidence(self):
        rows = self.weighed(self.row)
        if not rows:
            return ""
        lines = []
        for r in rows:
            criterion = self.handle_link(self.row["id"], f"ac-{r['criterion_n']}", _e(
                f"ac-{r['criterion_n']}" + (f" v{r['criterion_version']}" if r["criterion_version"] else ""))) \
                if r["criterion_n"] is not None else "—"
            cells = [_e(r["gate"]), _e(r["kind"]), criterion, _e(r["verdict"] or "—"),
                     self.commit_link(self.row, r["sha"]) if r["sha"] else "—", self.who(r["actor"]),
                     stamp(r["created_at"]), self.ledger_mentions(_e(r["note"]), self.row["id"])]
            struck = ' class="retracted"' if r["reason"] == "retracted" else ""
            lines.append(f'<tr{struck}{_attr("evidence-" + r["id"])}>' + "".join(f"<td>{c}</td>" for c in cells)
                         + "</tr>")
        head = "".join(f"<th>{name}</th>" for name in
                       ("Gate", "Kind", "Criterion", "Result", "Sha", "Actor", "Time", "Note"))
        return (f'<section class="rollup"{_attr("evidence")}>\n<h2>Evidence</h2>\n<div class="scroll"><table>\n'
                f"<thead><tr>{head}</tr></thead>\n<tbody>\n" + "\n".join(lines) + "\n</tbody>\n</table></div>\n</section>")

    # --- review: the decision ledger ---

    def review(self):
        if self.kind != "ticket":  # the decision ledger is a ticket's
            return ""
        item_id = self.row["id"]
        entries = self.conn.execute("SELECT * FROM ledger WHERE item_id = ? ORDER BY n", (item_id,)).fetchall()
        if not entries:
            return ""
        lines = []
        for entry in entries:
            verdict = self.conn.execute("SELECT verdict FROM ledger_verdicts WHERE item_id = ? AND n = ?"
                                        " ORDER BY seq DESC LIMIT 1", (item_id, entry["n"])).fetchone()
            settled = self.conn.execute("SELECT * FROM ledger_settlements WHERE item_id = ? AND n = ?"
                                        " ORDER BY seq DESC LIMIT 1", (item_id, entry["n"])).fetchone()
            linked = []
            if settled is not None:
                if settled["ask"]:
                    ask = items.get(self.conn, settled["ask"])  # answered, so its box is gone: plain title
                    linked.append(self.ask_link(ask, _e(ask["title"])) if ask["status"] == "open"
                                  else _e(ask["title"]))
                if settled["ticket"]:
                    linked.append(self.link(settled["ticket"], items.get(self.conn, settled["ticket"])["title"]))
                linked += [_e(settled["adr"])] if settled["adr"] else []
            if entry["refers_to"]:
                linked.append(f"corrects {self.ledger_link(item_id, entry['refers_to'], 'ledger-' + str(entry['refers_to']))}")
            for tied in self.conn.execute("SELECT ask_id FROM ledger_asks WHERE item_id = ? AND n = ? ORDER BY rowid",
                                          (item_id, entry["n"])):
                ask = items.get(self.conn, tied["ask_id"])
                if ask["status"] == "open" and not (settled is not None and settled["ask"] == ask["id"]):
                    linked.append(self.ask_link(ask, _e(ask["title"])))  # a closed ask's box is gone from the page
            alternatives = "; ".join(_alternatives(entry["alternatives"]))
            mention = lambda text: self.ledger_mentions(text, item_id)  # noqa: E731
            cells = [_e(entry["kind"]), mention(markdown.inline(entry["decided"])), mention(_e(alternatives)) or "—",
                     mention(markdown.inline(entry["why"])), _e(entry["origin"]), _e(verdict["verdict"]) if verdict else "—",
                     _e(settled["settlement"]) if settled else "—", ", ".join(linked) or "—"]
            lines.append(f'<tr{_attr("ledger-" + str(entry["n"]))}>' + "".join(f"<td>{c}</td>" for c in cells)
                         + "</tr>")
        head = "".join(f"<th>{name}</th>" for name in
                       ("Kind", "Decided", "Alternatives", "Why", "Origin", "Verdict", "Settlement", "Linked"))
        return (f'<section class="rollup"{_attr("review")}>\n<h2>Review</h2>\n<div class="scroll"><table>\n'
                f"<thead><tr>{head}</tr></thead>\n<tbody>\n" + "\n".join(lines) + "\n</tbody>\n</table></div>\n</section>")

    # --- written sections, folded ---

    def section_body(self, handle):
        return sections.body(self.conn, self.row["id"], handle)

    def sections(self):
        rendered = []
        for entry in sections.listing(self.conn, self.row, bodies=True):
            handle = entry["handle"]
            title = handle.replace("-", " ").capitalize()
            is_open = " open" if handle in OPEN_SECTION[self.kind] else ""
            if entry.get("generated"):
                inner = self.criteria(criteria.listing(self.conn, self.row["id"]))
            elif entry["present"]:
                inner = markdown.to_html(entry["body"])
            else:
                inner = '<p class="none">Not written yet.</p>'
            rendered.append(f'<details class="section"{_attr(page_handle(handle, self.kind))}{is_open}>\n'
                            f"<summary><h2>{_e(title)}</h2></summary>\n{inner}\n</details>")
        return rendered

    def criteria(self, listed):
        """One row per criterion, `ac-<n>`; a retired one stays, struck through, so its handle never drops."""
        if not listed:
            return '<p class="none">None</p>'
        rows = []
        for c in listed:
            version = f' <span class="v">v{c["version"]}</span>'
            if c["retired"]:
                rows.append(f'<li class="retired"{_attr(c["handle"])}><s>{markdown.inline(c["text"])}</s>{version}'
                            f' <span class="ev">retired: {_e(c["retired_reason"])}</span></li>')
                continue
            latest = c["latest"]
            if latest is None:
                status = "no evidence"
            else:
                sha = f", {_e(latest['sha'][:7])}" if latest.get("sha") else ""
                evidence_link = self.handle_link(self.row["id"], "evidence-" + latest["id"], _e(latest["id"]))
                status = f"{_e(c['status'])} ({_e(latest['kind'])} {evidence_link}{sha})"
            rows.append(f'<li class="ev-{_e(c["status"])}"{_attr(c["handle"])}><span class="h">{_e(c["handle"])}</span> '
                        f'{markdown.inline(c["text"])}{version} <span class="ev">{status}</span></li>')
        return '<ol class="criteria">\n' + "\n".join(rows) + "\n</ol>"


def _program_table(child):
    """The one program table a direct child appears in: the first that matches."""
    if child["kind"] == "project":
        return "projects"
    if child["form"] == "decision" and child["status"] == "open":
        return "open-decisions"
    if child["form"] == "decision" and child["resolution"] == "done":
        return "decisions-so-far"
    if child["status"] == "closed" and child["resolution"] == "out_of_scope":
        return "out-of-scope"
    return "other-children"


def _alternatives(raw):
    try:
        listed = json.loads(raw or "[]")
    except ValueError:
        return [raw]
    return [str(entry) for entry in listed] if isinstance(listed, list) else [str(listed)]


def _list_items(body):
    """Top-level list items in a section (a fog patch each): lines whose marker starts the line."""
    if not body:
        return 0
    return sum(1 for line in body.splitlines() if markdown.TOP_LEVEL_ITEM.match(line))


CSS = """
:root{--bg:#fbfaf8;--fg:#1c1b19;--muted:#6b6862;--line:#e3e0da;--card:#ffffff;--accent:#2f5bd3;
--ok:#1f7a45;--warn:#a5600a;--bad:#b42318;--pill:#eef1fb}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ecebe8;--muted:#a19e98;--line:#2e2d2b;--card:#1c1c1b;
--accent:#86a6ff;--ok:#5cc98a;--warn:#e2a54a;--bad:#ff7b6e;--pill:#232838}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.board{max-width:980px;margin:0 auto;padding:16px}
a{color:var(--accent)}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.9em}
pre{overflow-x:auto;background:var(--card);border:1px solid var(--line);padding:8px;border-radius:6px}
h1{font-size:1.5rem;margin:.2rem 0 .5rem}
h2{font-size:1.1rem;margin:0;display:inline}
.head{border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:16px}
.crumbs{color:var(--muted);font-size:.9rem}
.crumbs span{margin:0 .3rem}
.pill,.flag,.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:.8rem;background:var(--pill)}
.flag.blocked,.flag.urgent{color:var(--bad)}
.flag.progress{color:var(--warn)}
.flag.closed{color:var(--muted)}
.facts{display:flex;flex-wrap:wrap;gap:4px 20px;margin:0}
.facts div{display:flex;gap:6px}
.facts dt{color:var(--muted)}
.facts dd{margin:0}
.outcome{color:var(--muted)}
.handoff{font-size:.9rem;color:var(--muted)}
.tiles{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px;min-width:110px}
.tile .n,.funnel .n{display:block;font-size:1.3rem;font-weight:600}
.tile .l,.funnel .l,.pipeline .l{color:var(--muted);font-size:.85rem}
.funnel,.pipeline{display:flex;flex-wrap:wrap;gap:6px;list-style:none;padding:0;margin:0}
.funnel li,.pipeline li{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
.pipeline li.reached{border-color:var(--ok)}
.pipeline li.current{border-color:var(--accent);border-width:2px}
.pipeline li.ahead{opacity:.6}
.ev{display:block;font-size:.8rem;color:var(--muted)}
.waiting{margin:0 0 20px}
.waiting h2{display:block;margin-bottom:8px}
.waiting .clear{color:var(--ok);font-weight:600;margin:0}
.ask{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:8px;
padding:8px 12px;margin:0 0 8px}
.ask.urgent{border-left-color:var(--bad)}
.ask .q{margin:0 0 4px}
.ask .question>:first-child{margin-top:0}
.ask .reply{margin:0 0 4px;font-size:.9rem}
.ask .facts{font-size:.88rem}
.ask .cta{margin:4px 0 0;font-size:.85rem;color:var(--muted)}
.rollup,.section{margin:0 0 18px}
summary{cursor:pointer;margin-bottom:6px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.92rem}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}
tr.closed{color:var(--muted)}
tr.retracted td,li.retired{text-decoration:line-through;color:var(--muted)}
.none{color:var(--muted);margin:4px 0}
meter{width:70px}
.criteria li{margin:3px 0}
.criteria .ev{display:inline}
.criteria li.ev-pass .ev{color:var(--ok)}
.criteria li.ev-fail .ev{color:var(--bad)}
.criteria .h{color:var(--muted);font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.85em}
blockquote{margin:0;padding-left:12px;border-left:3px solid var(--line);color:var(--muted)}
""".strip()
