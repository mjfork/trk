"""Blast radius, Merged and Verified (Stage gates, Decision ledger): `trk blast-radius` rating a ticket's
changed files and recording its lane, Merged for the GREEN lane, `advance --outcome` into Verified closing
the ticket `done`, and `close --resolution done` refused on tickets.

Every test runs against a real throwaway git repo R: `base` on main, the feature branch's head `head`, a later
main commit `other` that does not contain `head`, and `m`, main's merge of the feature branch."""

import pytest

from gated import DEST, accept, commit, git, resolve, section_file, ticket, walk_to_verified


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "R"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    shas = {"path": str(r), "base": commit(r, ".trk/blast-radius", "# no rules: every change rates green\n")}
    git(r, "checkout", "-q", "-b", "feature")
    shas["head"] = commit(r, "feature")
    git(r, "checkout", "-q", "main")
    shas["other"] = commit(r, "other")
    git(r, "merge", "-q", "--no-ff", "feature", "-m", "merge")
    shas["m"] = git(r, "rev-parse", "HEAD")
    return shas


BRIEF = "".join(f"## {h}\n\nText under {h}.\n\n" for h in (
    "Decision", "What changes and why", "Why it's RED", "Impact if wrong", "Evidence", "Rollback",
    "Decision criteria", "Where to look", "Recommendation"))


def write_brief(trk, t, text=BRIEF, *flags):
    """Write ticket `t`'s `review-brief` section (a RED ticket's audit needs it complete)."""
    return ok(trk, "section", "set", t, "review-brief", "--file", str(section_file("review-brief", text)), *flags)


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def item(trk, item_id):
    return trk("show", item_id, "--json").json()["item"]


def head(trk):
    return trk("changes", "--since", "0", "--json").json()["head"]


def feed(trk, since=0):
    return trk("changes", "--since", str(since), "--json").json()["changes"]


def listed(trk, item_id, *flags):
    return trk("evidence", item_id, *flags, "--json").json()["evidence"]


def refusal(trk, *argv, code=1):
    result = trk(*argv, "--json")
    assert result.code == code, (argv, result.out, result.err)
    return result.error()


@pytest.fixture
def world(trk, repo):
    """Program P and accepted project J on repo R."""
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", repo["path"])
    accept(trk, j)
    return {"P": p, "J": j, **repo}


def new_ticket(trk, parent, form="spec-bound", title="T"):
    return ok(trk, "new", "work-item", title, "--form", form, "--parent", parent, *ticket(form))


def add(trk, item_id, kind, gate, *flags, note="x"):
    return ok(trk, "evidence", "add", item_id, "--kind", kind, "--gate", gate, *flags, "--note", note)


def pr(trk, t, sha):
    return add(trk, t, "pr", "built", "--ref", "https://github.com/o/r/pull/1", "--sha", sha)


def run(trk, t, gate, sha, verdict="pass", *flags):
    return add(trk, t, "test-run", gate, "--ref", "run-1", "--sha", sha, "--verdict", verdict, *flags)


def merge(trk, t, sha):
    return add(trk, t, "merge", "merged", "--ref", "https://github.com/o/r/pull/1", "--sha", sha)


def built(trk, w, t, sha=None):
    pr(trk, t, sha or w["head"])
    run(trk, t, "built", sha or w["head"])
    ok(trk, "advance", t)


def blast(trk, t, *flags):
    result = trk("blast-radius", t, *flags, "--json")
    assert result.code == 0, (flags, result.err)
    return result.json()["evidence"]


# --- AC 4 of the spec: green lane, Merged, then Verified closing done ---

def test_ac4_a_green_ticket_merges_unreviewed_and_reaches_verified_closing_done(trk, world):
    t1 = new_ticket(trk, world["J"], title="T1")
    built(trk, world, t1)
    assert ok(trk, "blast-radius", t1, "--base", world["base"]) == "green"
    shown = item(trk, t1)
    assert shown["lane"] == "green"
    merge(trk, t1, world["m"])
    ok(trk, "advance", t1)
    assert item(trk, t1)["stage"] == "merged"

    stale = run(trk, t1, "verified", world["other"], "pass", "--criterion", "ac-1")
    assert {e["id"]: e["reason"] for e in listed(trk, t1)}[stale] == "wrong-sha"
    failed = run(trk, t1, "verified", world["m"], "fail", "--criterion", "ac-1")
    before = head(trk)
    error = refusal(trk, "advance", t1, "--outcome", "It works.")
    assert (error["rule"], error["gate"]) == ("gate-unmet", "verified")
    assert error["missing"] == [{"need": "criterion-pass", "criterion": "ac-1", "version": 1,
                                 "latest": {"id": failed, "kind": "test-run", "sha": world["m"], "verdict": "fail"}}]
    assert head(trk) == before

    run(trk, t1, "verified", world["m"], "pass", "--criterion", "ac-1")
    since = head(trk)
    ok(trk, "advance", t1, "--outcome", "It works.")
    shown = item(trk, t1)
    assert (shown["stage"], shown["status"], shown["resolution"], shown["outcome"]) == (
        "verified", "closed", "done", "It works.")
    rows = feed(trk, since)
    assert [r["change"] for r in rows] == ["stage.changed", "item.closed"]
    assert rows[0]["data"] == {"old": "merged", "new": "verified", "via": "advance"}
    assert rows[1]["data"] == {"resolution": "done", "outcome": "It works."}


def test_reaching_verified_is_refused_while_a_child_ask_is_open_then_closes_and_releases_the_claim(trk, world):
    t = new_ticket(trk, world["J"])
    ok(trk, "actor", "add", "worker", "--kind", "agent")
    ok(trk, "claim", t, "--session", "W", "--as", "worker")
    a = ok(trk, "ask", t, "--to", "product", "Which?", "--question", f"@{section_file('question')}", "--as", "worker")
    built(trk, world, t)
    ok(trk, "blast-radius", t, "--base", world["base"])
    merge(trk, t, world["m"])
    ok(trk, "advance", t)
    run(trk, t, "verified", world["m"], "pass", "--criterion", "ac-1")
    since = head(trk)
    refused = trk("advance", t, "--outcome", "Done.", "--json")
    assert refused.code == 1 and refused.error()["rule"] == "open-asks" and a in refused.error()["message"]
    assert feed(trk, since) == []
    ok(trk, "withdraw", a, "--reason", "not needed", "--as", "worker")
    since = head(trk)
    ok(trk, "advance", t, "--outcome", "Done.")
    assert [(r["change"], r["item"]) for r in feed(trk, since)] == [
        ("stage.changed", t), ("item.closed", t), ("claim.released", t)]


def test_verified_needs_every_live_criterion_and_not_retired_ones(trk, world):
    t = new_ticket(trk, world["J"])
    ok(trk, "criterion", "add", t, "Second.")
    ok(trk, "criterion", "add", t, "Third.")
    ok(trk, "criterion", "retire", t, "ac-3", "--reason", "Dropped.")
    built(trk, world, t)
    ok(trk, "blast-radius", t, "--base", world["base"])
    merge(trk, t, world["m"])
    ok(trk, "advance", t)
    run(trk, t, "verified", world["m"], "pass", "--criterion", "ac-1")
    assert item(trk, t)["gate"] == {"next": "verified", "missing": [
        {"need": "criterion-pass", "criterion": "ac-2", "version": 1, "latest": None}]}
    run(trk, t, "verified", world["m"], "pass", "--criterion", "ac-2")
    assert item(trk, t)["gate"] == {"next": "verified", "missing": []}
    ok(trk, "advance", t, "--outcome", "Done.")


def test_a_retracted_merge_leaves_verified_listing_merge(trk, world):
    t = new_ticket(trk, world["J"])
    built(trk, world, t)
    ok(trk, "blast-radius", t, "--base", world["base"])
    merged = merge(trk, t, world["m"])
    ok(trk, "advance", t)
    run(trk, t, "verified", world["m"], "pass", "--criterion", "ac-1")
    ok(trk, "evidence", "retract", t, merged, "--note", "Wrong merge.")
    error = refusal(trk, "advance", t, "--outcome", "Done.")
    assert error["missing"][0] == {"need": "merge"}
    assert item(trk, t)["gate"]["missing"] == error["missing"]


# --- AC 5 of the spec: closing a ticket done ---

def test_ac5_a_ticket_closes_done_only_through_verified_and_wont_do_at_any_stage(trk, world):
    t = new_ticket(trk, world["J"])
    before = head(trk)
    error = refusal(trk, "close", t, "--resolution", "done", "--outcome", "Done.")
    assert (error["rule"], error["item"]) == ("done-needs-verified", t)
    assert "advance" in error["message"]
    assert head(trk) == before
    k = ok(trk, "new", "project", "K", "--parent", world["P"])
    framed = new_ticket(trk, k, title="Framed")
    assert item(trk, framed)["stage"] == "framed"
    ok(trk, "close", framed, "--resolution", "wont-do", "--outcome", "Dropped.")
    shown = item(trk, framed)
    assert (shown["status"], shown["resolution"], shown["stage"]) == ("closed", "wont-do", "framed")


def test_decisions_still_close_done(trk, world):
    d = ok(trk, "new", "work-item", "D", "--form", "decision", "--subtype", "task", "--parent", world["J"])
    resolve(trk, d)
    ok(trk, "close", d, "--resolution", "done", "--outcome", "Decided.")


# --- AC 5 of this ticket: unverifiable shas and --outcome ---

def test_advancing_into_verified_without_an_outcome_is_a_usage_error_and_writes_nothing(trk, world):
    t = new_ticket(trk, world["J"])
    built(trk, world, t)
    ok(trk, "blast-radius", t, "--base", world["base"])
    merge(trk, t, world["m"])
    ok(trk, "advance", t)
    run(trk, t, "verified", world["m"], "pass", "--criterion", "ac-1")
    before = head(trk)
    assert trk("advance", t).code == 2
    assert head(trk) == before
    assert item(trk, t)["stage"] == "merged"


def test_an_outcome_goes_with_advancing_into_verified_only(trk, world):
    t = new_ticket(trk, world["J"])
    pr(trk, t, world["head"])
    run(trk, t, "built", world["head"])
    assert trk("advance", t, "--outcome", "Built.").code == 2
    assert item(trk, t)["stage"] == "specd"


def test_a_verified_pass_at_an_unknown_sha_is_sha_unverifiable(trk, world):
    t = new_ticket(trk, world["J"])
    built(trk, world, t)
    ok(trk, "blast-radius", t, "--base", world["base"])
    merge(trk, t, world["m"])
    ok(trk, "advance", t)
    unknown = run(trk, t, "verified", "deadbeef", "pass", "--criterion", "ac-1")
    assert {e["id"]: e["reason"] for e in listed(trk, t)}[unknown] == "sha-unverifiable"
    error = refusal(trk, "advance", t, "--outcome", "Done.")
    (need,) = error["missing"]
    assert need == {"need": "sha-unverifiable", "evidence": unknown, "sha": "deadbeef", "criterion": "ac-1",
                    "checkout": world["path"],
                    "hint": f"deadbeef is unknown to {world['path']}: run `git fetch` there, then retry"}


def test_blast_radius_at_an_unknown_sha_or_without_a_checkout_is_sha_unverifiable(trk, world, tmp_path):
    t = new_ticket(trk, world["J"])
    built(trk, world, t)
    before = head(trk)
    error = refusal(trk, "blast-radius", t, "--sha", "deadbeef")
    assert (error["rule"], error["item"]) == ("sha-unverifiable", t)
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    error = refusal(trk, "blast-radius", t, "--repo", str(plain))
    assert (error["rule"], error["item"]) == ("sha-unverifiable", t)
    assert head(trk) == before


def test_a_ticket_without_a_checkout_cannot_be_rated(trk, world):
    n = ok(trk, "new", "project", "N", "--parent", world["P"])
    accept(trk, n)
    t = new_ticket(trk, n)
    built(trk, world, t)
    assert refusal(trk, "blast-radius", t)["rule"] == "sha-unverifiable"


def test_blast_radius_refuses_non_tickets_closed_tickets_and_a_ticket_with_no_head(trk, world):
    assert trk("blast-radius", world["J"]).code == 2
    t = new_ticket(trk, world["J"])
    assert trk("blast-radius", t).code == 2  # no pr row, so no head sha, and no --sha
    ok(trk, "close", t, "--resolution", "wont-do", "--outcome", "Dropped.")
    assert refusal(trk, "blast-radius", t, "--sha", world["head"])["rule"] == "closed"


# --- AC 4 of this ticket: a fix moves the head ---

def test_a_fix_at_a_new_head_keeps_built_and_merged_needs_a_fresh_blast_radius(trk, world, repo):
    t = new_ticket(trk, world["J"])
    built(trk, world, t)
    ok(trk, "blast-radius", t, "--base", world["base"])
    assert item(trk, t)["lane"] == "green"
    git(repo["path"], "checkout", "-q", "feature")
    fix = commit(repo["path"], "fix")
    git(repo["path"], "checkout", "-q", "main")
    git(repo["path"], "merge", "-q", "--no-ff", "feature", "-m", "merge fix")
    merged = git(repo["path"], "rev-parse", "HEAD")
    pr(trk, t, fix)
    run(trk, t, "built", fix)
    merge(trk, t, merged)
    shown = item(trk, t)
    assert (shown["stage"], shown["lane"]) == ("built", None)
    assert shown["gate"] == {"next": "merged", "missing": [{"need": "blast-radius"}]}
    assert blast(trk, t)["sha"] == fix
    ok(trk, "advance", t)
    assert item(trk, t)["stage"] == "merged"


# --- AC 3 of this ticket: lanes ---

def lane_repo(tmp_path, base_files, feature_files, branch="main", rules="# no rules: every change rates green\n"):
    """A checkout on `branch` holding `base_files`, and a feature branch adding `feature_files`; returns
    (path, the feature head)."""
    r = tmp_path / "L"
    r.mkdir()
    git(r, "init", "-q", "-b", branch)
    commit(r, ".trk/blast-radius", rules) if rules is not None else commit(r, "base")
    for name, body in base_files.items():
        commit(r, name, body)
    git(r, "checkout", "-q", "-b", "feature")
    for name in feature_files:
        commit(r, name)
    return str(r), git(r, "rev-parse", "HEAD")


def lane_ticket(trk, path, sha):
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", path)
    accept(trk, j)
    t = new_ticket(trk, j)
    pr(trk, t, sha)
    run(trk, t, "built", sha)
    ok(trk, "advance", t)
    return t


RULES = "# risky paths\nYELLOW src/**\nYELLOW **/*.json\n\nRED contract/**\n"


def test_rules_rate_each_file_by_its_highest_matching_rule(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES},
                          ["src/deep/er/a.py", "contract/x/y.json", "README.md", "docs/z.json"])
    t = lane_ticket(trk, path, sha)
    since = head(trk)
    row = blast(trk, t)
    assert (row["kind"], row["gate"], row["sha"], row["verdict"]) == ("blast-radius", "merged", sha, None)
    assert row["detail"].pop("base")  # the rating's base, kept for the Merged gate to check again
    assert row["detail"] == {"lane": "red", "files": [
        {"path": "README.md", "colour": "green", "rule": None, "adr": None, "roles": []},
        {"path": "contract/x/y.json", "colour": "red", "rule": "RED contract/**", "adr": None, "roles": ["engineering"]},
        {"path": "docs/z.json", "colour": "yellow", "rule": "YELLOW **/*.json", "adr": None, "roles": []},
        {"path": "src/deep/er/a.py", "colour": "yellow", "rule": "YELLOW src/**", "adr": None, "roles": []},
    ]}
    assert [r["change"] for r in feed(trk, since)] == ["evidence.added"]
    assert item(trk, t)["lane"] == "red"


def test_a_repo_with_no_rules_file_is_refused_and_records_no_lane(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {}, ["src/a.py"], rules=None)
    t = lane_ticket(trk, path, sha)
    before = head(trk)
    error = refusal(trk, "blast-radius", t)
    assert (error["rule"], error["item"]) == ("no-blast-rules", t)
    assert path in error["message"] and ".trk/blast-radius" in error["message"]
    assert head(trk) == before
    assert item(trk, t)["lane"] is None


def test_a_repo_whose_rules_file_is_comments_only_still_rates_every_file_green(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {}, ["src/a.py", "contract/b.json"], rules="# deliberately all green\n")
    t = lane_ticket(trk, path, sha)
    assert ok(trk, "blast-radius", t) == "green"


def rules_edit_repo(tmp_path, base_rules, edited):
    """A checkout whose main holds `base_rules` and whose feature branch rewrites the rules file to `edited`;
    returns (path, the feature head, the base)."""
    r = tmp_path / "E"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    base = commit(r, ".trk/blast-radius", base_rules)
    git(r, "checkout", "-q", "-b", "feature")
    sha = commit(r, ".trk/blast-radius", edited)
    return str(r), sha, base


@pytest.mark.parametrize("edited", [
    "# the map\nRED .trk/blast-radius\nYELLOW src/**\n",
    "# the map\nYELLOW src/**\n",
    "# all green now\n",
    "",
], ids=["rule-kept", "own-rule-deleted", "comments-only", "emptied"])
def test_a_change_to_the_rules_file_rates_red_whatever_the_file_says_at_the_head(trk, tmp_path, edited):
    path, sha, base = rules_edit_repo(tmp_path, "RED .trk/blast-radius\nYELLOW src/**\n", edited)
    t = lane_ticket(trk, path, sha)
    row = blast(trk, t, "--base", base)
    assert row["detail"]["lane"] == "red"
    assert row["detail"]["files"] == [{"path": ".trk/blast-radius", "colour": "red", "rule": "RED .trk/blast-radius",
                                       "adr": None, "roles": ["engineering"]}]
    assert item(trk, t)["lane"] == "red"


def test_a_rules_file_with_no_rule_for_itself_still_rates_red_and_a_rule_naming_a_role_keeps_that_role(trk, tmp_path):
    path, sha, base = rules_edit_repo(tmp_path, "# nothing\n", "RED:product .trk/blast-radius\n")
    t = lane_ticket(trk, path, sha)
    files = blast(trk, t, "--base", base)["detail"]["files"]
    assert [(f["colour"], f["rule"]) for f in files] == [("red", "RED:product .trk/blast-radius")]
    assert files[0]["roles"] == ["product"]


def test_a_change_that_leaves_the_rules_file_alone_is_not_raised(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {}, ["src/a.py"], rules="# deliberately all green\n")
    t = lane_ticket(trk, path, sha)
    assert ok(trk, "blast-radius", t) == "green"


def test_a_yellow_or_red_ticket_is_refused_merged_with_need_audit(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["src/a.py"])
    t = lane_ticket(trk, path, sha)
    assert ok(trk, "blast-radius", t) == "yellow"
    merge(trk, t, sha)
    error = refusal(trk, "advance", t)
    assert error["missing"] == [{"need": "audit"}]
    assert item(trk, t)["gate"]["missing"] == error["missing"]


def test_an_adr_scope_makes_files_at_least_yellow_and_is_recorded(trk, tmp_path):
    adr = "# ADR 0007: Storage\n\nStatus: accepted\nScope: lib/*.py, store/**\n\nBody.\n"
    path, sha = lane_repo(tmp_path, {"docs/adr/0007-storage.md": adr, ".trk/blast-radius": "RED store/hot/**\n"},
                          ["lib/b.py", "lib/sub/c.py", "store/hot/d.sql", "store/e.sql"])
    t = lane_ticket(trk, path, sha)
    files = blast(trk, t)["detail"]["files"]
    assert files == [
        {"path": "lib/b.py", "colour": "yellow", "rule": None, "adr": "docs/adr/0007-storage.md", "roles": []},
        {"path": "lib/sub/c.py", "colour": "green", "rule": None, "adr": None, "roles": []},
        {"path": "store/e.sql", "colour": "yellow", "rule": None, "adr": "docs/adr/0007-storage.md", "roles": []},
        {"path": "store/hot/d.sql", "colour": "red", "rule": "RED store/hot/**", "adr": "docs/adr/0007-storage.md", "roles": ["engineering"]},
    ]


def test_with_no_rules_file_every_file_is_green(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {}, ["contract/x.json", "src/a.py"])
    t = lane_ticket(trk, path, sha)
    detail = blast(trk, t)["detail"]
    assert detail["lane"] == "green"
    assert {f["colour"] for f in detail["files"]} == {"green"}


def test_the_base_defaults_to_origin_head_then_main_then_master(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {}, ["one", "two"], branch="master")
    t = lane_ticket(trk, path, sha)
    assert [f["path"] for f in blast(trk, t)["detail"]["files"]] == ["one", "two"]
    first = git(path, "rev-parse", "HEAD~1")
    git(path, "branch", "main", first)
    assert [f["path"] for f in blast(trk, t)["detail"]["files"]] == ["two"]
    git(path, "update-ref", "refs/remotes/origin/trunk", sha)
    git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert refusal(trk, "blast-radius", t)["rule"] == "empty-diff"
    # origin/HEAD now names the PR's own commit: no branch point, so even an honest explicit base is refused
    assert refusal(trk, "blast-radius", t, "--base", "master")["rule"] == "base-hides-pr"


# --- tr-y05c7: rating after the merge ---

def test_rating_after_a_fast_forward_merge_is_refused_empty_diff_not_rated_green(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["src/a.py"])
    branched = git(path, "rev-parse", "main")
    t = lane_ticket(trk, path, sha)
    git(path, "checkout", "-q", "main")
    git(path, "merge", "-q", "--ff-only", "feature")
    merge(trk, t, sha)
    before = head(trk)
    error = refusal(trk, "blast-radius", t)
    assert (error["rule"], error["item"]) == ("empty-diff", t)
    assert "--base" in error["message"]
    assert head(trk) == before and item(trk, t)["lane"] is None
    # a fast-forward leaves nothing that shows where the PR began (the commits are main's own now): rate before merging
    error = refusal(trk, "blast-radius", t, "--base", branched)
    assert error["rule"] == "base-hides-pr" and "already holds the PR" in error["message"]
    assert head(trk) == before and item(trk, t)["lane"] is None


def test_rating_after_a_merge_commit_rates_against_its_first_parent(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["src/a.py"])
    t = lane_ticket(trk, path, sha)
    git(path, "checkout", "-q", "main")
    before_merge = commit(path, "later.txt")
    git(path, "merge", "-q", "--no-ff", "feature", "-m", "merge")
    merge(trk, t, git(path, "rev-parse", "HEAD"))
    row = blast(trk, t)
    assert [f["path"] for f in row["detail"]["files"]] == ["src/a.py"]
    assert row["detail"]["lane"] == "yellow" and row["note"].endswith(f"(against {before_merge})")


def test_an_explicit_base_at_the_rated_sha_hides_the_pr_and_is_refused(trk, tmp_path):
    """It rated an empty diff GREEN: what `--base <PR head>` did to any PR (tr-eec4a)."""
    path, sha = lane_repo(tmp_path, {}, ["one"])
    t = lane_ticket(trk, path, sha)
    assert refusal(trk, "blast-radius", t, "--base", sha)["rule"] == "base-hides-pr"


def test_an_unknown_base_is_refused(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {}, ["one"])
    t = lane_ticket(trk, path, sha)
    assert refusal(trk, "blast-radius", t, "--base", "nowhere")["rule"] == "sha-unverifiable"


def test_a_head_without_main_master_or_origin_head_needs_base(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {}, ["one"], branch="trunk")
    t = lane_ticket(trk, path, sha)
    assert trk("blast-radius", t).code == 2
    # and no ref shows where the PR began, so an explicit base is refused too (until `gh` can compare the PR's files)
    assert refusal(trk, "blast-radius", t, "--base", "trunk")["rule"] == "base-hides-pr"


# --- AC 6 of this ticket: the shared helper ---

def test_the_shared_helper_walks_tickets_of_both_forms_to_verified(trk, world):
    def run_ok(*argv):
        return ok(trk, *argv)

    k = ok(trk, "new", "project", "K", "--parent", world["P"])
    accept(trk, k)
    for form in ("spec-bound", "self-contained"):
        t = new_ticket(trk, k, form)
        walk_to_verified(run_ok, t, "Shipped.")
        shown = item(trk, t)
        assert (shown["stage"], shown["resolution"], shown["outcome"]) == ("verified", "done", "Shipped.")


def test_a_malformed_rules_line_is_refused_rather_than_rated_green(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": "RED contract/**\nORANGE src/**\n"}, ["src/a.py"])
    t = lane_ticket(trk, path, sha)
    result = trk("blast-radius", t, "--json")
    assert result.code == 2 and "line 2" in result.error()["message"]


def test_verified_lists_open_children_and_close_done_refuses_them_first(trk, world):
    t = new_ticket(trk, world["J"])
    child = ok(trk, "new", "work-item", "Sub", "--form", "decision", "--subtype", "task", "--parent", t)
    assert refusal(trk, "close", t, "--resolution", "done", "--outcome", "Done.")["rule"] == "open-children"
    built(trk, world, t)
    ok(trk, "blast-radius", t, "--base", world["base"])
    merge(trk, t, world["m"])
    ok(trk, "advance", t)
    run(trk, t, "verified", world["m"], "pass", "--criterion", "ac-1")
    error = refusal(trk, "advance", t, "--outcome", "Done.")
    assert error["missing"] == [{"need": "open-children", "children": [child]}]
    assert item(trk, t)["gate"]["missing"] == error["missing"]
    resolve(trk, child)
    ok(trk, "close", child, "--resolution", "done", "--outcome", "Decided.")
    ok(trk, "advance", t, "--outcome", "Done.")


# --- SENSITIVE rules: counts for a directory, never its file names ---

SENSITIVE_RULES = "SENSITIVE desktop/corpus/**\nRED desktop/corpus/models/**\nYELLOW **/*.json\n"
CORPUS = ["desktop/corpus/models/acme-model.json", "desktop/corpus/models/globex-line.json",
          "desktop/corpus/readme.txt", "src/a.py", "docs/z.json"]


def store_bytes(home):
    return b"".join(p.read_bytes() for p in sorted(home.rglob("*")) if p.is_file())


def test_a_sensitive_directory_stores_counts_per_colour_and_no_file_name(trk, tmp_path, trk_home):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": SENSITIVE_RULES}, CORPUS)
    t = lane_ticket(trk, path, sha)
    ok(trk, "actor", "add", "worker", "--kind", "agent")
    row = blast(trk, t, "--as", "worker")
    assert row["detail"].pop("base")  # the rating's base, kept for the Merged gate to check again
    assert row["detail"] == {"lane": "red", "files": [
        {"path": "docs/z.json", "colour": "yellow", "rule": "YELLOW **/*.json", "adr": None, "roles": []},
        {"path": "src/a.py", "colour": "green", "rule": None, "adr": None, "roles": []},
    ], "sensitive": [{"dir": "desktop/corpus/**", "red": 2, "yellow": 0, "green": 1,
                   "roles": {"engineering": 2}}]}
    assert row["note"].startswith("red: 2 red, 1 yellow, 2 green")
    stored = store_bytes(trk_home)
    for name in ("acme-model", "globex-line", "readme.txt", "desktop/corpus/models"):
        assert name.encode() not in stored
    [ask] = [c for c in trk("tree", t, "--all", "--json").json()["items"] if c["form"] == "ask"]
    question = ok(trk, "section", "get", ask["id"], "question")
    assert "2 red file(s) under `desktop/corpus/**`" in question and "acme-model" not in question


@pytest.mark.parametrize("changed, lane", [(["s/x/a", "s/y/b"], "red"), (["s/y/b"], "green")])
def test_colour_rules_still_apply_to_sensitive_paths(trk, tmp_path, changed, lane):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": "SENSITIVE s/**\nRED s/x/**\n"}, changed)
    assert blast(trk, lane_ticket(trk, path, sha))["detail"]["lane"] == lane


def test_a_repo_without_a_sensitive_line_stores_detail_as_before(trk, tmp_path):
    path, sha = lane_repo(tmp_path, {".trk/blast-radius": RULES}, ["README.md"])
    assert "sensitive" not in blast(trk, lane_ticket(trk, path, sha))["detail"]
