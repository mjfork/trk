"""`trk blast-radius` rates an Azure DevOps pull request against the branch it targets, read with `az repos pr show`,
not against origin/HEAD, main or master. The PR is named by its link, or by a number in a checkout whose origin is
on Azure DevOps. `az` is a stub script named by `$TRK_AZ`; the remote is a local bare repo, so no test touches the
network. The fixture is test_blast_stale_base's: a feature branch off develop, whose cached origin/develop is stale."""

import json
import stat
import sys

import pytest

from gated import git
from test_blast_and_verified import DEST, accept, add, blast, new_ticket, ok, refusal, run
from test_blast_landing_files import later, origin  # noqa: F401 (fixtures)
from test_blast_late_rules import late, merged_ticket  # noqa: F401 (fixture)
from test_blast_stale_base import lanes, stale  # noqa: F401 (fixture)
from trk import azure

PR = "https://dev.azure.com/acme/Shop/_git/shop-web/pullrequest/42"


@pytest.fixture
def az(tmp_path, monkeypatch):
    """A stub `az` named by $TRK_AZ that answers `repos pr show --id 42 --org …` from the dict `az(...)` was last given."""
    answer = tmp_path / "az-answer.json"
    script = tmp_path / "az"
    script.write_text(f"#!{sys.executable}\n"
                      "import json, subprocess, sys\n"
                      f"answer = json.load(open({str(answer)!r}))\n"
                      "if answer.get('fail'):\n"
                      "    sys.stderr.write(answer['fail'])\n"
                      "    sys.exit(1)\n"
                      "argv = sys.argv[1:]\n"
                      "assert argv == ['repos', 'pr', 'show', '--id', '42', '--org', answer['org'], '--output', 'json'], argv\n"
                      "here = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()\n"
                      "print(json.dumps({'targetRefName': 'refs/heads/' + answer['base'],"
                      " 'lastMergeSourceCommit': {'commitId': answer.get('head') or here}}))\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("TRK_AZ", str(script))

    def say(base="develop", org="https://dev.azure.com/acme", **more):
        answer.write_text(json.dumps({"base": base, "org": org, **more}))

    say()
    return say


def azure_ticket(trk, path, sha, ref=PR):
    ok(trk, "init", "--me", "michael")
    p = ok(trk, "new", "program", "P", *DEST)
    j = ok(trk, "new", "project", "J", "--parent", p, "--repo", path)
    accept(trk, j)
    t = new_ticket(trk, j)
    add(trk, t, "pr", "built", "--ref", ref, "--sha", sha)
    run(trk, t, "built", sha)
    ok(trk, "advance", t)
    return t


def test_an_azure_pr_is_rated_against_the_branch_it_targets_not_main(trk, stale, az):
    git(stale["path"], "branch", "-q", "main", stale["old"])  # a main the old code fell back to
    t = azure_ticket(trk, stale["path"], stale["head"])
    row = blast(trk, t)
    assert f"against origin/develop {stale['tip'][:9]}" in row["note"]
    assert lanes(row) == {"feature.txt": "green"}


def test_the_target_is_fetched_fresh_so_a_stale_cached_copy_rates_nothing_extra(trk, stale, az):
    """origin/develop in the checkout is still the branch point: rated against it, develop's 0032 migration (merged
    into the branch) would rate RED as if the PR made it."""
    t = azure_ticket(trk, stale["path"], stale["head"])
    row = blast(trk, t)
    assert row["detail"]["lane"] == "green"
    assert not any(path.startswith("backend/drizzle/") for path in lanes(row))


def test_a_release_branch_target_is_used(trk, stale, az):
    git(stale["seed"], "push", "-q", "origin", f"{stale['merged']}:refs/heads/release/1.2")
    az(base="release/1.2")
    t = azure_ticket(trk, stale["path"], stale["head"])
    assert f"against origin/release/1.2 {stale['merged'][:9]}" in blast(trk, t)["note"]


def test_without_az_the_rating_goes_on_with_a_note(trk, stale, monkeypatch):
    t = azure_ticket(trk, stale["path"], stale["head"])
    result = trk("blast-radius", t, "--json")
    assert result.code == 0, result.err
    assert "az is not installed" in result.out + result.err


def test_an_az_failure_refuses_and_records_nothing(trk, stale, az):
    az(fail="ERROR: TF401180: The requested pull request was not found.")
    t = azure_ticket(trk, stale["path"], stale["head"])
    error = refusal(trk, "blast-radius", t)
    assert error["rule"] == "sha-unverifiable" and "TF401180" in error["message"]
    assert trk("evidence", t, "--json").json()["evidence"][-1]["kind"] != "blast-radius"


@pytest.mark.parametrize("ref, org", [
    (PR, "https://dev.azure.com/acme"),
    ("https://acme@dev.azure.com/acme/Shop/_git/shop-web/pullrequest/42?_a=files", "https://dev.azure.com/acme"),
    ("https://acme.visualstudio.com/Shop/_git/shop-web/pullrequest/42", "https://acme.visualstudio.com"),
    ("https://acme.visualstudio.com/DefaultCollection/Shop/_git/shop-web/pullrequest/42", "https://acme.visualstudio.com"),
])
def test_azure_pr_links_name_their_organization(ref, org):
    assert azure.pr_target(None, ref) == (org, "42")


@pytest.mark.parametrize("url, org", [
    ("https://acme@dev.azure.com/acme/Shop/_git/shop-web", "https://dev.azure.com/acme"),
    ("git@ssh.dev.azure.com:v3/acme/Shop/shop-web", "https://dev.azure.com/acme"),
    ("https://acme.visualstudio.com/Shop/_git/shop-web", "https://acme.visualstudio.com"),
    ("acme@vs-ssh.visualstudio.com:v3/acme/Shop/shop-web", "https://dev.azure.com/acme"),
    ("git@github.com:o/r.git", None),
])
def test_a_bare_number_is_an_azure_pr_only_in_an_azure_checkout(tmp_path, url, org):
    git(tmp_path, "init", "-q")
    git(tmp_path, "remote", "add", "origin", url)
    assert azure.pr_target(str(tmp_path), "#42") == (None if org is None else (org, "42"))


def test_github_and_branch_refs_are_not_azure():
    assert azure.pr_target(None, "https://github.com/o/r/pull/1") is None
    assert azure.pr_target(None, "feature/x") is None
    assert azure.pr_target(None, "42") is None  # no checkout to say which host a bare number is on


# --- a late rating's commit must be on the branch the Azure PR targets, not on main ---

def test_a_late_rating_is_placed_on_the_azure_prs_target_branch(trk, origin, az):
    """`scratch` is on origin/side, not origin/main: allowed for a PR that targets side, refused for one that
    targets main."""
    t = merged_ticket(trk, origin)
    add(trk, t, "pr", "built", "--ref", PR, "--sha", origin["head"])
    az(base="side", head=origin["head"])
    assert blast(trk, t, "--sha", origin["scratch"], "--base", origin["base"])["sha"] == origin["scratch"]
    az(base="main", head=origin["head"])
    error = refusal(trk, "blast-radius", t, "--sha", origin["scratch"], "--base", origin["base"])
    assert error["rule"] == "sha-off-base" and "origin/main" in error["message"]
