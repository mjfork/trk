"""Items carry a client name that children inherit (tr-fz0az): `client` is an item's own, `effective_client` its own
else the nearest ancestor's, as owner and effective_owner. Set by `new` and `edit --client`, in every item read, the
feed and `waiting`, and filtered by `ready --client` and `waiting --client`."""

import copy
import functools
import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource

from feed_contract import validator
from gated import new_flags, section_file

OLD = Path(__file__).parent / "fixtures" / "contract-before-client"


def ok(result):
    assert result.code == 0, result.err
    return result.json()


@pytest.fixture
def world(trk):
    ok(trk("init", "--me", "michael", "--json"))
    ok(trk("roles", "set", "product", "worker", "--json"))

    def new(*argv):
        return ok(trk("new", *argv, *new_flags(argv), "--json"))["item"]

    root = new("program", "Acme work", "--client", "acme")
    child = new("work-item", "Child", "--form", "self-contained", "--parent", root["id"])
    other = new("program", "Other work", "--client", "globex")
    plain = new("program", "No client")
    other_ticket = new("work-item", "Other ticket", "--form", "self-contained", "--parent", other["id"])
    plain_ticket = new("work-item", "Plain ticket", "--form", "self-contained", "--parent", plain["id"])
    return {"root": root, "child": child, "other": other, "plain": plain, "other_ticket": other_ticket,
            "plain_ticket": plain_ticket}


def question():
    return f"@{section_file('question', 'Why?')}"


def show(trk, item_id):
    return ok(trk("show", item_id, "--json"))["item"]


def test_new_sets_client_and_effective_client(world):
    assert (world["root"]["client"], world["root"]["effective_client"]) == ("acme", "acme")


def test_a_child_inherits_the_effective_client(world):
    assert (world["child"]["client"], world["child"]["effective_client"]) == (None, "acme")


def test_an_item_with_no_client_anywhere_has_none(world):
    assert (world["plain"]["client"], world["plain"]["effective_client"]) == (None, None)


def test_a_child_may_override_its_parents_client(trk, world):
    ok(trk("edit", world["child"]["id"], "--client", "globex", "--json"))
    child = show(trk, world["child"]["id"])
    assert (child["client"], child["effective_client"]) == ("globex", "globex")
    assert show(trk, world["root"]["id"])["effective_client"] == "acme"


def test_edit_none_clears_the_root_and_what_children_inherit(trk, world):
    edited = ok(trk("edit", world["root"]["id"], "--client", "none", "--json"))["item"]
    assert (edited["client"], edited["effective_client"]) == (None, None)
    assert show(trk, world["child"]["id"])["effective_client"] is None


def test_edit_journals_item_updated_with_old_and_new_and_the_effective_client(trk, world):
    ok(trk("edit", world["root"]["id"], "--client", "none", "--json"))
    row = ok(trk("changes", "--since", "0", "--json"))["changes"][-1]
    assert row["change"] == "item.updated" and row["item"] == world["root"]["id"]
    assert row["data"] == {"fields": ["client"], "old": {"client": "acme"}, "new": {"client": None}}
    assert row["client"] is None


def test_feed_rows_carry_the_effective_client(trk, world):
    ok(trk("edit", world["child"]["id"], "--title", "Renamed", "--json"))
    row = ok(trk("changes", "--since", "0", "--json"))["changes"][-1]
    assert row["item"] == world["child"]["id"] and row["client"] == "acme"
    created = [r for r in ok(trk("changes", "--since", "0", "--json"))["changes"]
               if r["change"] == "item.created" and r["item"] == world["root"]["id"]]
    assert created[0]["client"] == "acme"


def test_a_rows_without_an_item_have_no_client(trk, world):
    rows = ok(trk("changes", "--since", "0", "--json"))["changes"]
    assert all(row["client"] is None for row in rows if row["item"] is None)


def test_setting_the_same_client_changes_nothing(trk, world):
    before = ok(trk("changes", "--since", "0", "--json"))["head"]
    ok(trk("edit", world["root"]["id"], "--client", "acme", "--json"))
    assert ok(trk("changes", "--since", "0", "--json"))["head"] == before


@pytest.mark.parametrize("name", ["Acme", "acme_1", "-acme", "", "a b", "none"])
def test_a_bad_name_is_a_usage_error(trk, world, name):
    created = trk("new", "program", "X", "--criterion", "It works.", "--client", name, "--json")
    assert created.code == 2, created.out
    edited = trk("edit", world["root"]["id"], "--client", name, "--json") if name != "none" else None
    if edited is not None:
        assert edited.code == 2
    assert show(trk, world["root"]["id"])["client"] == "acme"


def test_edit_client_alone_counts_as_an_edit(trk, world):
    assert trk("edit", world["plain"]["id"], "--client", "acme", "--json").code == 0


def test_ready_filters_on_the_effective_client(trk, world):
    listed = ok(trk("ready", "--client", "acme", "--json"))["items"]
    assert {item["id"] for item in listed} == {world["child"]["id"]}
    assert all(item["effective_client"] == "acme" for item in listed)
    everything = {item["id"] for item in ok(trk("ready", "--json"))["items"]}
    assert {world["plain_ticket"]["id"], world["other_ticket"]["id"]} <= everything
    assert ok(trk("ready", "--client", "nobody", "--json"))["items"] == []


def test_waiting_carries_and_filters_on_the_client(trk, world):
    ask = ok(trk("ask", world["child"]["id"], "--to", "product", "Which?", "--question", question(), "--json"))
    asks = ok(trk("waiting", "--all", "--json"))["asks"]
    assert [(a["client"], a["effective_client"]) for a in asks] == [(None, "acme")]
    assert asks[0]["id"] == ask["item"]["id"]
    assert len(ok(trk("waiting", "--all", "--client", "acme", "--json"))["asks"]) == 1
    assert ok(trk("waiting", "--all", "--client", "globex", "--json"))["asks"] == []


def test_the_human_view_names_the_client(trk, world):
    out = trk("show", world["child"]["id"])
    assert "client: acme (inherited)" in out.out
    assert "client:" not in trk("show", world["plain"]["id"]).out


def test_every_item_read_validates_with_the_client_fields(trk, world):
    child = world["child"]["id"]
    ok(trk("ask", child, "--to", "product", "Which?", "--question", question(), "--json"))
    for name, reply in (("show", ok(trk("show", child, "--json"))), ("ready", ok(trk("ready", "--json"))),
                        ("waiting", ok(trk("waiting", "--all", "--json")))):
        assert list(validator(name).iter_errors(reply)) == []
    assert ok(trk("show", child, "--json"))["item"]["effective_client"] == "acme"


# -- older consumers: hx still holds the contract copy from before the two fields --

@functools.cache
def old_validator(name):
    resources = []
    for path in sorted(OLD.glob("*.schema.json")):
        schema = json.loads(path.read_text())
        resources.append((schema["$id"], Resource.from_contents(schema)))
    schema = json.loads((OLD / f"{name}.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema, registry=Registry().with_resources(resources))


def without_client(value):
    """The reply an older build prints: no `client` or `effective_client` anywhere."""
    if isinstance(value, dict):
        return {k: without_client(v) for k, v in value.items() if k not in ("client", "effective_client")}
    if isinstance(value, list):
        return [without_client(v) for v in value]
    return value


def test_a_reply_without_the_two_fields_still_validates_against_the_current_contract(trk, world):
    child = world["child"]["id"]
    ok(trk("ask", child, "--to", "product", "Which?", "--question", question(), "--json"))
    replies = {"show": ok(trk("show", child, "--json")), "ready": ok(trk("ready", "--json")),
               "waiting": ok(trk("waiting", "--all", "--json")), "changes": ok(trk("changes", "--since", "0", "--json"))}
    for name, reply in replies.items():
        assert list(validator(name).iter_errors(without_client(copy.deepcopy(reply)))) == [], name


def test_a_reply_with_the_two_fields_still_validates_against_the_older_contract(trk, world):
    child = world["child"]["id"]
    ok(trk("ask", child, "--to", "product", "Which?", "--question", question(), "--json"))
    replies = {"show": ok(trk("show", child, "--json")), "ready": ok(trk("ready", "--json")),
               "waiting": ok(trk("waiting", "--all", "--json")), "changes": ok(trk("changes", "--since", "0", "--json"))}
    for name, reply in replies.items():
        assert list(old_validator(name).iter_errors(reply)) == [], name


def test_the_older_copy_validates_an_older_reply(trk, world):
    reply = without_client(ok(trk("show", world["child"]["id"], "--json")))
    assert list(old_validator("show").iter_errors(reply)) == []


def test_a_malformed_client_breaks_the_current_contract(trk, world):
    reply = ok(trk("show", world["child"]["id"], "--json"))
    reply["item"]["effective_client"] = "Acme_1"
    assert list(validator("show").iter_errors(reply)) != []
