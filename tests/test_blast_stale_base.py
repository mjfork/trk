"""`trk blast-radius` rates exactly the PR's own changes (tr-xqea9): a ticket whose PR is known is rated against a
freshly fetched tip of the PR's base branch, and a rating whose changed files include files the PR's own diff
(`gh pr view --json files`) lacks is refused as `stale-base`, never recorded.

The fixture is a real case in miniature: a feature branch cut from develop at `old`, develop then gains
migration 0032, the branch merges it (the head), develop gains 0033, and the checkout's cached `origin/develop`
is still `old`. `gh` is a stub script named by `$TRK_GH`; the remote is a local bare repo, so no test touches
the network."""

import json
import stat
import sys

import pytest

from gated import git, commit
from test_blast_and_verified import blast, lane_ticket, ok, refusal

RULES = "# the migrations are the risky files\nRED backend/drizzle/**\n"
PR = "https://github.com/o/r/pull/1"


@pytest.fixture
def stale(tmp_path):
    """origin (bare), a seed clone that plays everyone else pushing to develop, and the builder's checkout W."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "develop")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "develop")
    git(seed, "remote", "add", "origin", str(origin))
    commit(seed, ".trk/blast-radius", RULES)
    old = commit(seed, "app.py")
    git(seed, "push", "-q", "origin", "develop")
    work = tmp_path / "W"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", "-b", "feature")
    commit(work, "feature.txt")
    commit(seed, "backend/drizzle/0032_add_jobs.sql")
    merged = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "-q", "origin", "develop")
    git(work, "fetch", "-q", "origin")
    git(work, "merge", "-q", "--no-ff", "origin/develop", "-m", "Merge develop")
    tip = commit(seed, "backend/drizzle/0033_add_accounts.sql")
    git(seed, "push", "-q", "origin", "develop")
    # the checkout never fetched since the branch point: origin/develop and origin/HEAD still name `old`
    git(work, "update-ref", "refs/remotes/origin/develop", old)
    return {"path": str(work), "head": git(work, "rev-parse", "HEAD"), "old": old, "merged": merged, "tip": tip,
            "origin": origin, "seed": seed}


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A stub `gh` named by $TRK_GH that answers `pr view <ref> --json …` from the dict `gh(...)` was last given."""
    answer = tmp_path / "gh-answer.json"
    script = tmp_path / "gh"
    script.write_text(f"#!{sys.executable}\n"
                      "import json, sys\n"
                      f"answer = json.load(open({str(answer)!r}))\n"
                      "if answer.get('fail'):\n"
                      "    sys.stderr.write(answer['fail'])\n"
                      "    sys.exit(1)\n"
                      "argv = sys.argv[1:]\n"
                      "if argv[:2] == ['pr', 'view']:\n"
                      "    assert argv[3:] == ['--json', 'baseRefName,headRefOid'], argv\n"
                      "    import subprocess\n"
                      "    here = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()\n"
                      "    print(json.dumps({'baseRefName': answer['base'], 'headRefOid': answer.get('head') or here}))\n"
                      "else:\n"
                      "    assert argv[:2] == ['api', '--paginate'] and argv[2] == answer['endpoint'], argv\n"
                      "    assert argv[3:] == ['--jq', '.[].filename | @json'], argv\n"
                      "    for path in answer['files']:\n"
                      "        print(json.dumps(path))\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("TRK_GH", str(script))

    def say(files=("feature.txt",), base="develop", **more):
        answer.write_text(json.dumps({"base": base, "files": list(files), "endpoint": "repos/o/r/pulls/1/files", **more}))

    say()
    return say


def lanes(row):
    return {f["path"]: f["colour"] for f in row["detail"]["files"]}


def test_a_stale_cached_base_is_refetched_so_only_the_prs_own_files_are_rated(trk, stale, gh):
    t = lane_ticket(trk, stale["path"], stale["head"])
    row = blast(trk, t)
    assert row["detail"]["lane"] == "green"
    assert lanes(row) == {"feature.txt": "green"}
    assert f"against origin/develop {stale['tip'][:9]}" in row["note"]


def test_a_base_that_pulls_in_files_the_pr_never_touched_is_refused_and_records_nothing(trk, stale, gh):
    """288609a83 against its old branch point: develop's migrations rated RED, and a false review ask opened."""
    t = lane_ticket(trk, stale["path"], stale["head"])
    error = refusal(trk, "blast-radius", t, "--base", stale["old"])
    assert error["rule"] == "stale-base" and error["item"] == t
    assert "backend/drizzle/0032_add_jobs.sql" in error["message"] and PR in error["message"]
    assert "feature.txt" not in error["message"]
    assert trk("evidence", t, "--json").json()["evidence"][-1]["kind"] != "blast-radius"
    assert trk("waiting", "--all", "--json").json()["asks"] == []
    assert trk("show", t, "--json").json()["item"]["lane"] is None


def test_a_correct_explicit_base_is_still_honoured(trk, stale, gh):
    t = lane_ticket(trk, stale["path"], stale["head"])
    git(stale["path"], "fetch", "-q", "origin")
    assert blast(trk, t, "--base", stale["tip"])["detail"]["lane"] == "green"


def test_the_prs_own_red_files_still_rate_red(trk, stale, gh):
    commit(stale["path"], "backend/drizzle/0034_mine.sql")
    head = git(stale["path"], "rev-parse", "HEAD")
    gh(files=("feature.txt", "backend/drizzle/0034_mine.sql"))
    t = lane_ticket(trk, stale["path"], head)
    row = blast(trk, t)
    assert row["detail"]["lane"] == "red"
    assert lanes(row) == {"backend/drizzle/0034_mine.sql": "red", "feature.txt": "green"}


def test_a_ticket_merged_by_a_merge_commit_is_rated_against_the_first_parent_as_before(trk, stale, gh):
    t = lane_ticket(trk, stale["path"], stale["head"])
    git(stale["seed"], "fetch", "-q", str(stale["path"]), "feature")
    git(stale["seed"], "merge", "-q", "--no-ff", stale["head"], "-m", "Merge PR 7")
    merge = git(stale["seed"], "rev-parse", "HEAD")
    git(stale["seed"], "push", "-q", "origin", "develop")
    git(stale["path"], "fetch", "-q", "origin")
    ok(trk, "evidence", "add", t, "--kind", "merge", "--gate", "merged", "--ref", PR, "--sha", merge, "--note", "m")
    assert blast(trk, t)["note"].endswith(f"(against {stale['tip']})")


def test_a_large_pr_is_checked_against_its_whole_file_list(trk, stale, gh):
    """`gh pr view --json files` stops at 100; the list is read with `gh api --paginate`, so file 101 counts."""
    for i in range(101):
        commit(stale["path"], f"bulk/{i:03}.txt")
    head = git(stale["path"], "rev-parse", "HEAD")
    t = lane_ticket(trk, stale["path"], head)
    gh(files=["feature.txt"] + [f"bulk/{i:03}.txt" for i in range(100)])  # file 101 missing
    error = refusal(trk, "blast-radius", t)
    assert error["rule"] == "stale-base" and "bulk/100.txt" in error["message"]
    gh(files=["feature.txt"] + [f"bulk/{i:03}.txt" for i in range(101)])
    assert len(blast(trk, t)["detail"]["files"]) == 102


def test_the_merge_row_of_an_earlier_pr_of_the_ticket_is_not_this_heads_pre_merge_point(trk, stale, gh):
    """tr-tk1tp rated 288609a83 against 182b69276, the first parent of the merge of PR 1687 recorded days before."""
    t = lane_ticket(trk, stale["path"], stale["head"])
    seed = stale["seed"]
    git(seed, "checkout", "-q", "-b", "earlier-pr", stale["old"])
    commit(seed, "earlier.txt")
    git(seed, "checkout", "-q", "-b", "oldline", stale["old"])
    commit(seed, "mainline.txt")
    git(seed, "merge", "-q", "--no-ff", "earlier-pr", "-m", "Merge earlier PR")
    git(seed, "push", "-q", "origin", "oldline")
    git(stale["path"], "fetch", "-q", "origin")
    earlier = git(seed, "rev-parse", "HEAD")
    ok(trk, "evidence", "add", t, "--kind", "merge", "--gate", "merged", "--ref", "1687", "--sha", earlier, "--note", "m")
    row = blast(trk, t)
    assert lanes(row) == {"feature.txt": "green"}
    assert f"against origin/develop {stale['tip'][:9]}" in row["note"]


def test_a_rename_is_not_a_file_the_pr_never_touched(trk, stale, gh):
    git(stale["path"], "mv", "app.py", "application.py")
    git(stale["path"], "commit", "-q", "-m", "rename")
    head = git(stale["path"], "rev-parse", "HEAD")
    gh(files=("feature.txt", "application.py"))
    t = lane_ticket(trk, stale["path"], head)
    assert lanes(blast(trk, t)) == {"app.py": "green", "application.py": "green", "feature.txt": "green"}


def test_a_prs_file_list_is_not_used_for_a_head_that_is_not_the_rated_sha_or_its_ancestor(trk, stale, gh):
    """An older head of a ticket with several PRs: today's file list is not the list of what is rated."""
    t = lane_ticket(trk, stale["path"], stale["head"])
    gh(files=("other.txt",), head=stale["tip"])  # the PR now sits on a commit that does not contain the rated head
    result = trk("blast-radius", t, "--json")
    assert result.code == 0 and result.json()["evidence"]["detail"]["lane"] == "green"
    assert "not used to check" in result.err and stale["tip"][:9] in result.err
    assert f"against origin/develop {stale['tip'][:9]}" in result.json()["evidence"]["note"]  # the fresh base still applies


def test_a_pr_whose_head_is_a_descendant_of_the_rated_sha_is_still_checked(trk, stale, gh):
    t = lane_ticket(trk, stale["path"], stale["head"])
    later = commit(stale["path"], "later.txt")
    gh(files=("feature.txt", "later.txt"), head=later)
    assert refusal(trk, "blast-radius", t, "--base", stale["old"])["rule"] == "stale-base"


def test_a_gh_that_cannot_read_the_pr_refuses_the_rating(trk, stale, gh):
    t = lane_ticket(trk, stale["path"], stale["head"])
    gh(fail="HTTP 404: Could not resolve to a PullRequest")
    error = refusal(trk, "blast-radius", t)
    assert error["rule"] == "sha-unverifiable" and "Could not resolve" in error["message"] and PR in error["message"]


def test_a_base_that_cannot_be_fetched_refuses_rather_than_using_the_cached_one(trk, stale, gh):
    t = lane_ticket(trk, stale["path"], stale["head"])
    git(stale["path"], "remote", "set-url", "origin", str(stale["origin"]) + ".gone")
    error = refusal(trk, "blast-radius", t)
    assert error["rule"] == "sha-unverifiable" and "develop" in error["message"] and "fetch" in error["message"]


def test_no_gh_on_the_machine_rates_as_before_and_says_the_pr_was_not_checked(trk, stale, monkeypatch, capsys):
    monkeypatch.setenv("TRK_GH", str(stale["origin"] / "no-gh-here"))
    t = lane_ticket(trk, stale["path"], stale["head"])
    result = trk("blast-radius", t, "--base", stale["old"], "--json")
    assert result.code == 0 and result.json()["evidence"]["detail"]["lane"] == "red"
    assert "gh" in result.err and "not checked" in result.err


@pytest.mark.parametrize("ref", ["none", "tr-xqea9", "run-1", "https://example.test/review/1"])
def test_a_ref_that_is_not_a_github_pr_is_never_looked_up(trk, stale, gh, monkeypatch, ref):
    t = lane_ticket(trk, stale["path"], stale["head"])
    ok(trk, "evidence", "add", t, "--kind", "pr", "--gate", "built", "--ref", ref, "--sha", stale["head"], "--note", "p")
    ok(trk, "evidence", "add", t, "--kind", "test-run", "--gate", "built", "--ref", "run-9", "--sha", stale["head"],
       "--verdict", "pass", "--note", "t")
    monkeypatch.setenv("TRK_GH", "/nonexistent/gh")
    result = trk("blast-radius", t, "--base", stale["old"], "--json")
    assert result.code == 0 and "not checked" not in result.err


def test_agents_md_step_7_tells_a_stacked_pr_to_pass_the_parents_head_as_base():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "docs" / "agents.md").read_text()
    step7 = text.split("7. **Rate the risk.**", 1)[1].split("8. **Merge**", 1)[0]
    assert "--base <commit you branched from>" not in step7
    assert "stacked on another open PR" in step7 and "--base <parent PR head sha>" in step7
    assert "before the parent" in step7
