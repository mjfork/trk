"""The RED-read ask and the review record (Stage gates, Blast radius; Decision ledger): a RED rating opens
one `engineering` ask under the ticket, `trk review record` writes `human-review` evidence at the head sha,
an approval answers the ask as a proxy answer by evidence, and Merged for a RED ticket needs that pass.

Every test runs against a real throwaway git repo whose rules file makes `contract/**` RED. Every journal
row a test writes is checked against `contract/changes.schema.json` by the suite's fixture."""

from gated import commit, git
from test_blast_and_verified import (DEST, RULES, accept, blast, feed, head, item, lane_repo, lane_ticket, merge,
                                     new_ticket, ok, pr, refusal, run, write_brief)

PR = "https://github.com/o/r/pull/1"
REVIEW = "https://github.com/o/r/pull/1#pullrequestreview-1"


def red_ticket(trk, tmp_path):
    """Ticket T7 at Built changing `contract/x.json`, with agent `worker` registered: (path, head sha, T7)."""
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["contract/x.json"])
    t7 = lane_ticket(trk, path, sha)
    return path, sha, t7


def land_and_extend(trk, path, t, sha):
    """The part of the PR rated so far lands on `main`, and the PR gains one GREEN commit: rating against `main` now
    shows only that commit, as it would for a PR whose RED file went in elsewhere. (A `--base` at the head, or at a
    commit in the middle of the PR, hides the PR and is refused `base-hides-pr`.) Returns the new head."""
    git(path, "branch", "-f", "main", sha)
    new = commit(path, "docs/z.md")
    pr(trk, t, new)
    return new


def asks_under(trk, item_id):
    return [c for c in trk("tree", item_id, "--all", "--json").json()["items"] if c["form"] == "ask"]


def review(trk, t, sha, verdict, *flags):
    return ok(trk, "review", "record", t, "--ref", REVIEW, "--sha", sha, verdict, "--note", "Read it.", *flags)


def needs(trk, t):
    return [m["need"] for m in item(trk, t)["gate"]["missing"]]


# --- ticket AC 1: the RED-ask half of spec AC 11 ---

def test_a_red_rating_opens_one_engineering_ask_and_an_approval_answers_it_by_evidence(trk, tmp_path):
    _, sha, t7 = red_ticket(trk, tmp_path)
    since = head(trk)
    assert ok(trk, "blast-radius", t7, "--as", "worker") == "red"
    [ask] = asks_under(trk, t7)
    assert (ask["title"], ask["asker"], ask["addressee_role"], ask["addressee"], ask["status"]) == (
        f"Read RED changes in {PR}", "worker", "engineering", "michael", "open")
    question = ok(trk, "section", "get", ask["id"], "question")
    assert PR in question and "contract/x.json" in question
    assert [r["change"] for r in feed(trk, since)] == ["evidence.added", "ask.opened"]

    ok(trk, "blast-radius", t7, "--as", "worker")
    assert [a["id"] for a in asks_under(trk, t7)] == [ask["id"]]

    merge(trk, t7, sha)
    review(trk, t7, sha, "changes-requested", "--as", "michael")
    assert item(trk, ask["id"])["status"] == "open"
    error = refusal(trk, "advance", t7)
    assert "human-review" in [m["need"] for m in error["missing"]]

    since = head(trk)
    approved = review(trk, t7, sha, "approved", "--as", "michael")
    answered = item(trk, ask["id"])
    assert (answered["status"], answered["resolution"], answered["outcome"]) == (
        "closed", "done", f"Approved on GitHub: {REVIEW}")
    rows = feed(trk, since)
    assert [r["change"] for r in rows] == ["evidence.added", "ask.answered"]
    assert (rows[1]["actor"], rows[1]["data"]["proxy"], rows[1]["data"]["via"], rows[1]["data"]["evidence"]) == (
        "michael", True, "evidence", approved)
    assert "human-review" not in needs(trk, t7)


def test_review_record_writes_human_review_rows_at_the_head_sha(trk, tmp_path):
    _, sha, t7 = red_ticket(trk, tmp_path)
    passed = review(trk, t7, sha, "approved")
    failed = review(trk, t7, sha, "changes-requested")
    rows = {e["id"]: e for e in trk("evidence", t7, "--json").json()["evidence"]}
    assert (rows[passed]["kind"], rows[passed]["gate"], rows[passed]["ref"], rows[passed]["sha"],
            rows[passed]["verdict"]) == ("human-review", "merged", REVIEW, sha, "pass")
    assert rows[failed]["verdict"] == "fail"
    assert trk("review", "record", t7, "--ref", REVIEW, "--sha", sha, "maybe", "--note", "x").code == 2
    assert trk("review", "record", t7, "--ref", REVIEW, "--sha", sha, "approved").code == 2  # --note stays required


# --- tr-753br: a person's direct approval, with no --ref ---

def direct(trk, t, sha, verdict="approved", *flags):
    return trk("review", "record", t, verdict, "--sha", sha, "--note", "x", *flags, "--json")


def rated_red_ticket(trk, tmp_path):
    """red_ticket rated RED by `worker`, so the RED-read ask to michael is open: (path, head sha, T7, ask id)."""
    path, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    [ask] = asks_under(trk, t7)
    return path, sha, t7, ask["id"]


def test_a_direct_approval_needs_no_ref_and_answers_the_ask_at_the_head(trk, tmp_path):
    _, sha, t7, ask = rated_red_ticket(trk, tmp_path)
    merge(trk, t7, sha)
    write_brief(trk, t7)  # a direct approval needs the complete review-brief (tr-4bhay)
    since = head(trk)
    approved = ok(trk, "review", "record", t7, "approved", "--sha", sha, "--note", "x", "--as", "michael")
    row = next(e for e in trk("evidence", t7, "--json").json()["evidence"] if e["id"] == approved)
    assert (row["kind"], row["gate"], row["ref"], row["sha"], row["verdict"], row["actor"], row["counts"]) == (
        "human-review", "merged", f"direct:michael@{sha}", sha, "pass", "michael", True)
    answered = item(trk, ask)
    assert (answered["status"], answered["resolution"], answered["outcome"]) == (
        "closed", "done", f"Approved by michael at {sha[:7]}")
    rows = feed(trk, since)
    assert [r["change"] for r in rows] == ["evidence.added", "ask.answered"]
    # contract/changes.schema.json fixes proxy true via evidence; a direct approval keeps it (tr-753br ledger)
    assert (rows[1]["data"]["proxy"], rows[1]["data"]["via"], rows[1]["data"]["evidence"]) == (True, "evidence", approved)
    assert "human-review" not in needs(trk, t7)


def test_a_direct_review_stores_the_full_head_when_the_pr_row_holds_a_short_sha(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["contract/x.json"])
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", path)
    accept(trk, j)
    t = new_ticket(trk, j)
    pr(trk, t, sha[:7])
    run(trk, t, "built", sha[:7])
    ok(trk, "advance", t)
    ok(trk, "blast-radius", t, "--as", "worker")
    [ask] = asks_under(trk, t)
    write_brief(trk, t)
    approved = ok(trk, "review", "record", t, "approved", "--sha", sha[:7], "--note", "x", "--as", "michael")
    row = next(e for e in trk("evidence", t, "--json").json()["evidence"] if e["id"] == approved)
    assert (row["ref"], row["sha"], row["counts"]) == (f"direct:michael@{sha}", sha, True)
    assert item(trk, ask["id"])["outcome"] == f"Approved by michael at {sha[:7]}"


def test_a_direct_review_takes_a_short_sha_but_needs_one(trk, tmp_path):
    _, sha, t7, _ = rated_red_ticket(trk, tmp_path)
    assert trk("review", "record", t7, "approved", "--note", "x", "--as", "michael").code == 2
    write_brief(trk, t7)
    ok(trk, "review", "record", t7, "approved", "--sha", sha[:7], "--note", "x", "--as", "michael")
    row = trk("evidence", t7, "--json").json()["evidence"][-1]
    assert (row["ref"], row["sha"]) == (f"direct:michael@{sha}", sha)


def test_a_direct_review_by_an_agent_is_refused(trk, tmp_path):
    _, sha, t7, ask = rated_red_ticket(trk, tmp_path)
    before = head(trk)
    assert direct(trk, t7, sha, "approved", "--as", "worker").error()["rule"] == "approval-needs-person"
    assert head(trk) == before and item(trk, ask)["status"] == "open"


def test_a_direct_review_of_a_sha_that_is_not_the_head_is_refused(trk, tmp_path):
    path, sha, t7, ask = rated_red_ticket(trk, tmp_path)
    other = commit(path, "elsewhere")
    for bad in (other, sha[:7] + ("0" if sha[7] != "0" else "1")):
        result = direct(trk, t7, bad, "approved", "--as", "michael")
        assert (result.code, result.error()["rule"]) == (1, "not-head")
    assert item(trk, ask)["status"] == "open"


def test_a_direct_review_before_there_is_a_head_is_refused(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["contract/x.json"])
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", path)
    accept(trk, j)
    t = new_ticket(trk, j)
    assert direct(trk, t, sha, "approved", "--as", "michael").error()["rule"] == "not-head"


def test_a_direct_review_needs_a_red_rating_and_an_open_red_read_ask_for_the_writer(trk, tmp_path):
    _, sha, t7 = red_ticket(trk, tmp_path)
    assert direct(trk, t7, sha, "approved", "--as", "michael").error()["rule"] == "no-red-read"  # not rated yet
    ok(trk, "blast-radius", t7, "--as", "michael")  # rated by the person: no ask is opened for them
    assert asks_under(trk, t7) == []
    assert direct(trk, t7, sha, "approved", "--as", "michael").error()["rule"] == "no-red-read"
    assert direct(trk, t7, sha, "changes-requested", "--as", "michael").error()["rule"] == "no-red-read"
    assert "human-review" in needs(trk, t7)
    ok(trk, "actor", "add", "other", "--kind", "human")
    ok(trk, "blast-radius", t7, "--as", "worker")
    [ask] = asks_under(trk, t7)
    assert direct(trk, t7, sha, "approved", "--as", "other").error()["rule"] == "no-red-read"  # not the addressee
    assert item(trk, ask["id"])["status"] == "open"


def test_a_direct_ref_cannot_be_given_explicitly(trk, tmp_path):
    _, sha, t7, ask = rated_red_ticket(trk, tmp_path)
    forged = f"direct:michael@{sha}"
    assert direct(trk, t7, sha, "approved", "--ref", forged, "--as", "michael").error()["rule"] == "reserved-ref"
    assert trk("evidence", "add", t7, "--kind", "human-review", "--gate", "merged", "--ref", forged.upper(), "--sha",
               sha, "--verdict", "pass", "--note", "x", "--as", "michael", "--json").error()["rule"] == "reserved-ref"
    assert item(trk, ask)["status"] == "open"


def test_a_direct_changes_requested_leaves_the_ask_open_and_a_github_ref_works_as_before(trk, tmp_path):
    _, sha, t7, ask = rated_red_ticket(trk, tmp_path)
    failed = ok(trk, "review", "record", t7, "changes-requested", "--sha", sha, "--note", "no", "--as", "michael")
    rows = {e["id"]: e for e in trk("evidence", t7, "--json").json()["evidence"]}
    assert (rows[failed]["ref"], rows[failed]["verdict"]) == (f"direct:michael@{sha}", "fail")
    assert item(trk, ask)["status"] == "open"
    review(trk, t7, sha, "approved", "--as", "michael")
    assert item(trk, ask)["outcome"] == f"Approved on GitHub: {REVIEW}"


def test_evidence_add_human_review_also_answers_the_red_read_ask(trk, tmp_path):
    _, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    [ask] = asks_under(trk, t7)
    ok(trk, "evidence", "add", t7, "--kind", "human-review", "--gate", "merged", "--ref", REVIEW, "--sha", sha,
       "--verdict", "pass", "--note", "Approved.", "--as", "michael")
    assert item(trk, ask["id"])["status"] == "closed"


def test_an_approval_at_another_sha_leaves_the_ask_open(trk, tmp_path):
    path, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    [ask] = asks_under(trk, t7)
    base = commit(path, "elsewhere")  # a commit that is not the head
    review(trk, t7, base, "approved", "--as", "michael")
    assert item(trk, ask["id"])["status"] == "open"
    assert "human-review" in needs(trk, t7)


def test_a_green_or_yellow_rating_opens_no_ask_and_needs_no_human_review(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["src/a.py"])
    t = lane_ticket(trk, path, sha)
    assert ok(trk, "blast-radius", t, "--as", "worker") == "yellow"
    assert asks_under(trk, t) == []
    merge(trk, t, sha)
    assert "human-review" not in needs(trk, t)


# --- ticket AC 2: a self-addressed RED-read ask ---

def test_when_engineering_resolves_to_the_writer_no_ask_is_opened_and_human_review_still_applies(trk, tmp_path):
    _, sha, t7 = red_ticket(trk, tmp_path)
    result = trk("blast-radius", t7, "--as", "michael")
    assert (result.code, result.out.strip()) == (0, "red")
    assert "engineering" in result.err and "michael" in result.err
    assert asks_under(trk, t7) == []
    merge(trk, t7, sha)
    assert "human-review" in needs(trk, t7)
    review(trk, t7, sha, "approved", "--as", "michael")
    assert "human-review" not in needs(trk, t7)


# --- ticket AC 3: a fix moves the head ---

def test_a_fix_moving_the_head_needs_a_fresh_approval_at_the_new_head(trk, tmp_path):
    path, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    merge(trk, t7, sha)
    first = review(trk, t7, sha, "approved", "--as", "michael")
    assert "human-review" not in needs(trk, t7)

    fix = commit(path, "contract/y.json")
    pr(trk, t7, fix)
    run(trk, t7, "built", fix)
    ok(trk, "blast-radius", t7, "--as", "worker")
    merge(trk, t7, fix)
    reasons = {e["id"]: e["reason"] for e in trk("evidence", t7, "--json").json()["evidence"]}
    assert reasons[first] == "wrong-sha"
    assert "human-review" in needs(trk, t7)
    fresh = [a for a in asks_under(trk, t7) if a["status"] == "open"]
    assert len(fresh) == 1  # the first was answered, so the new RED rating opened another

    review(trk, t7, fix, "approved", "--as", "michael")
    assert "human-review" not in needs(trk, t7)
    assert [a["status"] for a in asks_under(trk, t7)] == ["closed", "closed"]


def test_re_rating_after_an_approval_at_the_same_head_opens_no_new_ask(trk, tmp_path):
    _, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    review(trk, t7, sha, "approved", "--as", "michael")
    ok(trk, "blast-radius", t7, "--as", "worker")
    assert [a["status"] for a in asks_under(trk, t7)] == ["closed"]


def test_a_red_rating_at_a_sha_that_is_not_the_head_opens_no_ask(trk, tmp_path):
    path, _, t7 = red_ticket(trk, tmp_path)
    later = commit(path, "contract/z.json")
    assert ok(trk, "blast-radius", t7, "--sha", later, "--as", "worker") == "red"
    assert asks_under(trk, t7) == []


# --- a re-rating at a new head keeps the open ask current ---

def test_ac1_a_re_rating_at_a_new_head_rewrites_the_open_asks_question_to_the_new_sha_and_red_files(trk, tmp_path):
    path, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    [ask] = asks_under(trk, t7)
    new = commit(path, "contract/y.json")
    pr(trk, t7, new)
    since = head(trk)
    ok(trk, "blast-radius", t7, "--as", "worker")
    [again] = asks_under(trk, t7)
    assert (again["id"], again["status"]) == (ask["id"], "open")
    question = ok(trk, "section", "get", ask["id"], "question")
    assert f"rated RED at {new}" in question and sha not in question
    assert "- `contract/x.json`" in question and "- `contract/y.json`" in question
    assert "item.updated" in [r["change"] for r in feed(trk, since)]


def test_ac2_a_re_rating_at_the_same_head_leaves_the_question_unchanged(trk, tmp_path):
    _, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    [ask] = asks_under(trk, t7)
    before = ok(trk, "section", "get", ask["id"], "question")
    since = head(trk)
    ok(trk, "blast-radius", t7, "--as", "worker")
    assert ok(trk, "section", "get", ask["id"], "question") == before
    assert "item.updated" not in [r["change"] for r in feed(trk, since)]


# --- a re-rating withdraws the RED-read asks that no longer apply (tr-8zevz) ---

def test_a_re_rating_to_green_withdraws_the_open_red_read_ask_and_unblocks_close_and_verified(trk, tmp_path):
    path, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    [ask] = asks_under(trk, t7)
    since = head(trk)
    new = land_and_extend(trk, path, t7, sha)
    since = head(trk)
    assert ok(trk, "blast-radius", t7, "--base", "main", "--as", "worker") == "green"
    stale = item(trk, ask["id"])
    assert (stale["status"], stale["resolution"]) == ("closed", "wont-do")
    assert stale["outcome"].startswith("withdrawn: re-rated GREEN at ") and new[:7] in stale["outcome"]
    rows = feed(trk, since)
    assert [r["change"] for r in rows] == ["evidence.added", "ask.withdrawn"]
    assert rows[1]["actor"] == "worker" and rows[1]["item"] == ask["id"]
    assert item(trk, t7)["open_asks"] == []


def test_a_re_rating_to_yellow_withdraws_the_open_red_read_ask(trk, tmp_path):
    path, _ = lane_repo(tmp_path, {".trk/blast-radius": "RED contract/**\nYELLOW docs/**\n"},
                        ["contract/x.json", "docs/y.md"])
    t7 = lane_ticket(trk, path, git(path, "rev-parse", "HEAD"))
    assert ok(trk, "blast-radius", t7, "--as", "worker") == "red"
    [ask] = asks_under(trk, t7)
    git(path, "branch", "-f", "main", "HEAD~1")  # the RED file lands on main; the PR keeps its YELLOW one
    assert ok(trk, "blast-radius", t7, "--base", "main", "--as", "worker") == "yellow"
    assert "re-rated YELLOW" in item(trk, ask["id"])["outcome"]
    assert item(trk, ask["id"])["status"] == "closed"


def test_a_re_rating_that_is_still_red_keeps_the_open_ask(trk, tmp_path):
    _, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    ok(trk, "blast-radius", t7, "--as", "worker")
    assert [a["status"] for a in asks_under(trk, t7)] == ["open"]


def test_a_re_rating_at_a_sha_that_is_not_the_head_withdraws_nothing(trk, tmp_path):
    path, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    git(path, "checkout", "-q", "-b", "elsewhere", "main")
    later = commit(path, "docs/z.md")  # a GREEN commit that is not the PR's head
    assert ok(trk, "blast-radius", t7, "--sha", later, "--as", "worker") == "green"
    assert [a["status"] for a in asks_under(trk, t7)] == ["open"]


def test_a_re_rating_withdraws_only_the_ask_of_a_role_with_no_RED_file_left(trk, tmp_path):
    _, t = role_ticket(trk, tmp_path, "RED:product **/accepted-rulings.json\nRED contract/**\n")
    path = str(tmp_path / "L")
    ok(trk, "blast-radius", t, "--as", "worker")
    by_role = {a["addressee_role"]: a["id"] for a in asks_under(trk, t)}
    assert sorted(by_role) == ["engineering", "product"]
    git(path, "branch", "-f", "main", "HEAD~1")  # the product file lands on main; the PR keeps contract/x.json
    assert ok(trk, "blast-radius", t, "--base", "main", "--as", "worker") == "red"
    assert item(trk, by_role["product"])["status"] == "closed"
    assert item(trk, by_role["engineering"])["status"] == "open"


def test_an_answered_ask_is_not_touched_by_a_re_rating(trk, tmp_path):
    path, sha, t7 = red_ticket(trk, tmp_path)
    ok(trk, "blast-radius", t7, "--as", "worker")
    [ask] = asks_under(trk, t7)
    merge(trk, t7, sha)
    review(trk, t7, sha, "approved", "--as", "michael")
    land_and_extend(trk, path, t7, sha)
    assert ok(trk, "blast-radius", t7, "--base", "main", "--as", "worker") == "green"
    assert (item(trk, ask["id"])["resolution"]) == "done"


# --- a RED rule names the role that reviews it ---

def role_ticket(trk, tmp_path, rules):
    """Ticket at Built changing `reg/accepted-rulings.json` and `contract/x.json`, with `worker` and the
    human `francis` (holding `product`) registered: (head sha, ticket)."""
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": rules}, ["reg/accepted-rulings.json", "contract/x.json"])
    t = lane_ticket(trk, path, sha)
    ok(trk, "actor", "add", "francis", "--kind", "human")
    ok(trk, "roles", "set", "product", "francis")
    return sha, t


def test_a_red_product_rule_routes_the_ask_and_the_gate_to_the_product_holder(trk, tmp_path):
    sha, t = role_ticket(trk, tmp_path, "RED:product **/accepted-rulings.json\nYELLOW contract/**\n")
    row = blast(trk, t, "--as", "worker")
    assert row["detail"]["lane"] == "red"
    [ask] = asks_under(trk, t)
    assert (ask["addressee_role"], ask["addressee"]) == ("product", "francis")
    assert "reg/accepted-rulings.json" in ok(trk, "section", "get", ask["id"], "question")
    assert "contract/x.json" not in ok(trk, "section", "get", ask["id"], "question")
    files = {f["path"]: f["roles"] for f in row["detail"]["files"]}
    assert files == {"contract/x.json": [], "reg/accepted-rulings.json": ["product"]}

    merge(trk, t, sha)
    review(trk, t, sha, "approved", "--as", "michael")
    [need] = [m for m in item(trk, t)["gate"]["missing"] if m["need"] == "human-review"]
    assert (need["role"], need["actor"]) == ("product", "francis")
    assert item(trk, ask["id"])["status"] == "open"

    review(trk, t, sha, "approved", "--as", "francis")
    assert "human-review" not in needs(trk, t)
    assert item(trk, ask["id"])["status"] == "closed"


def test_a_bare_red_rule_still_routes_to_engineering(trk, tmp_path):
    sha, t = role_ticket(trk, tmp_path, "RED **/accepted-rulings.json\n")
    ok(trk, "blast-radius", t, "--as", "worker")
    [ask] = asks_under(trk, t)
    assert (ask["addressee_role"], ask["addressee"]) == ("engineering", "michael")
    merge(trk, t, sha)
    review(trk, t, sha, "approved", "--as", "francis")
    assert "human-review" in needs(trk, t)
    review(trk, t, sha, "approved", "--as", "michael")
    assert "human-review" not in needs(trk, t)


def test_two_roles_on_one_ticket_each_get_an_ask_and_each_must_approve(trk, tmp_path):
    sha, t = role_ticket(trk, tmp_path, "RED:product **/accepted-rulings.json\nRED contract/**\n")
    ok(trk, "blast-radius", t, "--as", "worker")
    assert sorted(a["addressee_role"] for a in asks_under(trk, t)) == ["engineering", "product"]
    merge(trk, t, sha)
    review(trk, t, sha, "approved", "--as", "francis")
    assert [m["role"] for m in item(trk, t)["gate"]["missing"] if m["need"] == "human-review"] == ["engineering"]
    review(trk, t, sha, "approved", "--as", "michael")
    assert "human-review" not in needs(trk, t)
    assert {a["status"] for a in asks_under(trk, t)} == {"closed"}


def test_a_red_rule_naming_an_unknown_role_is_a_usage_error_naming_the_line(trk, tmp_path):
    _, t = role_ticket(trk, tmp_path, "# rules\nRED:nobody **/accepted-rulings.json\n")
    result = trk("blast-radius", t, "--as", "worker")
    assert result.code == 2 and "line 2" in result.err and "'nobody'" in result.err
    assert trk("evidence", t, "--json").json()["evidence"][-1]["kind"] != "blast-radius"


def test_a_rule_may_name_the_relative_parent_role(trk, tmp_path):
    _, t = role_ticket(trk, tmp_path, "RED:parent **/accepted-rulings.json\n")
    assert ok(trk, "blast-radius", t, "--as", "worker") == "red"
