"""`trk pages setup` and `trk pages` (Page publisher spec: Setup, token and privacy; Reads and verbs),
against the in-process fake page backend. No real backend is ever called."""

import json
import sqlite3

import pytest

from fake_backend import BASE_URL, EXPIRES, TOKEN, FakeBackend
from gated import DEST, section_file, ticket
from trk import adapter


@pytest.fixture
def fake(monkeypatch):
    """The fake adapter behind every client the CLI builds; backoff sleeps are recorded, not slept."""
    service = FakeBackend()
    monkeypatch.setattr(adapter, "_runner", service.runner())
    return service


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "tracker-token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def tree(trk, monkeypatch):
    """The acceptance criteria's fresh store: P over J (draft) over decision D (grilling) and ticket T."""
    monkeypatch.setenv("TRK_NOW", "2026-10-11T09:00:00Z")

    def ok(*argv):
        result = trk(*argv)
        assert result.code == 0, (argv, result.err)
        return result.out.strip()

    ok("init", "--me", "michael")
    ok("actor", "add", "kim", "--kind", "human")
    ok("roles", "set", "product", "kim")
    p = ok("new", "program", "P", *DEST)
    j = ok("new", "project", "J", "--parent", p)
    d = ok("new", "work-item", "D", "--form", "decision", "--subtype", "grilling", "--parent", j)
    t = ok("new", "work-item", "T", "--form", "spec-bound", "--parent", j, *ticket("spec-bound"))
    return {"P": p, "J": j, "D": d, "T": t}


ME = "needs-you:michael"  # the default owner's Needs-you page, which every published store has


def command(token_file):
    return f"fake-adapter --token-file {token_file}"


def setup_argv(token_file, home, *extra):
    return ("pages", "setup", "--adapter", command(token_file), "--home", home, *extra)


def journal(trk_home, since=0):
    conn = sqlite3.connect(trk_home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM journal WHERE seq > ? ORDER BY seq", (since,)).fetchall()
    finally:
        conn.close()


def meta(trk_home, key):
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def head(trk_home):
    return journal(trk_home)[-1]["seq"]


# ----- AC 1: off until set up, the token file's mode, config rows, the secret ------------------------


@pytest.mark.parametrize("verb", ["flush", "pull", "tick"])
def test_publishing_verbs_refuse_before_setup(trk, tree, fake, verb):
    result = trk("pages", verb, "--json")
    assert result.code == 1
    assert result.error()["rule"] == "publishing-not-set-up"
    assert fake.requests == []


def test_pages_says_not_set_up_before_setup(trk, tree, fake):
    result = trk("pages")
    assert result.code == 0
    assert "not set up" in result.out
    assert trk("pages", "--json").json() == {"schema": 1, "set_up": False, "pages": [], "failed_replies": []}


def test_a_valid_setup_journals_one_config_row_per_key(trk, trk_home, tree, fake, token_file):
    before = head(trk_home)
    result = trk(*setup_argv(token_file, tree["P"]))
    assert result.code == 0, result.err
    rows = journal(trk_home, before)
    assert [row["change"] for row in rows] == ["config.changed"] * 3
    assert [json.loads(row["data"]) for row in rows] == [
        {"key": "publish-adapter", "old": None, "new": command(token_file)},
        {"key": "publish-expires", "old": None, "new": EXPIRES},
        {"key": "publish-home", "old": None, "new": tree["P"]},
    ]  # the team is already `tracker` by default: unchanged, so no row
    assert {row["actor"] for row in rows} == {"michael"}
    assert meta(trk_home, "publish_team") == "tracker"


def test_a_new_team_is_journaled_and_rerunning_setup_unchanged_journals_nothing(trk, trk_home, tree, fake, token_file):
    assert trk(*setup_argv(token_file, tree["P"], "--team", "scratch")).code == 0
    assert {"key": "publish-team", "old": "tracker", "new": "scratch"} in \
        [json.loads(row["data"]) for row in journal(trk_home)]
    before = head(trk_home)
    assert trk(*setup_argv(token_file, tree["P"], "--team", "scratch")).code == 0
    assert head(trk_home) == before
    assert len(fake.tool_calls("check")) == 2  # the token is checked every time


def test_the_secret_appears_in_no_output_and_nowhere_in_the_store(trk, trk_home, tree, fake, token_file):
    outputs = [trk(*setup_argv(token_file, tree["P"])), trk(*setup_argv(token_file, tree["P"]), "--json"),
               trk("pages"), trk("pages", "--json"), trk("changes", "--since", "0"), trk("config", "--json"),
               trk("show", tree["T"], "--json")]
    for result in outputs:
        assert result.code == 0, result.err
        assert TOKEN not in result.out + result.err
    for path in trk_home.iterdir():
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path.name
    conn = sqlite3.connect(trk_home / "store.db")
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert TOKEN not in dump


# ----- AC 2: the live check, the home item, the cursor --------------------------------------------


def test_setup_checks_the_token_with_one_guidance_call(trk, tree, fake, token_file):
    assert trk(*setup_argv(token_file, tree["P"])).code == 0
    assert fake.calls == [("check", {})]


def test_a_failed_token_check_exits_1_and_stores_nothing(trk, trk_home, tree, fake, token_file):
    token_file.write_text("tok_revoked\n")
    before = head(trk_home)
    result = trk(*setup_argv(token_file, tree["P"]), "--json")
    assert result.code == 1
    assert result.error()["rule"] == "adapter-check-failed"
    assert "401" in result.error()["message"] and "exit 2" in result.error()["message"]
    assert "tok_revoked" not in result.out + result.err
    assert head(trk_home) == before
    assert meta(trk_home, "publish_adapter") is None
    assert trk("pages", "flush").code == 1  # still off


def test_an_unreachable_service_fails_the_check_after_its_retries(trk, tree, fake, token_file):
    fake.fail_next("502", tool="check", times=4)
    result = trk(*setup_argv(token_file, tree["P"]), "--json")
    assert result.code == 1
    assert result.error()["rule"] == "adapter-check-failed"
    assert fake.sleeps == [5, 20, 60]


def test_a_closed_home_is_refused(trk, tree, fake, token_file):
    assert trk("close", tree["D"], "--resolution", "wont-do", "--outcome", "Dropped.").code == 0
    result = trk(*setup_argv(token_file, tree["D"]), "--json")
    assert result.code == 1
    assert result.error()["rule"] == "home-closed"
    assert fake.requests == []


def test_a_missing_home_exits_3(trk, tree, fake, token_file):
    result = trk(*setup_argv(token_file, "tr-zzzzz"), "--json")
    assert result.code == 3
    assert fake.requests == []


def test_setup_sets_the_publish_cursor_to_the_journal_head(trk, trk_home, tree, fake, token_file):
    assert trk(*setup_argv(token_file, tree["P"])).code == 0
    assert meta(trk_home, "publish_cursor") == str(head(trk_home))


def test_rerunning_setup_keeps_the_cursor_it_already_has(trk, trk_home, tree, fake, token_file):
    """A renewal must not skip rows the publisher has not marked yet: only the first setup sets it."""
    assert trk(*setup_argv(token_file, tree["P"])).code == 0
    cursor = meta(trk_home, "publish_cursor")
    assert trk("edit", tree["T"], "--title", "T renamed").code == 0
    assert trk(*setup_argv(token_file, tree["J"])).code == 0
    assert meta(trk_home, "publish_cursor") == cursor


def test_an_empty_team_is_a_usage_error(trk, tree, fake, token_file):
    result = trk(*setup_argv(token_file, tree["P"], "--team", ""))
    assert result.code == 2 and "team" in result.err
    assert fake.requests == []


@pytest.mark.parametrize("bad", ["", "  ", "fake-adapter 'unclosed"])
def test_a_bad_adapter_command_is_a_usage_error(trk, tree, fake, bad):
    result = trk("pages", "setup", "--adapter", bad, "--home", tree["P"])
    assert result.code == 2 and "--adapter" in result.err
    assert fake.requests == []


def test_a_missing_token_file_holds_the_adapter_and_fails_the_check(trk, tree, fake, tmp_path):
    result = trk(*setup_argv(tmp_path / "nope", tree["P"]), "--json")
    assert result.code == 1 and result.error()["rule"] == "adapter-check-failed"
    assert "cannot read the token file" in result.error()["message"]
    assert fake.requests == []


def test_an_expiry_that_is_no_date_fails_the_check(trk, tree, fake, token_file):
    fake.expires = "2027-13-01"
    result = trk(*setup_argv(token_file, tree["P"]), "--json")
    assert result.code == 1 and result.error()["rule"] == "adapter-check-failed"
    assert "YYYY-MM-DD" in result.error()["message"]


def test_an_adapter_of_another_protocol_is_refused(trk, tree, fake, token_file, monkeypatch):
    real = adapter._runner

    def other(argv, request):
        code, out, err = real(argv, request)
        if argv[-1] == "describe":
            out = json.dumps({**json.loads(out), "protocol": 2}).encode()
        return code, out, err

    monkeypatch.setattr(adapter, "_runner", other)
    result = trk(*setup_argv(token_file, tree["P"]), "--json")
    assert result.code == 1 and "protocol 2" in result.error()["message"]


def test_every_request_carries_the_protocol_and_no_secret(tree, fake, token_file):
    seen, real = [], adapter._runner

    def spying(argv, request):
        seen.append((argv, json.loads(request)))
        return real(argv, request)

    client = adapter.Client(command(token_file), runner=spying)
    client.check()
    client.publish(title="T", body="<p>x</p>", team="tracker", tags=["trk:tr-x"])
    assert [argv[-1] for argv, _ in seen] == ["describe", "check", "publish"]
    assert all(request["protocol"] == adapter.PROTOCOL for _, request in seen)
    assert TOKEN not in json.dumps(seen)


# ----- AC 4: the page list ------------------------------------------------------------------------


def test_pages_lists_every_page_bearing_item_dirty_until_published(trk, tree, fake, token_file):
    question = "@" + str(section_file("question", "Which units?\n"))
    assert trk("ask", tree["D"], "--to", "product", "Q?", "--question", question, "--as", "worker").code == 0
    assert trk(*setup_argv(token_file, tree["P"])).code == 0
    listing = trk("pages", "--json").json()
    assert listing["schema"] == 1
    assert listing["set_up"] is True
    assert [(row["item"], row["kind"], row["title"]) for row in listing["pages"]] == [
        (tree["P"], "program", "P"), (tree["J"], "project", "J"), (tree["D"], "decision", "D"),
        (tree["T"], "ticket", "T"), ("needs-you:kim", "needs-you", "Needs you: kim"),
        (ME, "needs-you", "Needs you: michael"), ("top:", "top", "All work")]
    for row in listing["pages"]:
        assert row == {"item": row["item"], "kind": row["kind"], "title": row["title"], "slug": None, "url": None,
                       "version": None, "published_at": None, "dirty": True, "due_at": None, "last_error": None}


def test_pages_shows_the_bookkeeping_and_filters_dirty_and_errors(trk, trk_home, tree, fake, token_file):
    assert trk(*setup_argv(token_file, tree["P"])).code == 0
    conn = sqlite3.connect(trk_home / "store.db")
    with conn:
        conn.execute("INSERT INTO pages (item_id, slug, url, version, state, published_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (tree["P"], "fake-0001", f"{BASE_URL}/artifacts/fake-0001", 3, "live", "2026-10-11T09:10:00.000Z"))
        conn.execute("INSERT INTO pages (item_id, slug, url, version, state, published_at, due_at, first_dirty_at,"
                     " last_error, last_error_at) VALUES (?, ?, ?, ?, 'live', ?, ?, ?, ?, ?)",
                     (tree["J"], "fake-0002", f"{BASE_URL}/artifacts/fake-0002", 1, "2026-10-11T09:10:00.000Z",
                      "2026-10-11T09:12:00.000Z", "2026-10-11T09:11:00.000Z", "update: tool_not_allowed "
                      "(update)", "2026-10-11T09:12:00.000Z"))
    conn.close()
    rows = {row["item"]: row for row in trk("pages", "--json").json()["pages"]}
    assert rows[tree["P"]] == {"item": tree["P"], "kind": "program", "title": "P", "slug": "fake-0001",
                               "url": f"{BASE_URL}/artifacts/fake-0001", "version": 3,
                               "published_at": "2026-10-11T09:10:00.000Z", "dirty": False, "due_at": None,
                               "last_error": None}
    assert rows[tree["J"]]["dirty"] is True
    assert rows[tree["J"]]["due_at"] == "2026-10-11T09:12:00.000Z"
    dirty = [row["item"] for row in trk("pages", "--dirty", "--json").json()["pages"]]
    assert dirty == [tree["J"], tree["D"], tree["T"], ME, "top:"]
    errors = [row["item"] for row in trk("pages", "--errors", "--json").json()["pages"]]
    assert errors == [tree["J"]]
    text = trk("pages").out
    assert "fake-0001" in text and "v3" in text and f"{BASE_URL}/artifacts/fake-0001" in text
    assert "2026-10-11T09:10:00.000Z" in text
    assert "tool_not_allowed (update)" in text
    lines = text.strip().splitlines()
    assert len(lines) == 6 and lines[4].startswith(ME) and lines[5].startswith("top:")
    assert lines[0].startswith(tree["P"])


def test_json_before_the_subverb_still_means_json(trk, tree, fake, token_file):
    result = trk("pages", "--json", *setup_argv(token_file, tree["P"])[1:])
    assert result.json()["home"] == tree["P"]
    assert trk("pages", "--json", "pull").json()["comments"] == []
    assert trk("pages", "--dirty", "setup", "--home", tree["P"]).code == 2


def test_pull_after_setup_with_no_page_published_reads_nothing(trk, tree, fake, token_file):
    assert trk(*setup_argv(token_file, tree["P"])).code == 0
    result = trk("pages", "pull", "--json")
    assert result.code == 0, result.err
    assert result.json()["comments"] == [] and fake.tool_calls("list_comments") == []


def test_setup_prints_what_it_configured_without_the_secret(trk, tree, fake, token_file):
    result = trk(*setup_argv(token_file, tree["P"]), "--json")
    assert result.json() == {"schema": 1, "adapter": command(token_file), "name": "fake",
                             "capabilities": ["comments"], "team": "tracker", "expires": EXPIRES, "home": tree["P"],
                             "changed": ["publish-adapter", "publish-expires", "publish-home"], "renewal_ask": None}
    text = trk(*setup_argv(token_file, tree["P"])).out
    assert "fake (comments)" in text and "tracker" in text and EXPIRES in text


def test_setup_is_the_default_owners_or_anyones(trk, trk_home, tree, fake, token_file):
    assert trk(*setup_argv(token_file, tree["P"]), "--as", "worker").code == 0
    assert {row["actor"] for row in journal(trk_home) if row["change"] == "config.changed"
            and json.loads(row["data"])["key"].startswith("publish-")} == {"worker"}
