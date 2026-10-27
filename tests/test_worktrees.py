"""Each item's worktree and archive location (tr-zwb4a). An item records one worktree, `{path, branch, repo,
session}`, with `trk worktree set|clear` or `trk claim --worktree`; `trk where <path>` names the items whose
recorded tree holds a path. `archive` is a path template (`<tree>` for the tree's directory name) that
descendants inherit as `effective_archive`, the way they inherit `client`. Store schema 10."""

import copy
import shutil
import sqlite3
from pathlib import Path

import pytest

from feed_contract import validator
from gated import commit, git, new_flags
from trk import store

FIXTURE_9 = Path(__file__).parent / "fixtures" / "schema9"
OPENED_9 = "2026-10-10T13:00:00.000Z"  # inside the fixture's live lease on T


def ok(result):
    assert result.code == 0, result.err
    return result.json()


@pytest.fixture
def repos(tmp_path):
    """A checkout `main` on branch main and its linked worktree `wt/feat` on branch feat."""
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    commit(main, "README")
    tree = tmp_path / "wt" / "feat"
    git(main, "worktree", "add", "-q", "-b", "feat", str(tree))
    (tree / "src" / "pkg").mkdir(parents=True)
    return {"main": main.resolve(), "tree": tree.resolve()}


@pytest.fixture
def world(trk):
    ok(trk("init", "--me", "michael", "--json"))

    def new(*argv):
        return ok(trk("new", *argv, *new_flags(argv), "--as", "worker", "--json"))["item"]

    program = new("program", "Acme work")
    project = new("project", "Trees", "--parent", program["id"])
    ticket = new("work-item", "Ticket", "--form", "self-contained", "--parent", project["id"])
    other = new("work-item", "Other ticket", "--form", "self-contained", "--parent", program["id"])
    return {"program": program, "project": project, "ticket": ticket, "other": other}


def show(trk, item_id):
    return ok(trk("show", item_id, "--json"))["item"]


def last_change(trk):
    return ok(trk("changes", "--since", "0", "--json"))["changes"][-1]


# ----- ac-1: worktree set / clear / claim --worktree --------------------------------------------------------


def test_set_records_the_realpath_the_branch_from_git_the_repo_and_the_writers_session(trk, world, repos, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(repos["tree"])
    item = ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(link), "--as", "worker",
                  "--session", "s-1", "--json"))["item"]
    assert item["worktree"] == {"path": str(repos["tree"]), "branch": "feat", "repo": str(repos["main"]),
                                "session": "s-1"}
    assert show(trk, world["ticket"]["id"])["worktree"] == item["worktree"]


def test_set_takes_an_explicit_branch(trk, world, repos):
    item = ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(repos["tree"]), "--branch", "other",
                  "--as", "worker", "--json"))["item"]
    assert item["worktree"]["branch"] == "other"


def test_set_takes_the_session_from_the_environment(trk, world, repos, monkeypatch):
    monkeypatch.setenv("TRK_SESSION", "s-env")
    item = ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(repos["tree"]), "--as", "worker",
                  "--json"))["item"]
    assert item["worktree"]["session"] == "s-env"


def test_a_directory_outside_git_records_no_branch_and_no_repo(trk, world, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    item = ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(plain), "--as", "worker",
                  "--json"))["item"]
    assert item["worktree"] == {"path": str(plain.resolve()), "branch": None, "repo": None, "session": None}


def test_a_path_that_is_no_directory_is_a_usage_error(trk, world, tmp_path):
    result = trk("worktree", "set", world["ticket"]["id"], "--path", str(tmp_path / "gone"), "--as", "worker",
                 "--json")
    assert result.code == 2
    assert show(trk, world["ticket"]["id"])["worktree"] is None


def test_set_and_clear_journal_item_updated_with_old_and_new(trk, world, repos):
    ticket = world["ticket"]["id"]
    ok(trk("worktree", "set", ticket, "--path", str(repos["tree"]), "--as", "worker", "--session", "s-1", "--json"))
    recorded = {"path": str(repos["tree"]), "branch": "feat", "repo": str(repos["main"]), "session": "s-1"}
    row = last_change(trk)
    assert (row["change"], row["item"], row["actor"]) == ("item.updated", ticket, "worker")
    assert row["data"] == {"fields": ["worktree"], "old": {"worktree": None}, "new": {"worktree": recorded}}

    ok(trk("worktree", "clear", ticket, "--as", "worker", "--json"))
    row = last_change(trk)
    assert row["data"] == {"fields": ["worktree"], "old": {"worktree": recorded}, "new": {"worktree": None}}
    assert show(trk, ticket)["worktree"] is None


def test_setting_the_same_tree_or_clearing_none_changes_nothing(trk, world, repos):
    ticket = world["ticket"]["id"]
    ok(trk("worktree", "clear", ticket, "--as", "worker", "--json"))
    head = ok(trk("changes", "--since", "0", "--json"))["head"]
    flags = ("--path", str(repos["tree"]), "--as", "worker", "--session", "s-1", "--json")
    ok(trk("worktree", "set", ticket, *flags))
    head = ok(trk("changes", "--since", "0", "--json"))["head"]
    ok(trk("worktree", "set", ticket, *flags))
    assert ok(trk("changes", "--since", "0", "--json"))["head"] == head


def test_a_new_tree_replaces_the_old_one(trk, world, repos):
    ticket = world["ticket"]["id"]
    ok(trk("worktree", "set", ticket, "--path", str(repos["tree"]), "--as", "worker", "--json"))
    item = ok(trk("worktree", "set", ticket, "--path", str(repos["main"]), "--as", "worker", "--json"))["item"]
    assert (item["worktree"]["path"], item["worktree"]["branch"]) == (str(repos["main"]), "main")


def test_claim_worktree_records_the_tree_with_the_claim(trk, world, repos):
    ticket = world["ticket"]["id"]
    claimed = ok(trk("claim", ticket, "--worktree", str(repos["tree"]), "--as", "worker", "--session", "s-c",
                     "--json"))["claimed"]
    assert claimed["claim"]["session"] == "s-c"
    assert claimed["worktree"] == {"path": str(repos["tree"]), "branch": "feat", "repo": str(repos["main"]),
                                   "session": "s-c"}
    kinds = [r["change"] for r in ok(trk("changes", "--since", "0", "--json"))["changes"]][-2:]
    assert kinds == ["claim.taken", "item.updated"]


def test_claim_next_takes_worktree_too(trk, world, repos):
    claimed = ok(trk("claim", "--next", "--root", world["program"]["id"], "--worktree", str(repos["tree"]),
                     "--as", "worker", "--session", "s-c", "--json"))["claimed"]
    assert claimed["worktree"]["path"] == str(repos["tree"])


def test_a_refused_claim_records_no_tree(trk, world, repos):
    ticket = world["ticket"]["id"]
    ok(trk("claim", ticket, "--as", "worker", "--session", "s-a", "--json"))
    taken = trk("claim", ticket, "--worktree", str(repos["tree"]), "--as", "worker", "--session", "s-b", "--json")
    assert taken.code == 4
    assert show(trk, ticket)["worktree"] is None


def test_the_human_view_names_the_tree(trk, world, repos):
    ticket = world["ticket"]["id"]
    ok(trk("worktree", "set", ticket, "--path", str(repos["tree"]), "--as", "worker", "--json"))
    assert f"worktree: {repos['tree']} (feat)" in trk("show", ticket).out
    assert "worktree:" not in trk("show", world["other"]["id"]).out


# ----- ac-2: trk where <path> ------------------------------------------------------------------------------


def test_where_names_the_owning_item_from_any_path_inside_its_tree(trk, world, repos):
    ticket = world["ticket"]["id"]
    ok(trk("claim", ticket, "--worktree", str(repos["tree"]), "--as", "worker", "--session", "s-c", "--json"))
    for path in (repos["tree"], repos["tree"] / "src" / "pkg", repos["tree"] / "src" / "missing.py"):
        reply = ok(trk("where", str(path), "--json"))
        assert reply["tree"] == str(repos["tree"])
        [owner] = reply["items"]
        assert owner["id"] == ticket and owner["title"] == "Ticket"
        assert (owner["root"], owner["root_title"]) == (world["program"]["id"], "Acme work")
        assert (owner["effective_owner"], owner["status"], owner["branch"]) == ("michael", "open", "feat")
        assert owner["claim"]["session"] == "s-c" and owner["claim"]["lease_until"]
        assert owner["worktree"]["path"] == str(repos["tree"])


def test_where_text_names_the_item_root_owner_status_claim_and_branch(trk, world, repos):
    ticket = world["ticket"]["id"]
    ok(trk("claim", ticket, "--worktree", str(repos["tree"]), "--as", "worker", "--session", "s-c", "--json"))
    out = trk("where", str(repos["tree"] / "src")).out
    assert ticket in out and "Ticket" in out and world["program"]["id"] in out
    assert "michael" in out and "open" in out and "s-c" in out and "feat" in out


def test_where_reports_no_claim_when_none_is_live(trk, world, repos):
    ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(repos["tree"]), "--as", "worker", "--json"))
    assert ok(trk("where", str(repos["tree"]), "--json"))["items"][0]["claim"] is None
    assert "unclaimed" in trk("where", str(repos["tree"])).out


def test_where_relative_path_resolves_against_the_current_directory(trk, world, repos, monkeypatch):
    ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(repos["tree"]), "--as", "worker", "--json"))
    monkeypatch.chdir(repos["tree"] / "src")
    assert ok(trk("where", "pkg", "--json"))["items"][0]["id"] == world["ticket"]["id"]


def test_a_sibling_whose_name_extends_the_tree_is_not_inside_it(trk, world, repos):
    ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(repos["tree"]), "--as", "worker", "--json"))
    sibling = repos["tree"].parent / "feat-2"
    sibling.mkdir()
    assert trk("where", str(sibling), "--json").code == 1


def test_an_unrecorded_path_exits_1_not_found_naming_its_git_worktree(trk, world, repos):
    result = trk("where", str(repos["tree"] / "src"), "--json")
    assert result.code == 1
    error = result.error()
    assert (error["code"], error["rule"]) == ("refused", "not-found")
    assert error["worktree"] == str(repos["tree"])
    assert str(repos["tree"]) in error["message"]
    text = trk("where", str(repos["tree"] / "src"))
    assert text.code == 1 and str(repos["tree"]) in text.err


def test_an_unrecorded_path_outside_git_names_no_worktree(trk, world, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    error = trk("where", str(plain), "--json").error()
    assert error["rule"] == "not-found" and error["worktree"] is None


def test_two_items_on_one_path_are_both_listed_newest_first(trk, world, repos, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2026-10-22T10:00:00Z")
    ok(trk("worktree", "set", world["other"]["id"], "--path", str(repos["tree"]), "--as", "worker", "--json"))
    monkeypatch.setenv("TRK_NOW", "2026-10-22T11:00:00Z")
    ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(repos["tree"]), "--as", "worker", "--json"))
    listed = [i["id"] for i in ok(trk("where", str(repos["tree"]), "--json"))["items"]]
    assert listed == [world["ticket"]["id"], world["other"]["id"]]


def test_the_innermost_recorded_tree_answers(trk, world, repos):
    """A tree inside another recorded tree (a worktree under the main checkout's tmp/) answers for paths in it."""
    inner = repos["main"] / "tmp" / "wt" / "inner"
    git(repos["main"], "worktree", "add", "-q", "-b", "inner", str(inner))
    ok(trk("worktree", "set", world["other"]["id"], "--path", str(repos["main"]), "--as", "worker", "--json"))
    ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(inner), "--as", "worker", "--json"))
    reply = ok(trk("where", str(inner / "x.py"), "--json"))
    assert (reply["tree"], [i["id"] for i in reply["items"]]) == (str(inner.resolve()), [world["ticket"]["id"]])
    assert ok(trk("where", str(repos["main"] / "README"), "--json"))["items"][0]["id"] == world["other"]["id"]


def test_where_carries_the_archive_location_so_a_cleanup_needs_no_show(trk, world, repos):
    ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(repos["tree"]), "--as", "worker", "--json"))
    entry = ok(trk("where", str(repos["tree"]), "--json"))["items"][0]
    assert (entry["archive"], entry["effective_archive"]) == (None, None)
    ok(trk("edit", world["program"]["id"], "--archive", "/far/<tree>.tgz", "--as", "worker", "--json"))
    entry = ok(trk("where", str(repos["tree"]), "--json"))["items"][0]
    assert (entry["archive"], entry["effective_archive"]) == (None, "/far/<tree>.tgz")
    ok(trk("edit", world["ticket"]["id"], "--archive", "/near/<tree>/", "--as", "worker", "--json"))
    reply = ok(trk("where", str(repos["tree"]), "--json"))
    assert (reply["items"][0]["archive"], reply["items"][0]["effective_archive"]) == ("/near/<tree>/", "/near/<tree>/")
    assert list(validator("where").iter_errors(reply)) == []
    assert "archive: /near/<tree>/" in trk("where", str(repos["tree"])).out


def test_a_closed_item_still_answers_with_its_status(trk, world, repos):
    ticket = world["ticket"]["id"]
    ok(trk("worktree", "set", ticket, "--path", str(repos["tree"]), "--as", "worker", "--json"))
    ok(trk("close", ticket, "--resolution", "wont-do", "--outcome", "Dropped.", "--as", "worker", "--json"))
    assert ok(trk("where", str(repos["tree"]), "--json"))["items"][0]["status"] == "closed"


def test_a_cleared_tree_no_longer_answers(trk, world, repos):
    ticket = world["ticket"]["id"]
    ok(trk("worktree", "set", ticket, "--path", str(repos["tree"]), "--as", "worker", "--json"))
    ok(trk("worktree", "clear", ticket, "--as", "worker", "--json"))
    assert trk("where", str(repos["tree"]), "--json").code == 1


def test_where_validates_against_its_contract(trk, world, repos):
    ok(trk("claim", world["ticket"]["id"], "--worktree", str(repos["tree"]), "--as", "worker", "--session", "s",
           "--json"))
    reply = ok(trk("where", str(repos["tree"]), "--json"))
    assert list(validator("where").iter_errors(reply)) == []
    broken = copy.deepcopy(reply)
    del broken["items"][0]["branch"]
    assert list(validator("where").iter_errors(broken)) != []


# ----- ac-3: archive and effective_archive ----------------------------------------------------------------


def test_nothing_set_reads_null(trk, world):
    item = show(trk, world["ticket"]["id"])
    assert (item["archive"], item["effective_archive"]) == (None, None)


def test_archive_on_a_program_is_inherited_by_its_descendants(trk, world):
    template = "~/projects/webapp/tmp/feature/archive/<tree>/"
    edited = ok(trk("edit", world["program"]["id"], "--archive", template, "--as", "worker", "--json"))["item"]
    assert (edited["archive"], edited["effective_archive"]) == (template, template)
    for name in ("project", "ticket", "other"):
        item = show(trk, world[name]["id"])
        assert (item["archive"], item["effective_archive"]) == (None, template), name


def test_a_nearer_archive_wins(trk, world):
    ok(trk("edit", world["program"]["id"], "--archive", "/far/<tree>.tgz", "--as", "worker", "--json"))
    ok(trk("edit", world["project"]["id"], "--archive", "/near/<tree>.tgz", "--as", "worker", "--json"))
    assert show(trk, world["ticket"]["id"])["effective_archive"] == "/near/<tree>.tgz"
    assert show(trk, world["other"]["id"])["effective_archive"] == "/far/<tree>.tgz"


def test_none_clears_it_so_it_inherits_again(trk, world):
    ok(trk("edit", world["program"]["id"], "--archive", "/far/<tree>.tgz", "--as", "worker", "--json"))
    ok(trk("edit", world["project"]["id"], "--archive", "/near/<tree>.tgz", "--as", "worker", "--json"))
    ok(trk("edit", world["project"]["id"], "--archive", "none", "--as", "worker", "--json"))
    assert show(trk, world["ticket"]["id"])["effective_archive"] == "/far/<tree>.tgz"
    ok(trk("edit", world["program"]["id"], "--archive", "none", "--as", "worker", "--json"))
    assert show(trk, world["ticket"]["id"])["effective_archive"] is None


def test_archive_edit_journals_item_updated(trk, world):
    ok(trk("edit", world["program"]["id"], "--archive", "/a/<tree>.tgz", "--as", "worker", "--json"))
    row = last_change(trk)
    assert row["change"] == "item.updated"
    assert row["data"] == {"fields": ["archive"], "old": {"archive": None}, "new": {"archive": "/a/<tree>.tgz"}}
    head = ok(trk("changes", "--since", "0", "--json"))["head"]
    ok(trk("edit", world["program"]["id"], "--archive", "/a/<tree>.tgz", "--as", "worker", "--json"))
    assert ok(trk("changes", "--since", "0", "--json"))["head"] == head


@pytest.mark.parametrize("value", ["", "  ", "a\nb"])
def test_a_blank_or_multiline_archive_is_a_usage_error(trk, world, value):
    assert trk("edit", world["program"]["id"], "--archive", value, "--as", "worker", "--json").code == 2


def test_the_human_view_names_the_archive(trk, world):
    ok(trk("edit", world["program"]["id"], "--archive", "/a/<tree>.tgz", "--as", "worker", "--json"))
    assert "archive: /a/<tree>.tgz (inherited)" in trk("show", world["ticket"]["id"]).out
    assert "archive: /a/<tree>.tgz\n" in trk("show", world["program"]["id"]).out
    ok(trk("edit", world["program"]["id"], "--archive", "none", "--as", "worker", "--json"))
    assert "archive:" not in trk("show", world["ticket"]["id"]).out


# ----- ac-4: reads, contract, schema 10 --------------------------------------------------------------------


def test_show_tree_and_ready_carry_the_three_fields_and_validate(trk, world, repos):
    ok(trk("edit", world["program"]["id"], "--archive", "/a/<tree>.tgz", "--as", "worker", "--json"))
    ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(repos["tree"]), "--as", "worker", "--json"))
    shown = ok(trk("show", world["ticket"]["id"], "--json"))
    ready = ok(trk("ready", "--json"))
    assert list(validator("show").iter_errors(shown)) == []
    assert list(validator("ready").iter_errors(ready)) == []
    for item in [shown["item"], *ready["items"], *ok(trk("tree", "--json"))["items"]]:
        assert {"worktree", "archive", "effective_archive"} <= set(item)
    by_id = {item["id"]: item for item in ready["items"]}
    assert by_id[world["ticket"]["id"]]["worktree"]["branch"] == "feat"
    assert by_id[world["ticket"]["id"]]["effective_archive"] == "/a/<tree>.tgz"


def test_a_malformed_worktree_breaks_the_contract(trk, world, repos):
    ok(trk("worktree", "set", world["ticket"]["id"], "--path", str(repos["tree"]), "--as", "worker", "--json"))
    reply = ok(trk("show", world["ticket"]["id"], "--json"))
    reply["item"]["worktree"] = {"path": str(repos["tree"])}
    assert list(validator("show").iter_errors(reply)) != []


def test_a_reply_from_an_older_build_without_the_fields_still_validates(trk, world):
    reply = ok(trk("show", world["ticket"]["id"], "--json"))
    for key in ("worktree", "archive", "effective_archive"):
        del reply["item"][key]
    assert list(validator("show").iter_errors(reply)) == []


@pytest.fixture
def v9_store(trk_home, monkeypatch):
    trk_home.mkdir()
    for name in ("store.db", "feed.head"):
        shutil.copyfile(FIXTURE_9 / name, trk_home / name)
    monkeypatch.setenv("TRK_NOW", OPENED_9)
    return trk_home


def query(home, statement, *params):
    conn = sqlite3.connect(home / "store.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(statement, params).fetchall()
    finally:
        conn.close()


def test_the_schema_9_fixture_is_a_checkpointed_store_built_by_client_names():
    assert len((FIXTURE_9 / "BUILT_FROM").read_text().strip()) == 40
    assert not any(FIXTURE_9.glob("store.db-*"))
    conn = sqlite3.connect(f"file:{FIXTURE_9 / 'store.db'}?immutable=1", uri=True)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == ("9",)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        assert "client" in columns and not any(c.startswith(("worktree", "archive")) for c in columns)
    finally:
        conn.close()


def test_a_schema_9_store_migrates_to_10_in_one_step_with_both_fields(trk, v9_store, repos):
    items_before = [tuple(r) for r in query(v9_store, "SELECT * FROM items ORDER BY id")]
    head = query(v9_store, "SELECT MAX(seq) FROM journal")[0][0]

    assert trk("changes", "--since", str(head), "--json").json()["changes"] == []  # migrates, journals nothing

    assert query(v9_store, "SELECT value FROM meta WHERE key = 'schema_version'")[0][0] == str(
        store.SCHEMA_VERSION)
    columns = [row["name"] for row in query(v9_store, "PRAGMA table_info(items)")]
    assert [tuple(r)[:len(items_before[0])] for r in query(v9_store, "SELECT * FROM items ORDER BY id")] == items_before
    assert {"worktree_path", "worktree_branch", "worktree_repo", "worktree_session", "worktree_at",
            "archive"} <= set(columns)
    assert query(v9_store, "SELECT count(*) FROM ledger_withdrawals")[0][0] == 0  # schema 11's table, empty

    t = query(v9_store, "SELECT id FROM items WHERE title = 'T'")[0][0]
    p = query(v9_store, "SELECT id FROM items WHERE title = 'P'")[0][0]
    item = ok(trk("show", t, "--json"))["item"]
    assert (item["worktree"], item["archive"], item["effective_archive"], item["effective_client"]) == \
        (None, None, None, "acme")
    assert item["claim"]["session"] == "s-live"
    ok(trk("worktree", "set", t, "--path", str(repos["tree"]), "--as", "worker", "--session", "s-live", "--json"))
    ok(trk("edit", p, "--archive", "/a/<tree>.tgz", "--as", "worker", "--json"))
    assert ok(trk("where", str(repos["tree"]), "--json"))["items"][0]["id"] == t
    assert ok(trk("show", t, "--json"))["item"]["effective_archive"] == "/a/<tree>.tgz"


def test_a_fresh_store_has_the_same_items_table_as_a_migrated_one(trk, v9_store, tmp_path, monkeypatch):
    trk("changes", "--since", "0")
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("TRK_HOME", str(fresh))
    trk("init", "--me", "michael")

    def shape(home):
        return [(r["name"], r["type"]) for r in query(home, "PRAGMA table_info(items)")]

    assert sorted(shape(fresh)) == sorted(shape(v9_store))
    assert {r["name"] for r in query(fresh, "SELECT name FROM sqlite_master WHERE type = 'index'")} >= \
        {r["name"] for r in query(v9_store, "SELECT name FROM sqlite_master WHERE type = 'index'")}


# ----- ac-5: docs ----------------------------------------------------------------------------------------


REPO = Path(__file__).resolve().parents[1]


def test_agents_md_and_the_trk_skill_tell_builders_to_record_and_clear_their_tree():
    for path in (REPO / "docs" / "agents.md", REPO / "skills" / "trk" / "SKILL.md"):
        text = path.read_text()
        assert "--worktree" in text, path
        assert "trk worktree clear" in text, path


def test_contract_documents_the_fields_and_where():
    item = (REPO / "contract" / "item.schema.json").read_text()
    for name in ('"worktree"', '"archive"', '"effective_archive"'):
        assert name in item
    assert (REPO / "contract" / "where.schema.json").exists()
