"""Every reference on a page links in the artifact viewer's documented forms: an off-site link opens in a new tab
(`target="_blank" rel="noopener"`), a bare PR number and a sha link to the repo's GitHub remote, a ticket's header
shows its head PR and sha (or `local branch … no PR`), and a section is `<full page URL>#<handle>` (never a bare
`#handle`, even on the page itself). How a click opens inside the viewer's iframe, and whether a bare `#handle`
works there, are untested; no page uses either."""

import re
import sqlite3

import pytest

from gated import DEST, accept, commit, git, ticket, walk_to_verified

SPEC_URL = "https://artifacts.example/a/proj"
TICKET_URL = "https://artifacts.example/a/tkt"


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, result.err
    return result.out.strip()


def page(trk, item_id):
    result = trk("pages", "render", item_id, "--out", "-")
    assert result.code == 0, result.err
    return result.out


def make(trk, trk_home, tmp_path, remote):
    """A checkout (with `origin` when `remote`), project J on it, ticket T Built there with a bare-number
    `pr` ref, an open ask on J, and both pages published. Returns the ids and the head sha."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, ".trk/blast-radius", "# no rules: every change rates green\n")
    git(repo, "checkout", "-q", "-b", "feature")  # main is where the PR merges into
    sha = commit(repo, "work")
    if remote:
        git(repo, "remote", "add", "origin", remote)
    ok(trk, "init", "--me", "michael")
    ok(trk, "actor", "add", "worker", "--kind", "agent")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", str(repo))
    accept(trk, j)
    t = ok(trk, "new", "work-item", "T", "--form", "spec-bound", "--parent", j, *ticket("spec-bound"))
    ref = "1559" if remote else "tr-demo"
    ok(trk, "evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", ref, "--sha", sha, "--note", "PR",
       "--as", "worker")
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "run", "--sha", sha,
       "--verdict", "pass", "--note", "green", "--as", "worker")
    ok(trk, "advance", t, "--as", "worker")
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "verified", "--criterion", "ac-1", "--ref", "run-2",
       "--sha", sha, "--verdict", "pass", "--note", "holds", "--as", "worker")
    conn = sqlite3.connect(trk_home / "store.db")
    with conn:
        conn.execute("INSERT INTO pages (item_id, slug, url, version, state) VALUES (?, 'pj', ?, 1, 'live')",
                     (j, SPEC_URL))
        conn.execute("INSERT INTO pages (item_id, slug, url, version, state) VALUES (?, 'pt', ?, 1, 'live')",
                     (t, TICKET_URL))
    conn.close()
    return {"P": p, "J": j, "T": t, "sha": sha, "repo": str(repo)}


NEW_TAB = 'target="_blank" rel="noopener"'


def test_a_bare_pr_number_and_a_sha_link_to_the_github_remote_in_a_new_tab(trk, trk_home, tmp_path):
    w = make(trk, trk_home, tmp_path, "git@github.com:o/r.git")
    header = page(trk, w["T"]).split('data-comment-id="tiles"')[0]
    assert f'<a href="https://github.com/o/r/pull/1559" {NEW_TAB}>#1559</a>' in header
    assert f'<a href="https://github.com/o/r/commit/{w["sha"]}" {NEW_TAB}><code>{w["sha"][:7]}</code></a>' in header
    row = re.search(rf'<tr data-comment-id="child-{w["T"]}">.*?</tr>', page(trk, w["J"])).group(0)
    assert f'<a href="https://github.com/o/r/pull/1559" {NEW_TAB}>#1559</a>' in row
    evidence_row = re.search(r'<tr[^>]*data-comment-id="evidence-[^"]*">.*?</tr>', page(trk, w["T"])).group(0)
    assert f"https://github.com/o/r/commit/{w['sha']}" in evidence_row


def test_https_remotes_work_too(trk, trk_home, tmp_path):
    w = make(trk, trk_home, tmp_path, "https://github.com/o/r.git")
    assert "https://github.com/o/r/pull/1559" in page(trk, w["T"])


def test_a_repo_with_no_remote_says_local_branch_no_pr_and_links_no_commit(trk, trk_home, tmp_path):
    w = make(trk, trk_home, tmp_path, None)
    header = page(trk, w["T"]).split('data-comment-id="tiles"')[0]
    assert f"local branch tr-demo at <code>{w['sha'][:7]}</code>, no PR" in header
    assert "/commit/" not in header and "github.com" not in header


def test_a_criterion_and_an_evidence_row_link_to_their_handle_on_the_full_page_url(trk, trk_home, tmp_path):
    w = make(trk, trk_home, tmp_path, None)
    html = page(trk, w["T"])
    assert f'<a href="{TICKET_URL}#ac-1">' in html
    done = ok(trk, "new", "work-item", "Done", "--form", "spec-bound", "--parent", w["J"], *ticket("spec-bound"))
    walk_to_verified(lambda *argv: ok(trk, *argv), done, repo=w["repo"], sha=w["sha"])
    conn = sqlite3.connect(trk_home / "store.db")
    with conn:
        conn.execute("INSERT INTO pages (item_id, slug, url, version, state) VALUES (?, 'pd', ?, 1, 'live')",
                     (done, "https://artifacts.example/a/done"))
    conn.close()
    finished = page(trk, done)
    assert re.search(r'<a href="https://artifacts\.example/a/done#evidence-[a-z0-9]+">[a-z0-9]+</a>', finished)
    assert 'href="#' not in finished
    assert 'href="#' not in html  # never a bare same-page handle


def test_ask_boxes_and_needs_you_rows_link_to_the_ask_box_on_the_full_page_url(trk, trk_home, tmp_path):
    w = make(trk, trk_home, tmp_path, None)
    question = tmp_path / "q.md"
    question.write_text("Which one?\n")
    ask = ok(trk, "ask", w["J"], "--to", "engineering", "Which way?", "--question", f"@{question}", "--as", "worker")
    box = page(trk, w["J"])
    assert f'<a href="{SPEC_URL}#ask-{ask}">Which way?</a>' in box
    needs = page(trk, "needs-you:michael")
    assert f'<a href="{SPEC_URL}#ask-{ask}">Which way?</a>' in needs
    assert 'href="#' not in box and 'href="#' not in needs


def test_ledger_references_link_to_their_row_on_the_full_page_url(trk, trk_home, tmp_path):
    w = make(trk, trk_home, tmp_path, None)
    ok(trk, "ledger", "add", w["T"], "--kind", "fork", "--decided", "Kept the cache", "--alternative", "Dropped it",
       "--why", "Reads are hot", "--as", "worker")
    ok(trk, "ledger", "add", w["T"], "--kind", "fork", "--decided", "Corrects ledger-1: dropped it after all",
       "--alternative", "Kept it", "--why", "See ledger-1", "--refers-to", "1", "--as", "worker")
    question = tmp_path / "q.md"
    question.write_text("Does ledger-1 hold?\n")
    ask = ok(trk, "ask", w["T"], "--to", "engineering", "--ledger", "1", "Which way?", "--question", f"@{question}",
             "--as", "worker")
    link = f'<a href="{TICKET_URL}#ledger-1">ledger-1</a>'
    ticket_page = page(trk, w["T"])
    review = ticket_page[ticket_page.index('data-comment-id="review"'):]
    assert review.count(link) >= 3  # the decided and why mentions and the "corrects" cell
    assert f'<a href="{TICKET_URL}#ask-{ask}">Which way?</a>' in review  # the open ask tied to row 1
    box = ticket_page[ticket_page.index(f'data-comment-id="ask-{ask}"'):]
    assert f"<dt>Ledger</dt><dd>{link}</dd>" in box and f"Does {link} hold?" in box
    assert 'href="#' not in ticket_page
    other = page(trk, "needs-you:michael")
    assert link in other  # the Needs-you page links the same handle on the ticket's page


def test_links_use_the_remote_of_the_repo_that_holds_the_head_sha(trk, trk_home, tmp_path):
    w = make(trk, trk_home, tmp_path, "git@github.com:o/first.git")
    second = tmp_path / "second"
    second.mkdir()
    git(second, "init", "-q", "-b", "main")
    git(second, "remote", "add", "origin", "git@github.com:o/second.git")
    sha = commit(second, "work-b")
    ok(trk, "edit", w["T"], "--add-repo", str(second))
    ok(trk, "evidence", "add", w["T"], "--kind", "pr", "--gate", "built", "--ref", "77", "--sha", sha, "--note", "PR",
       "--as", "worker")
    ok(trk, "evidence", "add", w["T"], "--kind", "test-run", "--gate", "built", "--ref", "run-b", "--sha", sha,
       "--verdict", "pass", "--note", "green", "--as", "worker")
    header = page(trk, w["T"]).split('data-comment-id="tiles"')[0]
    assert "https://github.com/o/second/pull/77" in header
    assert f"https://github.com/o/second/commit/{sha}" in header
    assert "o/first" not in header
    row = re.search(rf'<tr data-comment-id="child-{w["T"]}">.*?</tr>', page(trk, w["J"])).group(0)
    assert "https://github.com/o/second/pull/77" in row
