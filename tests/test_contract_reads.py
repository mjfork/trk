"""The read verbs herdr uses (show, ready, claims, actors, roles, waiting) print JSON that validates against
their files in contract/, checked here against real CLI output; each schema also rejects a reply
that breaks it, so a vacuous schema can't pass."""

import copy

import pytest

from feed_contract import validator
from gated import new_flags

T0 = "2026-10-10T12:00:00.000Z"
SESSION = "5b0c1d2e-0000-4000-8000-000000000001"


def errors(name, value):
    return list(validator(name).iter_errors(value))


def ok(result):
    assert result.code == 0, result.err
    return result.json()


@pytest.fixture
def world(trk, monkeypatch, tmp_path):
    """A store with every item kind and form, sections, edges, a live claim, a handoff, a closed item and an open ask."""
    monkeypatch.setenv("TRK_NOW", T0)
    ok(trk("init", "--me", "michael", "--json"))
    ok(trk("actor", "add", "worker", "--kind", "agent", "--display", "Worker", "--json"))
    ok(trk("roles", "set", "product", "worker", "--json"))

    def new(*argv):
        return ok(trk("new", *argv, *new_flags(argv), "--json"))["item"]["id"]

    program = new("program", "P", "--jira", "https://x/PROJ-1", "--repo", "/r", "--urgent")
    project = new("project", "Spec", "--parent", program)
    bound = new("work-item", "Bound", "--form", "spec-bound", "--parent", project)
    free = new("work-item", "Free", "--form", "self-contained", "--parent", program, "--owner", "worker")
    choice = new("work-item", "Choice", "--form", "decision", "--subtype", "research", "--parent", program)
    done = new("work-item", "Done", "--form", "self-contained", "--parent", program)
    body = tmp_path / "notes.md"
    body.write_text("extra\n")
    ok(trk("section", "set", free, "notes", "--file", str(body), "--json"))
    ok(trk("handoff", "add", free, "--file", str(body), "--session", SESSION, "--as", "worker", "--json"))
    ok(trk("link", choice, "blocks", bound, "--json"))
    ok(trk("link", free, "relates", choice, "--json"))
    ok(trk("close", done, "--resolution", "wont-do", "--outcome", "Not needed.", "--json"))
    ok(trk("claim", free, "--session", SESSION, "--as", "worker", "--json"))
    asked = ok(trk("ask", free, "--to", "product", "Which?", "--question", f"@{body}", "--json"))["item"]["id"]
    return {"program": program, "project": project, "bound": bound, "free": free, "choice": choice, "done": done,
            "ask": asked}


def test_show_validates_for_every_kind_and_form(trk, world):
    for item_id in world.values():
        assert errors("show", ok(trk("show", item_id, "--json"))) == []
        assert errors("show", ok(trk("show", item_id, "--no-sections", "--json"))) == []


def test_ready_validates(trk, world):
    reply = ok(trk("ready", "--json"))
    assert reply["items"], "the world has ready items"
    assert errors("ready", reply) == []
    assert errors("ready", ok(trk("ready", "--root", world["bound"], "--json"))) == []


def test_claims_validates_with_and_without_a_live_claim(trk, world):
    reply = ok(trk("claims", "--json"))
    assert reply["claims"]
    assert errors("claims", reply) == []
    ok(trk("claim", "release", "--session", SESSION, "--json"))
    empty = ok(trk("claims", "--json"))
    assert empty["claims"] == []
    assert errors("claims", empty) == []


def test_actors_and_roles_validate(trk, world):
    assert errors("actors", ok(trk("actors", "--json"))) == []
    assert errors("roles", ok(trk("roles", "--json"))) == []


@pytest.fixture
def shown(trk, world):
    return ok(trk("show", world["free"], "--json"))


def changed(reply, path, value):
    """A copy of `reply` with the value at `path` replaced; ... deletes it."""
    reply = copy.deepcopy(reply)
    *parents, last = path
    target = reply
    for key in parents:
        target = target[key]
    if value is ...:
        del target[last]
    else:
        target[last] = value
    return reply


@pytest.mark.parametrize(
    "path, value",
    [
        pytest.param(("schema",), 2, id="schema-2"),
        pytest.param(("item", "id"), ..., id="missing-id"),
        pytest.param(("item", "kind"), "work_item", id="internal-kind-spelling"),
        pytest.param(("item", "form"), None, id="work-item-without-form"),
        pytest.param(("item", "status"), "closed", id="closed-without-resolution"),
        pytest.param(("item", "resolution"), "wont_do", id="internal-resolution-spelling"),
        pytest.param(("item", "parent"), None, id="depth-2-without-parent"),
        pytest.param(("item", "spec_state"), "draft", id="spec-state-off-a-project"),
        pytest.param(("item", "ready"), True, id="ready-with-why-not"),
        pytest.param(("item", "claim"), None, id="in-progress-without-claim"),
        pytest.param(("item", "claim", "lease_until"), "soon", id="claim-lease-not-a-timestamp"),
        pytest.param(("item", "sections", 0, "present"), "yes", id="section-present-not-boolean"),
        pytest.param(("item", "edges", 0, "dir"), "both", id="unknown-edge-dir"),
        pytest.param(("item", "created_at"), "2026-10-10", id="created-not-a-timestamp"),
        pytest.param(("item", "handoff"), ..., id="missing-handoff"),
        pytest.param(("item", "handoff", "body"), "extra\n", id="handoff-with-body"),
        pytest.param(("item", "handoff", "first_line"), "two\nlines", id="handoff-first-line-newline"),
        pytest.param(("item", "handoff", "id"), 0, id="handoff-id-zero"),
        pytest.param(("item", "handoff", "at"), "2026-10-10", id="handoff-at-not-a-timestamp"),
        pytest.param(("item", "stage"), "shipped", id="unknown-stage"),
        pytest.param(("item", "stage"), ..., id="missing-stage"),
        pytest.param(("item", "criteria"), ..., id="missing-criteria"),
        pytest.param(("item", "criteria", 0, "handle"), "1", id="criterion-handle-not-ac-n"),
        pytest.param(("item", "criteria", 0, "status"), "passed", id="unknown-criterion-status"),
    ],
)
def test_show_schema_rejects_a_broken_item(shown, path, value):
    assert errors("show", changed(shown, path, value)) != []


def test_show_schema_rejects_a_stage_off_a_ticket(trk, world):
    shown = ok(trk("show", world["choice"], "--json"))
    assert errors("show", shown) == []
    assert errors("show", changed(shown, ("item", "stage"), "framed")) != []


@pytest.mark.parametrize(
    "path, value",
    [
        pytest.param(("item", "asker"), ..., id="ask-without-asker"),
        pytest.param(("item", "addressee_role"), "Product", id="ask-role-not-a-slug"),
        pytest.param(("item", "addressee_kind"), "robot", id="ask-unknown-addressee-kind"),
    ],
)
def test_show_schema_rejects_a_broken_ask(trk, world, path, value):
    shown = ok(trk("show", world["ask"], "--json"))
    assert errors("show", changed(shown, path, value)) != []


def test_show_schema_rejects_ask_keys_off_an_ask_and_a_broken_open_ask(trk, world, shown):
    assert errors("show", changed(shown, ("item", "asker"), "worker")) != []
    assert errors("show", changed(shown, ("item", "open_asks"), ...)) != []
    assert errors("show", changed(shown, ("item", "open_asks", 0, "urgent"), "no")) != []


def test_show_carries_the_latest_handoff_summary_or_null(trk, world, shown):
    assert shown["item"]["handoff"]["first_line"] == "extra"
    assert ok(trk("show", world["bound"], "--json"))["item"]["handoff"] is None


def test_ready_schema_rejects_a_broken_item(trk, world):
    reply = ok(trk("ready", "--json"))
    assert errors("ready", changed(reply, ("items", 0, "urgent"), "no")) != []
    assert errors("ready", changed(reply, ("items",), ...)) != []


def test_claims_actors_and_roles_schemas_reject_broken_replies(trk, world):
    claims = ok(trk("claims", "--json"))
    actors = ok(trk("actors", "--json"))
    roles = ok(trk("roles", "--json"))
    assert errors("claims", changed(claims, ("claims", 0, "session"), None)) != []
    assert errors("claims", changed(claims, ("claims", 0, "item"), ...)) != []
    assert errors("actors", changed(actors, ("actors", 0, "kind"), "robot")) != []
    assert errors("actors", changed(actors, ("actors", 0, "id"), "Herdr")) != []
    assert errors("roles", changed(roles, ("roles", 0, "role"), "parent")) != []
    assert errors("roles", changed(roles, ("roles", 0, "actor_kind"), None)) != []


def test_additional_fields_are_allowed(shown):
    """ADR 0002: adding fields never bumps schema, so consumers' schemas must accept them."""
    extra = changed(shown, ("item", "audience"), {"url": "https://x", "version": 1})
    extra["future"] = True
    assert errors("show", extra) == []


def test_an_items_page_is_null_or_its_url_version_publish_time_and_dirtiness(shown):
    page = {"url": "https://x.test/artifacts/a", "version": 2, "published_at": "2026-10-11T09:00:00.000Z",
            "dirty": False}
    assert errors("show", changed(shown, ("item", "page"), page)) == []
    assert errors("show", changed(shown, ("item", "page"), {"url": "https://x", "version": 1})) != []
    assert shown["item"]["page"] is None


def test_the_other_item_reads_print_the_same_item(trk, world, tmp_path):
    """tree, claim, new, edit and section writes print item.schema.json's object too."""
    assert all(errors("item", item) == [] for item in ok(trk("tree", "--json"))["items"])
    replies = [
        ok(trk("new", "work-item", "N", "--form", "decision", "--subtype", "task", "--parent", world["program"], "--json"))["item"],
        ok(trk("edit", world["choice"], "--title", "Choice 2", "--json"))["item"],
        ok(trk("claim", world["choice"], "--session", SESSION, "--as", "worker", "--json"))["claimed"],
    ]
    body = tmp_path / "q.md"
    body.write_text("Which?\n")
    replies.append(ok(trk("section", "set", world["choice"], "question", "--file", str(body), "--json"))["item"])
    assert all(errors("item", item) == [] for item in replies)


def test_waiting_validates_and_rejects_a_broken_reply(trk, world):
    reply = ok(trk("waiting", "--all", "--json"))
    assert [a["id"] for a in reply["asks"]] == [world["ask"]]
    assert errors("waiting", reply) == []
    assert errors("item", {**reply["asks"][0], "parent": world["free"]}) == [], "otherwise the item JSON"
    assert errors("waiting", ok(trk("waiting", "--me", "michael", "--json"))) == []
    assert errors("waiting", changed(reply, ("asks", 0, "parent"), ...)) != []
    assert errors("waiting", changed(reply, ("asks", 0, "parent", "kind"), "work_item")) != []
    assert errors("waiting", changed(reply, ("asks", 0, "age_seconds"), -1)) != []
    assert errors("waiting", changed(reply, ("asks", 0, "form"), "decision")) != []
    assert errors("waiting", changed(reply, ("asks", 0, "asker"), ...)) != []
    assert errors("waiting", changed(reply, ("asks", 0, "created_at"), "2026-10-10")) != []
