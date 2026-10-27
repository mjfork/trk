"""`trk pages render`: the Board page of one item, rendered locally from the store (spec 04: Page template,
Which items get a page, Handle rules, Links, Modules and runtime).

The golden renders come from one fixture store built through the CLI, with item ids drawn from a seeded
generator so the bytes are stable. Regenerate them with TRK_UPDATE_GOLDEN=1 only for an intended
template change (and bump render.RENDER_VERSION with it).
"""

import json
import os
import random
import re
import sqlite3
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from gated import DEST, section_file, walk_to_verified
from trk import evidence, ids, markdown, render

GOLDEN = Path(__file__).parent / "golden" / "pages"
SRC = Path(__file__).resolve().parents[1] / "src"


# --- the fixture store ------------------------------------------------------------------------


@pytest.fixture
def seeded_ids(monkeypatch):
    """Item and evidence ids from seeded generators, so the same steps make the same ids."""
    draw = random.Random(4).choice
    real = ids.new_id
    monkeypatch.setattr(ids, "new_id", lambda prefix, taken: real(prefix, taken, draw=draw))
    monkeypatch.setattr(evidence, "secrets", SimpleNamespace(choice=random.Random(5).choice))


class World:
    def __init__(self, trk, monkeypatch):
        self.trk, self.monkeypatch, self.minute = trk, monkeypatch, 0

    def at(self, minute):
        self.monkeypatch.setenv("TRK_NOW", f"2026-10-11T{9 + minute // 60:02d}:{minute % 60:02d}:00Z")

    def ok(self, *argv):
        self.minute += 1
        self.at(self.minute)
        result = self.trk(*argv)
        assert result.code == 0, (argv, result.err)
        return result.out.strip()

    def tick(self):
        self.minute += 1
        self.at(self.minute)

    def same_minute(self, *argv):
        """`ok` at the minute the last `tick` set: a ticket's walk to Verified takes one minute, however many steps."""
        result = self.trk(*argv)
        assert result.code == 0, (argv, result.err)
        return result.out.strip()

    def file(self, name, body):
        return "@" + str(section_file(name, body))


NOTES = """## Where we are

Pages are *rendered* from the store, **never** by hand. See [the spec](https://example.com/spec?a=1&b=2),
mail <mailto:team@example.com>, and `trk pages render`.

- first
- second, with a [bad link](javascript:alert(1))
  - nested

| Page | Kind |
|---|---|
| P | program |

<script>alert("raw html")</script>

```
<b>code stays text</b>
```
"""


@pytest.fixture
def world(trk, monkeypatch, seeded_ids):
    """Program P over project J (accepted) and the children that land in every rollup table:
    J holds spec-bound T (claimed, urgent, blocked, with a handoff and JIRA link), self-contained S,
    closed ticket C and decision D with an open ask; P holds decision X closed done, ticket Q closed
    out of scope, an open ticket O, an ask A and a project K closed out of scope."""
    w = World(trk, monkeypatch)
    w.at(0)
    trk("init", "--me", "michael", "--display", "Michael Fork")
    w.ok("actor", "add", "kim", "--kind", "human", "--display", "Kim")
    w.tick()  # `trk init` registers `worker` now; the clock keeps the timeline the goldens were recorded at
    w.ok("roles", "set", "product", "kim")
    p = w.ok("new", "program", "Tracker v1", "--criterion", "Pages replace the boards",
             "--criterion", "Asks are answered by comment", "--section", f"notes={w.file('notes', NOTES)}",
             "--section", f"not-yet-specified={w.file('fog', '- page lifecycle\n- audience views\n  - nested\n')}",
             "--section", f"out-of-scope={w.file('oos', 'A written out-of-scope on a program.\n')}")
    w.ok("criterion", "add", p, "Every handle is stable")
    w.ok("criterion", "retire", p, "ac-2", "--reason", "moved to the comments project")
    j = w.ok("new", "project", "Page publisher", "--parent", p,
             "--section", f"problem={w.file('problem', 'Boards drift.\n')}",
             "--section", f"out-of-scope={w.file('joos', 'Images.\n')}", "--criterion", "Pages publish",
             *(arg for handle in ("solution", "user-stories", "implementation-decisions", "testing-decisions")
               for arg in ("--section", f"{handle}={w.file(handle, f'The {handle}.\n')}")))
    judged = trk("evidence", "add", j, "--kind", "judgment", "--gate", "accept", "--note", "The spec is complete.")
    assert judged.code == 0, judged.err  # at J's minute, so every later time stays as it was
    w.ok("spec", "accept", j)
    t = w.ok("new", "work-item", "Render a page", "--form", "spec-bound", "--parent", j, "--urgent",
             "--jira", "https://example.atlassian.net/browse/PROJ-1",
             "--section", f"what-to-build={w.file('what', 'Render **one** page.\n')}",
             "--section", f"tiles={w.file('tiles', 'A section named like a template handle.\n')}",
             "--section", f"s-extra={w.file('sextra', 'Already s-prefixed.\n')}",
             "--criterion", "It renders", "--criterion", "It is `byte-identical`")
    s = w.ok("new", "work-item", "Fix the board", "--form", "self-contained", "--parent", j,
             "--section", f"current-behaviour={w.file('cur', 'Drifts.\n')}",
             "--section", f"desired-behaviour={w.file('des', 'Does not.\n')}", "--criterion", "No drift")
    c = w.ok("new", "work-item", "Old ticket", "--form", "spec-bound", "--parent", j,
             "--section", f"what-to-build={w.file('old', 'Old.\n')}", "--criterion", "Done")
    w.tick()
    walk_to_verified(w.same_minute, c, "Shipped before pages.")
    d = w.ok("new", "work-item", "Which template", "--form", "decision", "--subtype", "grilling", "--parent", j,
             "--section", f"question={w.file('question', 'Board or list?\n')}")
    w.ok("ask", d, "--to", "product", "Board or list?", "--question", w.file("q", "Which one?\n"), "--as", "worker")
    w.ok("claim", t, "--session", "s-1", "--as", "worker")
    w.ok("link", s, "blocks", t)
    w.ok("handoff", "add", t, "--file", str(section_file("handoff", "Rendered the header.\nThen the tiles.\n")),
         "--as", "worker", "--session", "s-1")
    x = w.ok("new", "work-item", "Which store", "--form", "decision", "--subtype", "research", "--parent", p,
             "--section", f"question={w.file('xq', 'SQLite?\n')}")
    w.ok("section", "set", x, "resolution", "--file", str(section_file("xr", "SQLite.\n")))
    w.ok("close", x, "--resolution", "done", "--outcome", "SQLite it is.")
    q = w.ok("new", "work-item", "Dark mode", "--form", "self-contained", "--parent", p,
             "--section", f"current-behaviour={w.file('qc', 'Light.\n')}",
             "--section", f"desired-behaviour={w.file('qd', 'Dark.\n')}", "--criterion", "Dark")
    w.ok("close", q, "--resolution", "out-of-scope", "--outcome", "Pages follow the system theme.")
    o = w.ok("new", "work-item", "Loose ticket", "--form", "spec-bound", "--parent", p,
             "--section", f"what-to-build={w.file('loose', 'Loose.\n')}", "--criterion", "Loose")
    a = w.ok("ask", p, "--to", "parent", "Publish now?", "--question", w.file("pq", "Ready?\n"), "--as", "worker")
    k = w.ok("new", "project", "Hosted app", "--parent", p)
    w.ok("close", k, "--resolution", "out-of-scope", "--outcome", "No hosted app in v1.")
    return {"P": p, "J": j, "T": t, "S": s, "C": c, "D": d, "X": x, "Q": q, "O": o, "A": a, "K": k}


def page(trk, item_id):
    result = trk("pages", "render", item_id, "--out", "-")
    assert result.code == 0, result.err
    return result.out


# --- a parse of the handled elements ----------------------------------------------------------


class Handles(HTMLParser):
    """Every data-comment-id in document order, each with the handles enclosing it; and every
    href, src and <link>."""

    VOID = {"meta", "br", "hr", "img", "input", "link"}

    def __init__(self, text):
        super().__init__()
        self.stack, self.found, self.hrefs, self.srcs, self.links = [], [], [], [], 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link":
            self.links += 1
        if "href" in attrs:
            self.hrefs.append(attrs["href"])
        if "src" in attrs:
            self.srcs.append(attrs["src"])
        handle = attrs.get("data-comment-id")
        if handle is not None:
            self.found.append((handle, [h for _, h in self.stack if h is not None]))
        if tag not in self.VOID:
            self.stack.append((tag, handle))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    @property
    def handles(self):
        return [handle for handle, _ in self.found]

    def top_level(self):
        return [handle for handle, outer in self.found if not outer]


def child_ids(trk, item_id):
    return [c["id"] for c in trk("show", item_id, "--json").json()["item"]["children"]]


# --- golden renders and the invariants every page keeps ---------------------------------------

KINDS = {"program": "P", "project": "J", "ticket": "T", "decision": "D"}


@pytest.mark.parametrize("kind", list(KINDS))
def test_golden_render_per_page_kind(trk, world, kind):
    html = page(trk, world[KINDS[kind]])
    golden = GOLDEN / f"{kind}.html"
    if os.environ.get("TRK_UPDATE_GOLDEN") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(html)
    assert html == golden.read_text()
    assert page(trk, world[KINDS[kind]]) == html  # rendering twice gives the same bytes


@pytest.mark.parametrize("kind", list(KINDS))
def test_every_page_is_self_contained_with_unique_handles(trk, world, kind):
    parsed = Handles(page(trk, world[KINDS[kind]]))
    assert parsed.srcs == [] and parsed.links == 0
    assert all(h.startswith(("http://", "https://", "mailto:", "#")) for h in parsed.hrefs), parsed.hrefs
    assert len(parsed.handles) == len(set(parsed.handles))


@pytest.mark.parametrize("kind", list(KINDS))
def test_each_child_appears_in_exactly_one_rollup_table(trk, world, kind):
    item_id = world[KINDS[kind]]
    parsed = Handles(page(trk, item_id))
    rows = [(handle, outer) for handle, outer in parsed.found if handle.startswith("child-")]
    assert sorted(handle for handle, _ in rows) == sorted(f"child-{c}" for c in child_ids(trk, item_id))
    assert all(len(outer) == 1 for _, outer in rows)  # each row sits directly in one table


def test_program_children_land_in_the_first_table_that_matches(trk, world):
    parsed = Handles(page(trk, world["P"]))
    table = {handle[len("child-"):]: outer[0] for handle, outer in parsed.found if handle.startswith("child-")}
    assert table == {world["J"]: "projects", world["K"]: "projects", world["X"]: "decisions-so-far",
                     world["Q"]: "out-of-scope", world["O"]: "other-children", world["A"]: "other-children"}
    assert parsed.top_level()[:9] == ["header", "tiles", "waiting", "in-progress", "projects", "open-decisions",
                                      "decisions-so-far", "out-of-scope", "other-children"]


def test_spec_ac3_the_project_page_holds_the_board_in_order(trk, world):
    first, second = page(trk, world["J"]), page(trk, world["J"])
    assert first == second
    parsed = Handles(first)
    assert parsed.top_level() == ["header", "tiles", "waiting", "in-progress", "tickets", "other-children",
                                  "problem", "solution", "user-stories", "implementation-decisions",
                                  "testing-decisions", "acceptance-criteria", "out-of-scope",
                                  "evidence"]  # the accept judgment
    assert len(parsed.handles) == len(set(parsed.handles))
    assert parsed.srcs == [] and parsed.links == 0
    assert dict(parsed.found)["stage-funnel"] == ["tiles"]
    assert dict(parsed.found)["ac-1"] == ["acceptance-criteria"]


def test_ticket_page_has_the_stage_pipeline_criteria_and_handoff(trk, world):
    html = page(trk, world["T"])
    parsed = Handles(html)
    assert parsed.top_level() == ["header", "tiles", "waiting", "children", "what-to-build", "acceptance-criteria",
                                  "s-tiles", "s-s-extra"]
    found = dict(parsed.found)
    assert found["handoff"] == ["header"]
    assert [h for h, outer in parsed.found if outer == ["tiles", "stage"]] == [
        "stage-framed", "stage-specd", "stage-built", "stage-merged", "stage-verified"]
    assert found["ac-2"] == ["acceptance-criteria"]
    assert "Rendered the header." in html and "Then the tiles." not in html  # the first line only
    assert f"trk handoff show {world['T']}" in html


def test_a_retired_criterion_keeps_its_row_struck_through(trk, world):
    html = page(trk, world["P"])
    assert re.search(r'<li class="retired" data-comment-id="ac-2"><s>Asks are answered by comment</s>', html)
    assert 'data-comment-id="ac-3"' in html


# --- handle rules ----------------------------------------------------------------------------


@pytest.mark.parametrize("handle, kind, expected", [
    ("tiles", "ticket", "s-tiles"), ("stage", "ticket", "s-stage"), ("child-x", "project", "s-child-x"),
    ("ac-1", "ticket", "s-ac-1"), ("s-extra", "ticket", "s-s-extra"), ("ledger-2", "ticket", "s-ledger-2"),
    ("out-of-scope", "program", "s-out-of-scope"), ("out-of-scope", "project", "out-of-scope"),
    ("notes", "program", "notes"), ("resolution", "decision", "resolution"), ("staged", "ticket", "staged"),
])
def test_a_written_section_on_a_reserved_handle_renders_under_s_prefix(handle, kind, expected):
    assert render.page_handle(handle, kind) == expected


def test_reserved_handles_on_the_program_page(trk, world):
    parsed = Handles(page(trk, world["P"]))
    assert parsed.top_level()[9:] == ["destination", "notes", "not-yet-specified", "s-out-of-scope"]


# --- Markdown --------------------------------------------------------------------------------


def test_section_markdown_renders_and_raw_html_is_escaped(trk, world):
    html = page(trk, world["P"])
    notes = html[html.index('data-comment-id="notes"'):html.index('data-comment-id="not-yet-specified"')]
    assert "<h4>Where we are</h4>" in notes  # a section's ## sits two levels below the page's h2
    assert "<em>rendered</em>" in notes and "<strong>never</strong>" in notes
    assert '<a href="https://example.com/spec?a=1&amp;b=2" target="_blank" rel="noopener">the spec</a>' in notes
    assert '<a href="mailto:team@example.com">mailto:team@example.com</a>' in notes
    assert "<code>trk pages render</code>" in notes
    assert "<ul><li>first</li><li>second, with a [bad link](javascript:alert(1))<ul><li>nested</li></ul></li></ul>" in notes
    assert "<thead><tr><th>Page</th><th>Kind</th></tr></thead><tbody><tr><td>P</td><td>program</td></tr>" in notes
    assert "<script>" not in html and "&lt;script&gt;alert(&quot;raw html&quot;)&lt;/script&gt;" in notes
    assert "<pre><code>&lt;b&gt;code stays text&lt;/b&gt;</code></pre>" in notes


@pytest.mark.parametrize("source, expected", [
    ("# One", "<h3>One</h3>"),
    ("###### Six", "<h6>Six</h6>"),
    ("*a* _b_ **c** __d__ ~~e~~", "<p><em>a</em> <em>b</em> <strong>c</strong> <strong>d</strong> <del>e</del></p>"),
    ("snake_case_word", "<p>snake_case_word</p>"),
    ("`<i>` and ``a`b``", "<p><code>&lt;i&gt;</code> and <code>a`b</code></p>"),
    ("1. a\n2. b", "<ol><li>a</li><li>b</li></ol>"),
    ("- a\n1. b", "<ul><li>a</li></ul><ol><li>b</li></ol>"),
    ("[x](HTTPS://a.b)", '<p><a href="HTTPS://a.b" target="_blank" rel="noopener">x</a></p>'),
    ("[x](data:text/html,hi)", "<p>[x](data:text/html,hi)</p>"),
    ("[x](/relative)", "<p>[x](/relative)</p>"),
    ("![alt](https://x/y.png)", '<p><a href="https://x/y.png" target="_blank" rel="noopener">alt</a></p>'),
    ('[x](https://a" onclick="y)', "<p>[x](https://a&quot; onclick=&quot;y)</p>"),
    ("<img src=x onerror=alert(1)>", "<p>&lt;img src=x onerror=alert(1)&gt;</p>"),
    ("> quote", "<blockquote><p>quote</p></blockquote>"),
    ("a | b\n--|--\n1 | 2 \\| 3", '<div class="scroll"><table><thead><tr><th>a</th><th>b</th></tr></thead>'
                                  "<tbody><tr><td>1</td><td>2 | 3</td></tr></tbody></table></div>"),
    ("---", "<hr>"),
    ("# C#\n## Done ##", "<h3>C#</h3><h4>Done</h4>"),
    ("```\n```python\nx\n```", "<pre><code>```python\nx</code></pre>"),
    ("Page: https://a.b/x_y_z (v4)", '<p>Page: <a href="https://a.b/x_y_z" target="_blank" rel="noopener">'
                                      "https://a.b/x_y_z</a> (v4)</p>"),
    ("see https://a.b/c.", '<p>see <a href="https://a.b/c" target="_blank" rel="noopener">https://a.b/c</a>.</p>'),
    ("(https://a.b/c)", '<p>(<a href="https://a.b/c" target="_blank" rel="noopener">https://a.b/c</a>)</p>'),
    ("https://a.b/w_(x)", '<p><a href="https://a.b/w_(x)" target="_blank" rel="noopener">https://a.b/w_(x)</a></p>'),
    ("`https://a.b`", "<p><code>https://a.b</code></p>"),
    ("[l](https://a.b) <https://c.d>", '<p><a href="https://a.b" target="_blank" rel="noopener">l</a> '
                                       '<a href="https://c.d" target="_blank" rel="noopener">https://c.d</a></p>'),
    ("ftp://a.b and javascript:x", "<p>ftp://a.b and javascript:x</p>"),
])
def test_markdown_converter(source, expected):
    assert markdown.to_html(source) == expected


# --- times, claims and links -----------------------------------------------------------------


def test_times_are_absolute_utc_and_the_claim_shows_its_actor_never_its_lease(trk, world, monkeypatch):
    before = page(trk, world["T"])
    lease = trk("claims", "--json").json()["claims"][0]["lease_until"]
    assert lease[:16] not in before.replace(" ", "T") and "ago" not in before
    assert re.search(r"<dt>Claimed by</dt><dd>worker</dd>", before)
    assert re.search(r"<dt>Updated</dt><dd>2026-10-11 \d\d:\d\dZ</dd>", before)
    monkeypatch.setenv("TRK_NOW", "2026-10-11T09:50:00Z")  # past half the lease: renew writes claim.renewed
    assert trk("claim", "renew", "--session", "s-1", "--as", "worker").code == 0
    assert trk("claims", "--json").json()["claims"][0]["lease_until"] != lease
    assert page(trk, world["T"]) == before


def test_a_link_to_an_unpublished_page_is_plain_text_until_its_page_exists(trk, world, trk_home):
    plain = page(trk, world["P"])
    assert "href" not in plain.split('data-comment-id="tiles"')[0]  # the breadcrumb-free header links nothing
    row = re.search(rf'<tr data-comment-id="child-{world["J"]}">.*?</tr>', plain).group(0)
    assert "<a " not in row and "Page publisher" in row
    conn = sqlite3.connect(trk_home / "store.db")
    with conn:
        conn.execute("INSERT INTO pages (item_id, slug, url, version, state) VALUES (?, 'pp', ?, 1, 'live')",
                     (world["J"], "https://artifacts.example/a/pp"))
        conn.execute("INSERT INTO pages (item_id, slug, url, version, state) VALUES (?, 'xx', ?, 1, 'live')",
                     (world["P"], "javascript:alert(1)"))
        conn.execute("INSERT INTO pages (item_id, state) VALUES (?, 'publishing')", (world["X"],))
    conn.close()
    linked = page(trk, world["P"])
    assert f'<tr data-comment-id="child-{world["J"]}"><td><a href="https://artifacts.example/a/pp">Page publisher</a>' in linked
    assert '<a href="https://artifacts.example/a/pp">' in page(trk, world["T"]).split("</nav>")[0]  # breadcrumb
    assert "javascript:" not in page(trk, world["J"])  # a page url that is not http(s) never becomes a link
    assert re.search(rf'<li data-comment-id="child-{world["X"]}">Which store —', linked)  # publishing: no url yet


# --- evidence, review and the stage cells -----------------------------------------------------


def test_evidence_and_review_render_only_when_their_rows_exist(trk, world, trk_home):
    assert 'data-comment-id="evidence"' not in page(trk, world["T"])
    t = world["T"]

    def evidence(*argv):
        result = trk("evidence", "add", t, *argv, "--as", "worker")
        assert result.code == 0, result.err
        return result.out.strip()

    pr = evidence("--kind", "pr", "--gate", "built", "--ref", "https://github.com/o/r/pull/1", "--sha", "abcdef1234",
                  "--note", "the PR")
    run = evidence("--kind", "test-run", "--gate", "built", "--ref", "run-1", "--sha", "abcdef1234",
                   "--verdict", "pass", "--note", "suite green")
    failed = evidence("--kind", "test-run", "--gate", "built", "--ref", "run-2", "--sha", "abcdef1234",
                      "--verdict", "fail", "--note", "flaky")
    retract = trk("evidence", "retract", t, failed, "--note", "wrong run", "--as", "worker").out.strip()
    # No CLI writes ledger rows yet (Decision ledger's tickets); these stand in for them.
    conn = sqlite3.connect(trk_home / "store.db")
    with conn:
        conn.execute("INSERT INTO ledger (item_id, n, kind, decided, alternatives, why, origin, actor, created_at)"
                     " VALUES (?, 1, 'fork', 'Use *stdlib*', '[\"jinja\"]', 'No deps', 'builder', 'worker',"
                     " '2026-10-11T11:00:00.000Z')", (t,))
        conn.execute("INSERT INTO ledger_verdicts (item_id, n, verdict, note, actor, seq, created_at)"
                     " VALUES (?, 1, 'sound', 'ok', 'kim', 1000, '2026-10-11T11:00:00.000Z')", (t,))
    conn.close()
    html = page(trk, t)
    parsed = Handles(html)
    assert parsed.top_level()[-2:] == ["evidence", "review"]
    assert [h for h, outer in parsed.found if outer == ["evidence"]] == [f"evidence-{e}" for e in (pr, run, failed, retract)]
    assert f'<tr class="retracted" data-comment-id="evidence-{failed}">' in html
    # The Built cell shows its latest counting row: the retracted failure does not count.
    assert re.search(r'data-comment-id="stage-built"><span class="l">Built</span><span class="ev">pass abcdef1</span>', html)
    assert '<tr data-comment-id="ledger-1"><td>fork</td><td><em>stdlib</em>' in html.replace("Use ", "")
    assert "<td>sound</td>" in html
    project = page(trk, world["J"])
    assert '<a href="https://github.com/o/r/pull/1" target="_blank" rel="noopener">https://github.com/o/r/pull/1</a>' in project
    assert 'data-comment-id="review"' not in project  # the ledger is a ticket's


# --- the verb --------------------------------------------------------------------------------


def test_an_ask_has_no_page(trk, world):
    result = trk("pages", "render", world["A"], "--json")
    assert (result.code, result.error()["rule"], result.error()["item"]) == (1, "no-page", world["A"])
    assert "asks have no page" in trk("pages", "render", world["A"]).err


def test_an_unknown_item_exits_3(trk, world):
    assert trk("pages", "render", "tr-zzzzz").code == 3


def test_out_writes_the_page_to_a_file(trk, world, tmp_path):
    out = tmp_path / "p.html"
    result = trk("pages", "render", world["P"].upper(), "--out", str(out), "--json")
    assert result.code == 0, result.err
    assert out.read_text() == page(trk, world["P"])
    assert result.json() == {"schema": 1, "item": world["P"], "render_version": render.RENDER_VERSION,
                             "out": str(out), "bytes": len(out.read_bytes())}
    assert trk("pages", "render", world["P"], "--out", str(out)).out == ""
    assert trk("pages", "render", world["P"]).out == out.read_text()  # stdout by default


def test_claim_renew_imports_none_of_the_page_modules(trk, world):
    probe = ("import sys; from trk.cli import main; code = main(sys.argv[1:]);"
             " print(sorted(m for m in sys.modules if m.startswith('trk.')))")
    env = {**os.environ, "PYTHONPATH": str(SRC), "TRK_NOW": "2026-10-11T09:50:00Z"}
    done = subprocess.run([sys.executable, "-c", probe, "claim", "renew", "--session", "s-1", "--as", "worker"],
                          capture_output=True, text=True, env=env, check=True)
    loaded = json.loads(done.stdout.strip().splitlines()[-1].replace("'", '"'))
    assert "trk.cli" in loaded
    assert not {"trk.render", "trk.markdown", "trk.pages", "trk.publisher", "trk.folder", "trk.flusher",
                 "trk.renewal", "trk.timer"} & set(loaded)


# --- the waiting box ------------------------------------------------------------------------


def waiting_ids(trk, root):
    return [entry["id"] for entry in trk("waiting", "--all", "--root", root, "--json").json()["asks"]]


def ask_boxes(html):
    """Each `ask-<id>` element of the waiting box, by id, as its exact bytes."""
    found = re.findall(r'(<div class="ask[^"]*" data-comment-id="ask-([^"]+)">.*?on this box</p>\n</div>)', html, re.S)
    return {ask: box for box, ask in found}


def test_the_waiting_box_follows_the_tiles_and_lists_the_subtree_asks_as_trk_waiting_orders_them(trk, world):
    urgent = trk("ask", world["O"], "--to", "product", "Ship it?", "--urgent", "--question",
                 "@" + str(section_file("uq", "Today?\n")), "--as", "worker").out.strip()
    html = page(trk, world["P"])
    parsed = Handles(html)
    assert parsed.top_level()[:3] == ["header", "tiles", "waiting"]
    boxed = [h[len("ask-"):] for h, outer in parsed.found if h.startswith("ask-")]
    assert boxed == waiting_ids(trk, world["P"]) and boxed[0] == urgent and world["A"] in boxed
    assert all(outer == ["waiting"] for h, outer in parsed.found if h.startswith("ask-"))
    assert [h[len("ask-"):] for h in Handles(page(trk, world["J"])).handles if h.startswith("ask-")] == \
        waiting_ids(trk, world["J"])


def test_an_ask_box_shows_question_role_addressee_asker_opened_time_and_where_it_sits(trk, world, trk_home):
    (d_ask,) = waiting_ids(trk, world["D"])
    box = ask_boxes(page(trk, world["P"]))[d_ask]
    assert "Board or list?" in box and "Which one?" in box
    assert "product (Kim)" in box and "worker" in box
    opened = trk("show", d_ask, "--json").json()["item"]["created_at"]
    assert render.stamp(opened) in box
    assert "Which template" in box and "<a " not in box  # D's page is not published yet: plain text
    assert "Answer by commenting on this box" in box
    a_box = ask_boxes(page(trk, world["P"]))[world["A"]]
    assert "parent (Michael Fork)" in a_box and "Tracker v1" in a_box
    conn = sqlite3.connect(trk_home / "store.db")
    with conn:
        conn.execute("INSERT INTO pages (item_id, slug, url, version, state) VALUES (?, 'dd', ?, 1, 'live')",
                     (world["D"], "https://artifacts.example/a/dd"))
    conn.close()
    assert '<a href="https://artifacts.example/a/dd">Which template</a>' in ask_boxes(page(trk, world["J"]))[d_ask]


def test_an_empty_waiting_box_says_nothing_is_waiting_and_keeps_its_handle(trk, world):
    html = page(trk, world["T"])
    assert re.search(r'<section class="waiting none" data-comment-id="waiting">\n<h2>Waiting on people</h2>\n'
                     r'<p class="clear">Nothing is waiting on people</p>\n</section>', html)
    assert not [h for h in Handles(html).handles if h.startswith("ask-")]


def test_an_ask_box_is_the_same_element_on_every_ancestor_page(trk, world):
    (d_ask,) = waiting_ids(trk, world["D"])
    boxes = [ask_boxes(page(trk, world[key]))[d_ask] for key in ("D", "J", "P")]
    assert boxes[0] == boxes[1] == boxes[2]
    assert ask_boxes(page(trk, world["P"]))[world["A"]] not in page(trk, world["J"])


def test_an_answered_ask_leaves_every_box_and_a_reopened_one_returns_under_the_same_handle(trk, world, monkeypatch):
    (d_ask,) = waiting_ids(trk, world["D"])
    assert trk("answer", d_ask, "--outcome", "Board", "--answer", "@" + str(section_file("ans", "Board.\n")),
               "--as", "kim").code == 0
    assert f"ask-{d_ask}" not in page(trk, world["J"])
    assert "Nothing is waiting on people" in page(trk, world["D"])
    monkeypatch.setenv("TRK_NOW", "2026-10-11T11:30:00Z")
    assert trk("reopen", d_ask, "--reason", "need units", "--as", "worker").code == 0
    assert "<dt>Opened</dt><dd>2026-10-11 11:30Z</dd>" in ask_boxes(page(trk, world["J"]))[d_ask]  # the reopen


def test_spec_ac4_render_half_an_ask_on_d_shows_the_same_box_on_d_j_and_p(trk, monkeypatch, seeded_ids):
    w = World(trk, monkeypatch)
    w.at(0)
    trk("init", "--me", "michael")
    w.ok("actor", "add", "kim", "--kind", "human")
    w.tick()  # as in `world`: the minute registering `worker` used to take
    w.ok("roles", "set", "product", "kim")
    p = w.ok("new", "program", "P", *DEST)
    j = w.ok("new", "project", "J", "--parent", p)
    d = w.ok("new", "work-item", "D", "--form", "decision", "--subtype", "grilling", "--parent", j)
    t = w.ok("new", "work-item", "T", "--form", "spec-bound", "--parent", j,
             "--section", f"what-to-build={w.file('t', 'T.\n')}", "--criterion", "T works")
    w.ok("claim", d, "--session", "S1", "--as", "worker")
    ask = w.ok("ask", d, "--to", "product", "Q?", "--question", w.file("q", "Q?\n"), "--as", "worker")
    boxes = [ask_boxes(page(trk, item))[ask] for item in (d, j, p)]
    assert boxes[0] == boxes[1] == boxes[2]
    assert "product (kim)" in boxes[0]
    children = dict(Handles(page(trk, d)).found)
    assert children[f"child-{ask}"] == ["children"]
    assert f"ask-{ask}" not in page(trk, t)


# --- Needs-you pages ------------------------------------------------------------------------


PEOPLE = ("kim", "michael")  # kim: the ask to product; michael: the ask to parent on P, and T blocked on his S


@pytest.mark.parametrize("person", PEOPLE)
def test_golden_render_of_a_needs_you_page(trk, world, person):
    html = page(trk, f"needs-you:{person}")
    golden = GOLDEN / f"needs-you-{person}.html"
    if os.environ.get("TRK_UPDATE_GOLDEN") == "1":
        golden.write_text(html)
    assert html == golden.read_text()
    assert page(trk, f"needs-you:{person}") == html
    parsed = Handles(html)
    assert parsed.srcs == [] and parsed.links == 0
    assert all(h.startswith(("http://", "https://", "mailto:", "#")) for h in parsed.hrefs), parsed.hrefs
    assert len(parsed.handles) == len(set(parsed.handles))
    assert parsed.top_level() == ["header", "waiting", "blocked"]


def test_a_needs_you_page_shows_each_ask_in_its_program_pages_box(trk, world):
    boxes = ask_boxes(page(trk, "needs-you:kim"))
    assert list(boxes) == [entry["id"] for entry in trk("waiting", "--me", "kim", "--json").json()["asks"]]
    program = ask_boxes(page(trk, world["P"]))
    assert boxes == {ask: program[ask] for ask in boxes}


LONG_QUESTION = (
    "Should the board show **every** ticket, or only the open ones? Closed tickets crowd the table once a "
    "project has more than twenty, but hiding them loses the record of what shipped.\n\n"
    "Options:\n\n- *Board*: every ticket, closed ones folded\n- *List*: open tickets only\n\n"
    "I recommend **Board**. It unblocks [the spec](https://example.com/spec) and Render a page.\n")


def test_an_ask_box_shows_the_whole_question_as_markdown_on_every_page(trk, world, trk_home):
    ask = trk("ask", world["T"], "--to", "product", "Board or list for tickets?", "--question",
              "@" + str(section_file("long", LONG_QUESTION)), "--as", "worker").out.strip()
    whole = markdown.to_html(LONG_QUESTION)
    assert len(LONG_QUESTION) > 240 and "<li><em>List</em>: open tickets only</li>" in whole
    for where in (world["P"], world["J"], "needs-you:kim"):
        box = ask_boxes(page(trk, where))[ask]
        assert box.startswith(f'<div class="ask" data-comment-id="ask-{ask}">\n'
                              '<p class="q"><strong>Board or list for tickets?</strong></p>\n')
        assert f'<div class="question">{whole}</div>' in box
        assert "…" not in box and box.endswith('<p class="cta">Answer by commenting on this box</p>\n</div>')
    # The feed row, which hx's inbox line quotes, keeps the 240-character excerpt.
    conn = sqlite3.connect(trk_home / "store.db")
    (data,) = conn.execute("SELECT data FROM journal WHERE item = ? AND change = 'ask.opened'", (ask,)).fetchone()
    conn.close()
    excerpt = json.loads(data)["question"]
    assert len(excerpt) == 240 and excerpt.endswith("…") and excerpt.startswith("Should the board show every")


def test_a_needs_you_page_lists_what_is_blocked_on_the_person_in_tree_order(trk, world):
    html = page(trk, "needs-you:michael")
    assert list(ask_boxes(html)) == [world["A"]]
    # P waits on its ask to parent (michael), T on the ticket S michael owns; D waits on kim's ask.
    assert re.findall(r'data-comment-id="blocked-([^"]+)"', html) == [world["P"], world["T"]]
