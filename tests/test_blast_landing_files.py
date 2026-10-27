"""A late-rules rating lists the landing's own files only (tr-3qtnd): the rules come from the rated descendant's tree,
the file list from `pre...merge` and `base...head`, never `base...sha`, which also holds every later PR's files. A
review recorded at the merge sha counts for such a rating when it lists exactly that diff, and a plain fast-forward
merge can be rated late when `gh`'s file list proves the base.

The shape is a real case: PR B (`src/a.py`, head `head`) merged no-ff at `m`; PR C added the rules file
(`rules`); PR D added `src/later.py` (`later`)."""

import json
import stat
import sys

import pytest

from gated import DEST, accept, commit, git
from test_blast_and_verified import add, built, item, listed, merge, new_ticket, ok, refusal, write_brief
from test_blast_late_rules import RULES, early, late, merged_ticket, needs, rated  # noqa: F401  (fixtures)

PR_URL = "https://github.com/o/r/pull/1"


@pytest.fixture
def later(late):
    """The `late` repo with a third PR after the rules: `src/later.py` on main."""
    git(late["path"], "checkout", "-q", "main")
    return {**late, "later": commit(late["path"], "src/later.py")}


# --- ac-1: the file list is the landing's, not base...sha ---

def test_a_late_rating_lists_the_prs_files_and_not_a_later_prs(trk, later):
    t = merged_ticket(trk, later)
    lane, paths, counts = rated(trk, t, "--sha", later["later"], "--base", later["base"])
    assert (lane, paths, counts) == ("red", ["src/a.py"], True)
    assert "blast-radius" not in needs(trk, t)


def test_the_rules_file_a_later_pr_added_is_not_listed_but_its_rules_rate_the_files(trk, later):
    t = merged_ticket(trk, later)
    assert rated(trk, t, "--sha", later["rules"], "--base", later["base"]) == ("red", ["src/a.py"], True)
    git(later["path"], "checkout", "-q", "main")
    yellow = commit(later["path"], RULES, "YELLOW src/**\n")  # the rated tree's rules, not the merge's (it has none)
    assert rated(trk, t, "--sha", yellow, "--base", later["base"]) == ("yellow", ["src/a.py"], True)


def test_a_file_the_prs_landing_changed_stays_listed_after_a_later_pr_removed_it(trk, later):
    git(later["path"], "rm", "-q", "src/a.py")
    git(later["path"], "commit", "-q", "-m", "rm")
    removed = git(later["path"], "rev-parse", "HEAD")
    t = merged_ticket(trk, later)
    assert rated(trk, t, "--sha", removed, "--base", later["base"]) == ("red", ["src/a.py"], True)


# --- ac-2 and ac-3: a review at the landing sha counts for the rating that lists exactly the landing ---

def review_at(trk, t, sha, verdict="pass"):
    return add(trk, t, "human-review", "merged", "--ref", PR_URL, "--sha", sha, "--verdict", verdict, "--as", "michael")


def review_state(trk, t):
    return [(e["sha"], e["counts"], e["reason"]) for e in listed(trk, t) if e["kind"] == "human-review"]


def test_a_review_at_the_merge_sha_counts_once_the_late_rating_lists_exactly_the_landing(trk, later):
    t = merged_ticket(trk, later)
    review_at(trk, t, later["m"])
    assert review_state(trk, t) == [(later["m"], False, "wrong-sha")]  # no late rating yet: the head is the only sha
    assert rated(trk, t, "--sha", later["later"], "--base", later["base"]) == ("red", ["src/a.py"], True)
    assert review_state(trk, t) == [(later["m"], True, None)]
    assert "human-review" not in needs(trk, t)


def test_a_review_at_the_merge_sha_does_not_count_for_a_rating_that_lists_more_than_the_landing(trk, tmp_path):
    """main fast-forwarded to the PR's first commit before the merge, so `pre...merge` is only the second: the rating
    also lists `src/a.py` (from `base...head`), which the review at the merge never covered."""
    r = tmp_path / "Q"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "README.md")
    base = commit(r, "README2.md")
    git(r, "checkout", "-q", "-b", "f")
    commit(r, "src/a.py")
    head = commit(r, "docs/b.md")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--ff-only", "f~1")
    git(r, "merge", "-q", "--no-ff", "f", "-m", "merge f")
    m = git(r, "rev-parse", "HEAD")
    rules = commit(r, RULES, "RED src/**\n")
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", str(r))
    accept(trk, j)
    t = new_ticket(trk, j)
    built(trk, {"head": head}, t)
    merge(trk, t, m)
    review_at(trk, t, m)
    assert rated(trk, t, "--sha", rules, "--base", base) == ("red", ["docs/b.md", "src/a.py"], True)
    assert review_state(trk, t) == [(m, False, "wrong-sha")]
    assert "human-review" in needs(trk, t)


def test_a_review_at_another_descendant_never_counts_and_a_failing_review_at_the_merge_fails_the_need(trk, later):
    t = merged_ticket(trk, later)
    review_at(trk, t, later["rules"])
    review_at(trk, t, later["m"], "fail")
    rated(trk, t, "--sha", later["later"], "--base", later["base"])
    assert review_state(trk, t) == [(later["rules"], False, "wrong-sha"), (later["m"], True, None)]
    assert "human-review" in needs(trk, t)  # the only counting review fails


def test_a_review_at_the_head_still_counts_for_a_late_rating(trk, later):
    t = merged_ticket(trk, later)
    review_at(trk, t, later["head"])
    rated(trk, t, "--sha", later["later"], "--base", later["base"])
    assert review_state(trk, t) == [(later["head"], True, None)]
    assert "human-review" not in needs(trk, t)


def red_asks(trk):
    return trk("waiting", "--all", "--json").json()["asks"]


def test_no_red_read_ask_opens_when_a_review_of_the_same_landing_already_counts(trk, later):
    t = merged_ticket(trk, later)
    review_at(trk, t, later["m"])
    ok(trk, "blast-radius", t, "--sha", later["later"], "--base", later["base"], "--as", "worker")
    assert red_asks(trk) == []


def test_the_red_read_ask_still_opens_without_a_review_of_the_landing(trk, later):
    t = merged_ticket(trk, later)
    ok(trk, "blast-radius", t, "--sha", later["later"], "--base", later["base"], "--as", "worker")
    write_brief(trk, t)  # a RED-read ask stays held from `waiting` until the brief is complete (tr-4bhay)
    assert [a["parent"]["id"] for a in red_asks(trk)] == [t]


# --- ac-4: a plain fast-forward merge can be rated late, the base proven by gh's file list ---

@pytest.fixture
def ff(trk, tmp_path):
    """A two-commit PR (`src/a.py`, then `src/b.py`) fast-forwarded into main; the rules file arrives after it."""
    r = tmp_path / "FF"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    shas = {"path": str(r), "base": commit(r, "README.md")}
    git(r, "checkout", "-q", "-b", "f")
    shas["first"] = commit(r, "src/a.py")
    shas["head"] = commit(r, "src/b.py")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--ff-only", "f")
    shas["rules"] = commit(r, RULES, "RED src/**\n")
    shas["later"] = commit(r, "src/later.py")
    ok(trk, "init", "--me", "michael")
    ok(trk, "actor", "add", "worker", "--kind", "agent")
    p = ok(trk, "new", "program", "P", *DEST)
    shas["J"] = ok(trk, "new", "project", "J", "--parent", p, "--repo", str(r))
    accept(trk, shas["J"])
    return shas


@pytest.fixture
def gh(tmp_path, monkeypatch, ff):
    """A stub `gh` for the PR `PR_URL`: its base branch main, its head `ff['head']`, the files it is told."""
    answer = tmp_path / "gh-answer.json"
    script = tmp_path / "gh"
    script.write_text(f"#!{sys.executable}\n"
                      "import json, sys\n"
                      f"answer = json.load(open({str(answer)!r}))\n"
                      "argv = sys.argv[1:]\n"
                      "if argv[:2] == ['pr', 'view']:\n"
                      "    print(json.dumps({'baseRefName': 'main', 'headRefOid': answer['head']}))\n"
                      "elif argv[0] == 'api' and '/compare/' in argv[1]:\n"
                      "    if answer['compare'] is None:\n"
                      "        sys.stderr.write('gh: Not Found (HTTP 404)')\n"
                      "        sys.exit(1)\n"
                      "    print(answer['compare'])\n"
                      "else:\n"
                      "    for path in answer['files']:\n"
                      "        print(json.dumps(path))\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("TRK_GH", str(script))

    def say(files=("src/a.py", "src/b.py"), compare="behind"):
        answer.write_text(json.dumps({"head": ff["head"], "files": list(files), "compare": compare}))

    say()
    return say


def ff_ticket(trk, ff):
    t = new_ticket(trk, ff["J"])
    built(trk, ff, t)
    merge(trk, t, ff["head"])  # a fast-forward: the merge row is the head itself
    return t


def test_a_late_rating_after_a_fast_forward_merge_counts_when_gh_proves_the_base(trk, ff, gh):
    t = ff_ticket(trk, ff)
    assert rated(trk, t, "--sha", ff["later"], "--base", ff["base"]) == ("red", ["src/a.py", "src/b.py"], True)
    assert "blast-radius" not in needs(trk, t)


def test_a_late_rating_after_a_fast_forward_merge_is_refused_for_a_base_inside_the_pr(trk, ff, gh):
    t = ff_ticket(trk, ff)
    before = len(listed(trk, t))
    for hiding in (ff["first"], ff["head"]):
        assert rated(trk, t, "--sha", ff["later"], "--base", hiding)[0] == "refused"
    assert refusal(trk, "blast-radius", t, "--sha", ff["later"], "--base", ff["first"])["rule"] == "base-hides-pr"
    assert len(listed(trk, t)) == before


def test_a_late_rating_after_a_fast_forward_merge_is_refused_when_the_base_lists_files_the_pr_did_not_change(trk, ff, gh):
    t = ff_ticket(trk, ff)
    gh(files=("src/a.py",))  # a base before `README.md`'s commit would list more than gh does
    assert refusal(trk, "blast-radius", t, "--sha", ff["later"], "--base", ff["base"])["rule"] == "stale-base"


def test_a_late_rating_after_a_fast_forward_merge_needs_gh_to_prove_the_base(trk, ff):
    """No `gh` (the suite's default): the pr rows alone are the builder's word, so the base stays unproven."""
    t = ff_ticket(trk, ff)
    assert refusal(trk, "blast-radius", t, "--sha", ff["later"], "--base", ff["base"])["rule"] == "base-hides-pr"
    assert refusal(trk, "blast-radius", t, "--sha", ff["later"], "--base", ff["first"])["rule"] == "base-hides-pr"


# --- the rated sha must be on the branch the PR merged into (audit of 21a7289, ledger-4) ---

def scratch_commit(late, body="# all green\n"):
    """A commit the builder makes on top of the merge, off main, with permissive rules."""
    git(late["path"], "checkout", "-q", "-b", "scratch", late["m"])
    sha = commit(late["path"], RULES, body)
    git(late["path"], "checkout", "-q", "main")
    return sha


def test_a_scratch_commit_with_permissive_rules_cannot_carry_a_late_rating(trk, later):
    """No origin: the local default branch is the trusted one, and the scratch commit is not on it."""
    scratch = scratch_commit(later)
    t = merged_ticket(trk, later)
    before = len(listed(trk, t))
    error = refusal(trk, "blast-radius", t, "--sha", scratch, "--base", later["base"])
    assert error["rule"] == "sha-off-base" and scratch in error["message"]
    assert len(listed(trk, t)) == before and needs(trk, t) == ["blast-radius"]


@pytest.fixture
def origin(later, tmp_path):
    """The `later` repo with an origin: main pushed through `rules`, a side branch pushed too, and a local commit
    on main that was not pushed."""
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    path = later["path"]
    git(path, "remote", "add", "origin", str(bare))
    git(path, "push", "-q", "origin", f"{later['rules']}:refs/heads/main")
    scratch = scratch_commit(later)
    git(path, "push", "-q", "origin", f"{scratch}:refs/heads/side")
    git(path, "fetch", "-q", "origin")
    return {**later, "scratch": scratch}


def test_with_an_origin_a_late_rating_needs_a_commit_origins_default_branch_holds(trk, origin):
    t = merged_ticket(trk, origin)
    assert rated(trk, t, "--sha", origin["rules"], "--base", origin["base"]) == ("red", ["src/a.py"], True)
    assert refusal(trk, "blast-radius", t, "--sha", origin["later"], "--base", origin["base"])["rule"] == "sha-off-base"  # not pushed


def test_with_an_origin_a_side_branch_commit_on_origin_is_not_on_the_default_branch(trk, origin):
    t = merged_ticket(trk, origin)
    error = refusal(trk, "blast-radius", t, "--sha", origin["scratch"], "--base", origin["base"])
    assert error["rule"] == "sha-off-base" and "origin/" in error["message"]
    assert needs(trk, t) == ["blast-radius"]


def test_with_gh_github_places_the_rated_sha_on_the_base_branch(trk, ff, gh):
    t = ff_ticket(trk, ff)
    gh(compare="identical")
    assert rated(trk, t, "--sha", ff["later"], "--base", ff["base"])[::2] == ("red", True)


@pytest.mark.parametrize("status", ["ahead", "diverged"])
def test_with_gh_a_sha_github_puts_off_the_base_branch_is_refused_though_main_holds_it_here(trk, ff, gh, status):
    t = ff_ticket(trk, ff)
    gh(compare=status)  # the local main holds ff['later']; GitHub is the authority
    before = len(listed(trk, t))
    error = refusal(trk, "blast-radius", t, "--sha", ff["later"], "--base", ff["base"])
    assert error["rule"] == "sha-off-base" and status in error["message"]
    assert len(listed(trk, t)) == before


def test_with_gh_a_sha_github_cannot_place_is_unverifiable(trk, ff, gh):
    t = ff_ticket(trk, ff)
    gh(compare=None)  # a commit never pushed: 404
    assert refusal(trk, "blast-radius", t, "--sha", ff["later"], "--base", ff["base"])["rule"] == "sha-unverifiable"


# --- a rules file the merge already had, edited since, is listed at its RED floor (ledger-4) ---

def test_rules_weakened_after_the_merge_rate_red_and_the_rules_file_is_listed(trk, early):
    git(early["path"], "checkout", "-q", "main")
    weak = commit(early["path"], RULES, "# all green\n")
    t = merged_ticket(trk, early)
    assert rated(trk, t, "--sha", weak, "--base", early["rules"]) == ("red", [RULES, "src/a.py"], True)


def test_rules_untouched_since_the_merge_are_not_listed(trk, early):
    later = commit(early["path"], "docs/x.md")
    t = merged_ticket(trk, early)
    assert rated(trk, t, "--sha", later, "--base", early["rules"]) == ("red", ["src/a.py"], True)


def test_rules_that_arrive_after_the_merge_rate_the_prs_files_by_the_new_rules_and_are_not_listed(trk, later):
    t = merged_ticket(trk, later)
    assert rated(trk, t, "--sha", later["later"], "--base", later["base"]) == ("red", ["src/a.py"], True)


# --- a review at the landing covers only a rating whose own listing is the landing (audit, ledger-5) ---

def tamper(trk_home, evidence_id, edit):
    import sqlite3

    conn = sqlite3.connect(trk_home / "store.db")
    conn.execute("DROP TRIGGER evidence_no_update")  # the store is append-only; this test plays an older trk's row
    detail = json.loads(conn.execute("SELECT detail FROM evidence WHERE id = ?", (evidence_id,)).fetchone()[0])
    edit(detail)
    conn.execute("UPDATE evidence SET detail = ? WHERE id = ?", (json.dumps(detail), evidence_id))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("edit", [
    lambda d: d["files"].append({"path": ".trk/blast-radius", "colour": "red", "rule": "RED .trk/**", "adr": None,
                                 "roles": ["engineering"]}),
    lambda d: d.setdefault("sensitive", []).append({"dir": "x/**", "green": 0, "yellow": 0, "red": 1, "roles": {}}),
], ids=["extra-listed-file", "extra-sensitive-count"])
def test_a_rating_that_lists_more_than_the_landing_keeps_the_review_need(trk, trk_home, later, edit):
    """A row made before late ratings listed only the landing (tr-24v2e's, at 80bc707b) carries a later PR's files."""
    t = merged_ticket(trk, later)
    review_at(trk, t, later["m"])
    row = trk("blast-radius", t, "--sha", later["rules"], "--base", later["base"], "--json").json()["evidence"]
    assert review_state(trk, t) == [(later["m"], True, None)]
    tamper(trk_home, row["id"], edit)
    assert review_state(trk, t) == [(later["m"], False, "wrong-sha")]
    assert "human-review" in needs(trk, t)


# --- UP-0019: a short sha is stored in full, so `red_read.sha` meets its contract ---

def test_a_short_sha_on_a_red_rating_is_stored_in_full(trk, later):
    t = merged_ticket(trk, later)
    row = trk("blast-radius", t, "--sha", later["later"][:9], "--base", later["base"], "--json",
              "--as", "worker").json()["evidence"]
    assert row["sha"] == later["later"]
    write_brief(trk, t)
    [ask] = red_asks(trk)
    assert ask["red_read"]["sha"] == later["later"]


def test_a_short_red_rating_already_stored_reads_in_full(trk, later, trk_home):
    import sqlite3
    t = merged_ticket(trk, later)
    ok(trk, "blast-radius", t, "--sha", later["later"], "--base", later["base"], "--as", "worker")
    write_brief(trk, t)
    db = sqlite3.connect(trk_home / "store.db")
    with db:  # a row as an older trk stored it: lift the append-only trigger for this one write only
        db.execute("DROP TRIGGER evidence_no_update")
        db.execute("UPDATE evidence SET sha = ? WHERE kind = 'blast-radius'", (later["later"][:9],))
    db.close()
    [ask] = red_asks(trk)
    assert ask["red_read"]["sha"] == later["later"]
