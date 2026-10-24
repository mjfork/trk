"""The RED-read ask as data (tr-bvxs5): `trk waiting --json` and `trk show --json` give an open RED-read ask a
`red_read` object (the ticket, the RED rating's full head sha, the roles and RED file count, the nine review-brief
fields as text or null, what the brief lacks, whether it is complete, who wrote it), read by the same parser the
gates use. Every ask also carries `rev` (the latest journal seq touching it, a RED-read ask's ticket included),
`awaiting` (`addressee` or `asker`) and a full `body` on each reply `trk reply` recorded.

Every test runs against a real throwaway git repo whose rules file makes `contract/**` RED."""

import pytest

from feed_contract import validator
from gated import commit
from test_blast_and_verified import BRIEF, head, item, ok, pr, write_brief
from test_red_read import asks_under, red_ticket
from test_review_brief import without
from trk.blast import BRIEF_HEADINGS, brief_missing

FIELDS = ("decision", "what_changes", "why_red", "impact", "evidence", "rollback", "decision_criteria",
          "where_to_look", "recommendation")
V = ("--as", "reviewer", "--session", "V")


def errors(name, value):
    return list(validator(name).iter_errors(value))


def rated_red(trk, tmp_path):
    """T7 rated RED by worker, its RED-read ask open to michael: (repo path, head sha, T7, ask id)."""
    path, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "actor", "add", "reviewer", "--kind", "agent")
    assert ok(trk, "blast-radius", t7, "--as", "worker") == "red"
    [ask] = asks_under(trk, t7)
    return path, sha, t7, ask["id"]


@pytest.fixture
def red(trk, tmp_path):
    """(head sha, T7, ask id) of `rated_red`."""
    _, sha, t7, ask = rated_red(trk, tmp_path)
    return sha, t7, ask


def waiting(trk, ask):
    """The ask's `waiting --json` entry: in its addressee's list, or its asker's while it is held (tr-4bhay: a
    RED-read ask with no complete brief, or no counting rating, is left out of the addressee's list)."""
    for me in ("michael", "worker"):
        reply = trk("waiting", "--me", me, "--json").json()
        assert errors("waiting", reply) == []
        found = [a for a in reply["asks"] if a["id"] == ask]
        if found:
            return found[0]
    raise AssertionError(f"{ask} is in nobody's waiting list")


def shown(trk, ask):
    reply = trk("show", ask, "--json").json()
    assert errors("show", reply) == []
    return reply["item"]


def reply(trk, ask, body, actor, tmp_path):
    f = tmp_path / "reply.md"
    f.write_text(body)
    ok(trk, "reply", ask, "--as", actor, "--body", f"@{f}")


# --- ac-1: red_read ---

def test_a_complete_brief_is_data_in_waiting_and_show(trk, red):
    sha, t7, ask = red
    write_brief(trk, t7, BRIEF, *V)
    for entry in (waiting(trk, ask), shown(trk, ask)):
        rr = entry["red_read"]
        assert (rr["ticket"], rr["sha"], rr["roles"], rr["files_count"]) == (t7, sha, ["engineering"], 1)
        assert len(rr["sha"]) == 40
        assert rr["brief"] == {f: f"Text under {h}." for f, h in zip(FIELDS, BRIEF_HEADINGS)}
        assert (rr["brief_lacking"], rr["brief_complete"]) == ([], True)
        assert rr["brief_writer"] == {"actor": "reviewer", "session": "V"}


def test_no_brief_is_nine_nulls_and_everything_lacking(trk, red):
    _, _, ask = red
    rr = waiting(trk, ask)["red_read"]
    assert rr["brief"] == dict.fromkeys(FIELDS)
    assert (rr["brief_lacking"], rr["brief_complete"], rr["brief_writer"]) == (list(BRIEF_HEADINGS), False, None)


def test_a_partial_brief_names_what_it_lacks_and_keeps_what_it_has(trk, red):
    _, t7, ask = red
    text = without("Rollback", "Evidence")
    write_brief(trk, t7, text, *V)
    rr = shown(trk, ask)["red_read"]
    assert rr["brief_lacking"] == ["Evidence", "Rollback"] == brief_missing(text)
    assert not rr["brief_complete"]
    assert (rr["brief"]["rollback"], rr["brief"]["evidence"]) == (None, None)
    assert rr["brief"]["decision"] == "Text under Decision."
    assert rr["brief_writer"] == {"actor": "reviewer", "session": "V"}


def test_data_and_gate_agree_on_tolerant_headings(trk, red):
    """tr-nc512's matching rules: numbering, bold-only lines, a colon, "it is" for "it's"."""
    _, t7, ask = red
    text = "".join(f"**{i}. {h}:**\n\nBody {i}.\n\n" for i, h in enumerate(BRIEF_HEADINGS, 1)).replace(
        "Why it's RED", "Why it is RED")
    write_brief(trk, t7, text, *V)
    rr = waiting(trk, ask)["red_read"]
    assert rr["brief_complete"] and rr["brief_lacking"] == []
    assert rr["brief"]["why_red"] == "Body 3." and rr["brief"]["recommendation"] == "Body 9."
    assert "review-brief" not in [m["need"] for m in item(trk, t7)["gate"]["missing"]]
    write_brief(trk, t7, text.replace("**9. Recommendation:**", "9. Recommendation"), *V)  # no longer a heading
    rr = waiting(trk, ask)["red_read"]
    assert rr["brief_lacking"] == ["Recommendation"] and not rr["brief_complete"]
    [need] = [m for m in item(trk, t7)["gate"]["missing"] if m["need"] == "review-brief"]
    assert need["missing"] == rr["brief_lacking"]


def test_only_a_red_read_ask_carries_red_read(trk, red, tmp_path):
    _, t7, ask = red
    other = tmp_path / "q.md"
    other.write_text("Which?\n")
    plain = ok(trk, "ask", t7, "--to", "engineering", "Which?", "--question", f"@{other}", "--as", "worker")
    assert "red_read" not in waiting(trk, plain) and "red_read" not in shown(trk, plain)
    assert "red_read" in waiting(trk, ask)
    assert "red_read" not in item(trk, t7)


def test_red_read_follows_the_ticket_while_the_ask_is_open(trk, red):
    sha, t7, ask = red
    write_brief(trk, t7, BRIEF, *V)
    assert shown(trk, ask)["red_read"]["sha"] == sha
    ok(trk, "review", "record", t7, "approved", "--sha", sha, "--note", "x", "--as", "michael")
    assert "red_read" not in shown(trk, ask)  # answered: nothing left to approve


# --- ledger-6: a stale rating keeps the card, marked not current ---

def test_a_fresh_rating_is_current(trk, red):
    sha, _, ask = red
    rr = waiting(trk, ask)["red_read"]
    assert (rr["sha"], rr["current"]) == (sha, True)


def test_a_new_head_before_a_re_rating_keeps_red_read_marked_not_current(trk, tmp_path):
    path, sha, t7, ask = rated_red(trk, tmp_path)
    write_brief(trk, t7, BRIEF, *V)
    fix = commit(path, "contract/y.json")
    pr(trk, t7, fix)
    assert item(trk, t7)["lane"] is None  # the rating no longer counts at the new head
    for entry in (waiting(trk, ask), shown(trk, ask)):
        rr = entry["red_read"]
        assert (rr["sha"], rr["current"], rr["brief_complete"]) == (sha, False, True)
        assert entry["status"] == "open"
    assert ok(trk, "blast-radius", t7, "--as", "worker") == "red"
    [again] = [a for a in asks_under(trk, t7) if a["status"] == "open"]
    rr = waiting(trk, again["id"])["red_read"]
    assert (rr["sha"], rr["current"]) == (fix, True)


def test_a_reopen_stage_keeps_red_read_marked_not_current(trk, tmp_path):
    _, sha, t7, ask = rated_red(trk, tmp_path)
    ok(trk, "reopen-stage", t7, "framed", "--reason", "back", "--as", "worker")
    rr = shown(trk, ask)["red_read"]
    assert (rr["sha"], rr["current"]) == (sha, False)


# --- rev, awaiting, reply bodies ---

def test_rev_advances_on_every_reply(trk, red, tmp_path):
    _, _, ask = red
    seen = [shown(trk, ask)["rev"]]
    for actor in ("michael", "worker", "michael", "michael"):
        reply(trk, ask, f"{actor} says {len(seen)}.\n", actor, tmp_path)
        entry, wait = shown(trk, ask), waiting(trk, ask)
        assert entry["rev"] == wait["rev"] == head(trk) > seen[-1]
        seen.append(entry["rev"])
    assert seen == sorted(set(seen))


def test_a_red_read_ask_rev_is_the_max_over_the_ask_and_its_ticket(trk, red):
    _, t7, ask = red
    before = shown(trk, ask)["rev"]
    write_brief(trk, t7, BRIEF, *V)  # a ticket write; the ask row itself is rewritten too
    after = waiting(trk, ask)["rev"]
    assert after > before
    ok(trk, "actor", "add", "bystander", "--kind", "agent")
    ok(trk, "criterion", "add", t7, "Another line.", "--as", "bystander")  # touches the ticket only
    assert waiting(trk, ask)["rev"] == head(trk) > after


def test_a_plain_ask_rev_ignores_other_items(trk, red, tmp_path):
    _, t7, _ = red
    q = tmp_path / "q.md"
    q.write_text("Which?\n")
    plain = ok(trk, "ask", t7, "--to", "engineering", "Which?", "--question", f"@{q}", "--as", "worker")
    mine = shown(trk, plain)["rev"]
    ok(trk, "criterion", "add", t7, "Another line.", "--as", "worker")
    assert shown(trk, plain)["rev"] == mine < head(trk)


def test_awaiting_follows_who_wrote_last_since_the_opening(trk, red, tmp_path):
    _, _, ask = red
    assert shown(trk, ask)["awaiting"] == "addressee"
    reply(trk, ask, "Why RED?\n", "michael", tmp_path)
    assert (shown(trk, ask)["awaiting"], waiting(trk, ask)["awaiting"]) == ("asker", "asker")
    reply(trk, ask, "Because the contract changes.\n", "worker", tmp_path)
    assert shown(trk, ask)["awaiting"] == "addressee"
    reply(trk, ask, "Fine, one more thing.\n", "michael", tmp_path)
    assert shown(trk, ask)["awaiting"] == "asker"


def test_a_closed_ask_awaits_no_one(trk, red, tmp_path):
    sha, t7, ask = red
    write_brief(trk, t7, BRIEF, *V)  # a direct approval is refused without the brief (tr-4bhay)
    reply(trk, ask, "Looks fine.\n", "michael", tmp_path)
    assert shown(trk, ask)["awaiting"] == "asker"
    ok(trk, "review", "record", t7, "approved", "--sha", sha, "--note", "x", "--as", "michael")
    closed = shown(trk, ask)
    assert closed["status"] == "closed" and closed["awaiting"] is None


def test_each_reply_carries_its_full_text(trk, red, tmp_path):
    _, _, ask = red
    long = "word " * 200 + "\n\nsecond paragraph\n"
    reply(trk, ask, long, "michael", tmp_path)
    [r] = shown(trk, ask)["replies"]
    assert r["body"] == long and r["excerpt"] != long and r["comment"].startswith("reply-")


def test_old_output_still_validates(trk, red):
    """The new fields are optional: an ask without rev, awaiting, red_read or reply bodies is still valid."""
    _, _, ask = red
    reply_json = trk("waiting", "--all", "--json").json()
    for entry in reply_json["asks"]:
        for key in ("rev", "awaiting", "red_read"):
            del entry[key]
    assert errors("waiting", reply_json) == []
    old = trk("show", ask, "--json").json()
    for key in ("rev", "awaiting", "red_read"):
        del old["item"][key]
    assert errors("show", old) == []
    non_ask = {**old["item"], "rev": 1}
    assert errors("item", {**non_ask, "id": "tr-aaaaa", "form": "self-contained", "kind": "work-item"}) != []


# --- held: an ask whose rating no longer counts never reaches its addressee as a plain question ---

def held_of(trk, ask, asker="worker"):
    """`held` of the ask as its asker lists it, or None when it is not held."""
    found = [a for a in trk("waiting", "--me", asker, "--json").json()["asks"] if a["id"] == ask]
    return found[0]["held"] if found else None  # only a held ask is in its asker's list


def for_michael(trk):
    return [a["id"] for a in trk("waiting", "--me", "michael", "--json").json()["asks"]]


def test_a_new_head_before_a_re_rating_holds_the_ask_as_rating_stale_until_re_rated(trk, tmp_path):
    path, _, t7, ask = rated_red(trk, tmp_path)
    write_brief(trk, t7, BRIEF, *V)
    assert (for_michael(trk), held_of(trk, ask)) == ([ask], None)
    pr(trk, t7, commit(path, "contract/y.json"))
    assert for_michael(trk) == []
    assert held_of(trk, ask).startswith("rating-stale:")
    assert shown(trk, ask)["status"] == "open" and shown(trk, ask)["red_read"]["current"] is False
    ok(trk, "blast-radius", t7, "--as", "worker")
    assert (for_michael(trk), held_of(trk, ask)) == ([ask], None)


def test_a_reopen_stage_holds_the_ask_as_rating_stale(trk, tmp_path):
    _, _, t7, ask = rated_red(trk, tmp_path)
    write_brief(trk, t7, BRIEF, *V)
    ok(trk, "reopen-stage", t7, "framed", "--reason", "back", "--as", "worker")
    assert for_michael(trk) == [] and held_of(trk, ask).startswith("rating-stale:")


def test_a_retracted_rating_holds_the_ask_as_rating_missing_until_re_rated(trk, tmp_path):
    _, _, t7, ask = rated_red(trk, tmp_path)
    write_brief(trk, t7, BRIEF, *V)
    [rating] = [e["id"] for e in trk("evidence", t7, "--json").json()["evidence"] if e["kind"] == "blast-radius"]
    ok(trk, "evidence", "retract", t7, rating, "--note", "wrong base", "--as", "worker")
    assert for_michael(trk) == [] and held_of(trk, ask).startswith("rating-missing:")
    assert "red_read" not in shown(trk, ask) or shown(trk, ask)["red_read"]["current"] is False
    ok(trk, "blast-radius", t7, "--as", "worker")
    assert (for_michael(trk), held_of(trk, ask)) == ([ask], None)
