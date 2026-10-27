"""A program's close is harder to meet than a project's (tr-43mme): a `judgment` never counts, a run needs a release
sha as well as a ref, and a `human-accept` counts only from the acceptor or role the criterion names. A run is also
read against GitHub when the program closes (ledger-5): it must have concluded success, with the configured job
success, at the recorded release sha. Projects and tickets keep their gates. `gh` is a stub named by $TRK_GH."""

import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from gated import DEST, accepting, closable, run_all

HERE = Path(__file__).parent

SHA = "a" * 40
RUN = "https://github.com/o/r/actions/runs/77"
JOB = "verify-deployed-release"


def tag(sha=SHA):
    return {"type": "commit", "sha": sha}


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A stub `gh` (tests/fake_gh.py) that answers from what `say(...)` was last given and logs each call. `say` takes the
    run's `conclusion`, `head`, `jobs`, `branch` and `event`; `default_branch`; `tags` ({name: {"type": "commit", "sha"}}
    or an annotated {"type": "tag", "sha": <tag object>, "commit": <sha>}), `branches` (names), `rules` ({branch: [rule
    types], "*": the rest}), `tag_rulesets` ([{"target", "enforcement", "include", "exclude", "types"}]), `classic`
    ({branch: protection}); and `*_fail` texts that make that call fail. By default every ref is protected."""
    answer, log, script = tmp_path / "gh-answer.json", tmp_path / "gh-calls.log", tmp_path / "gh"
    script.write_text(f"#!{sys.executable}\nimport runpy, sys\nrunpy.run_path({str(HERE / 'fake_gh.py')!r}, run_name='__main__')\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("TRK_GH", str(script))
    monkeypatch.setenv("FAKE_GH_ANSWER", str(answer))
    monkeypatch.setenv("FAKE_GH_LOG", str(log))

    def say(conclusion="success", head=SHA, jobs=((JOB, "success"),), branch="main", event="push",
            default_branch="main", **more):
        answer.write_text(json.dumps({"conclusion": conclusion, "headSha": head, "headBranch": branch, "event": event,
                                      "jobs": [{"name": n, "conclusion": c} for n, c in jobs],
                                      "default_branch": default_branch,
                                      "tag_rulesets": [{"target": "tag", "enforcement": "active", "include": ["~ALL"],
                                                        "types": ["creation", "update"]}], **more}))

    say.calls = lambda: log.read_text().splitlines() if log.exists() else []
    say()
    return say


def run_row(trk, world, ref=RUN, sha=SHA, kind="test-run"):
    return add(trk, world["P"], kind, "--ref", ref, "--sha", sha, "--verdict", "pass")


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def refusal(trk, *argv):
    result = trk(*argv, "--json")
    assert result.code == 1, (argv, result.out, result.err)
    error = result.error()
    assert error["rule"] == "gate-unmet"
    return error


def close(trk, item_id):
    return trk("close", item_id, "--resolution", "done", "--outcome", "Arrived.", "--json")


def add(trk, item_id, kind, *flags, actor="worker"):
    return ok(trk, "evidence", "add", item_id, "--kind", kind, "--gate", "close", "--criterion", "ac-1",
              *flags, "--note", "Recorded.", "--as", actor)


def start(trk):
    """A store with a human `michael` and an agent `worker`, for the tests that make their own program."""
    ok(trk, "init", "--me", "michael")


def checkout(tmp_path, name, origin="https://github.com/o/r.git"):
    """A git checkout under tmp_path whose `origin` is `origin` (none when it is None); its path."""
    path = tmp_path / name
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return str(path)


@pytest.fixture
def world(trk, tmp_path):
    ok(trk, "init", "--me", "michael")
    ok(trk, "actor", "add", "kim", "--kind", "human")
    ok(trk, "roles", "set", "security", "kim")
    return {"P": ok(trk, "new", "program", "P", *DEST, "--repo", checkout(tmp_path, "r"))}


def named(trk, program, text):
    ok(trk, "criterion", "edit", program, "ac-1", text)


def test_a_judgment_does_not_close_a_program(trk, world):
    add(trk, world["P"], "judgment")
    error = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")
    (need,) = error["missing"]
    assert need["need"] == "criterion-pass" and need["criterion"] == "ac-1"
    assert [(u["kind"], u["reason"]) for u in need["uncounted"]] == [("judgment", "program-judgment")]
    listed = trk("evidence", world["P"], "--json").json()["evidence"]
    assert [(e["counts"], e["reason"]) for e in listed] == [(False, "program-judgment")]


def test_a_project_still_closes_on_a_judgment(trk, world):
    project = ok(trk, "new", "project", "K", "--parent", world["P"])
    ok(trk, "close", project, "--resolution", "wont-do", "--outcome", "Dropped.")
    other = ok(trk, "new", "project", "J", "--parent", world["P"])
    run_all(trk, [*accepting(other), *closable(other)])
    ok(trk, "close", other, "--resolution", "done", "--outcome", "Delivered.")


def test_a_run_without_a_release_sha_does_not_close_a_program(trk, world):
    for kind in ("test-run", "webtester-run"):
        add(trk, world["P"], kind, "--ref", "run-1", "--verdict", "pass")
        error = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")
        assert error["missing"][0]["uncounted"][-1]["reason"] == "no-release-sha"


def test_a_run_with_a_ref_and_a_release_sha_closes_a_program(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0
    assert gh.calls() == ["run view 77 -R o/r --json conclusion,event,headBranch,headSha,jobs",
                          "repo view o/r --json defaultBranchRef", "api repos/o/r/rules/branches/main",
                          "api repos/o/r/rulesets/100"]


def test_a_later_run_with_a_sha_replaces_the_refused_one(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    add(trk, world["P"], "test-run", "--ref", RUN, "--verdict", "pass")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_a_webtester_run_is_read_the_same_way(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(conclusion="failure")
    run_row(trk, world, kind="webtester-run")
    assert refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"][0]["reason"] \
        == "run-not-success"


def test_close_needs_the_release_verification_job_configured(trk, world, gh):
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert need["need"] == "release-verify-job" and "config set release-verify-job" in need["hint"]
    assert gh.calls() == []


@pytest.mark.parametrize("seen, reason", [
    ({"conclusion": "failure"}, "run-not-success"),
    ({"conclusion": None}, "run-not-success"),
    ({"jobs": [(JOB, "skipped")]}, "job-not-success"),
    ({"jobs": [(JOB, "failure")]}, "job-not-success"),
    ({"jobs": [("deploy", "success")]}, "job-not-success"),
    ({"head": "b" * 40}, "run-wrong-sha"),
])
def test_a_run_that_does_not_prove_the_release_is_refused(trk, world, gh, seen, reason):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(**seen)
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["criterion"], need["reason"]) == ("release-run", "ac-1", reason)


def test_an_abbreviated_release_sha_matches_the_runs_head(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world, sha=SHA[:12])
    assert close(trk, world["P"]).code == 0


def test_a_run_gh_cannot_read_is_sha_unverifiable(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world)
    gh(fail="HTTP 404: Not Found")
    result = trk("close", world["P"], "--resolution", "done", "--outcome", "Arrived.", "--json")
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"
    assert "HTTP 404" in result.error()["message"]


def test_a_ref_that_is_not_a_run_url_is_sha_unverifiable(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world, ref="release-run")
    result = trk("close", world["P"], "--resolution", "done", "--outcome", "Arrived.", "--json")
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"
    assert gh.calls() == []


def test_a_missing_gh_is_sha_unverifiable(trk, world):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world)
    result = trk("close", world["P"], "--resolution", "done", "--outcome", "Arrived.", "--json")
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"


def test_reading_a_program_never_calls_gh(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world)
    ok(trk, "show", world["P"], "--json")
    ok(trk, "evidence", world["P"])
    assert gh.calls() == []


def test_human_accept_from_a_human_the_criterion_does_not_name_is_refused(trk, world):
    named(trk, world["P"], "Every customer is served by the shared deployment. accept-by: michael")
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="kim")
    error = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")
    assert error["missing"][0]["uncounted"][0]["reason"] == "not-acceptor"


def test_human_accept_from_the_named_acceptor_closes_a_program(trk, world):
    named(trk, world["P"], "Every customer is served by the shared deployment. accept-by: michael")
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="michael")
    assert close(trk, world["P"]).code == 0


def test_human_accept_from_the_holder_of_a_named_role_closes_a_program(trk, world):
    named(trk, world["P"], "No tenant reads another's data. accept-by: security")
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="michael")
    assert close(trk, world["P"]).code == 1
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="kim")
    assert close(trk, world["P"]).code == 0


def test_a_role_reassigned_after_the_row_moves_the_acceptor(trk, world):
    named(trk, world["P"], "No tenant reads another's data. accept-by: security")
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="kim")
    ok(trk, "roles", "set", "security", "michael")
    assert close(trk, world["P"]).code == 1


def test_human_accept_on_a_criterion_that_names_nobody_is_refused(trk, world):
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="michael")
    error = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")
    assert error["missing"][0]["uncounted"][0]["reason"] == "no-acceptor-named"


def test_an_agent_named_as_acceptor_does_not_count(trk, world):
    named(trk, world["P"], "It holds. accept-by: worker")
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="worker")
    assert close(trk, world["P"]).code == 1


def test_editing_the_acceptor_out_of_the_text_stales_the_row(trk, world):
    named(trk, world["P"], "It holds. accept-by: michael")
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="michael")
    named(trk, world["P"], "It holds, differently.")
    assert close(trk, world["P"]).code == 1


def test_a_criterion_names_one_acceptor_only(trk, world):
    named(trk, world["P"], "It holds. accept-by: michael, kim")
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="kim")
    assert close(trk, world["P"]).code == 1
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="michael")
    assert close(trk, world["P"]).code == 0


# --- the run belongs to the program (ledger-8) ---


def test_a_run_of_another_repository_is_refused(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world, ref="https://github.com/x/other/actions/runs/77")
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["criterion"], need["reason"]) == ("release-run", "ac-1", "run-wrong-repo")
    assert "x/other" in need["detail"] and "o/r" in need["detail"]
    assert gh.calls() == []


def test_the_run_repository_is_compared_without_case(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world, ref="https://github.com/O/R/actions/runs/77")
    assert close(trk, world["P"]).code == 0


def test_a_run_of_any_of_the_programs_repos_counts(trk, tmp_path, gh):
    start(trk)
    program = ok(trk, "new", "program", "P", *DEST, "--repo", checkout(tmp_path, "a", "git@github.com:x/a.git"),
                 "--repo", checkout(tmp_path, "b", "https://github.com/o/r"))
    ok(trk, "config", "set", "release-verify-job", JOB)
    add(trk, program, "test-run", "--ref", RUN, "--sha", SHA, "--verdict", "pass")
    assert close(trk, program).code == 0


def test_a_program_with_no_repo_cannot_have_its_run_checked(trk, gh):
    start(trk)
    program = ok(trk, "new", "program", "P", *DEST)
    ok(trk, "config", "set", "release-verify-job", JOB)
    add(trk, program, "test-run", "--ref", RUN, "--sha", SHA, "--verdict", "pass")
    result = close(trk, program)
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"
    assert "no local checkout" in result.error()["message"] and gh.calls() == []


def test_a_program_whose_repo_has_no_github_origin_cannot_have_its_run_checked(trk, tmp_path, gh):
    start(trk)
    program = ok(trk, "new", "program", "P", *DEST, "--repo", checkout(tmp_path, "local", origin=None))
    ok(trk, "config", "set", "release-verify-job", JOB)
    add(trk, program, "test-run", "--ref", RUN, "--sha", SHA, "--verdict", "pass")
    result = close(trk, program)
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable" and gh.calls() == []


# --- the program names its own verification job (ledger-9) ---


def notes(trk, tmp_path, program, text, by=()):
    path = tmp_path / "notes.md"
    path.write_text(text)
    ok(trk, "section", "set", program, "notes", "--file", str(path), *by)


def test_a_program_names_its_own_release_verification_job(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"The map.\n\nrelease-verify-job: {JOB}\n")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_the_programs_job_wins_over_the_store_wide_one(trk, world, tmp_path, gh):
    ok(trk, "config", "set", "release-verify-job", "some-other-job")
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_the_programs_job_must_have_passed_even_when_the_store_wide_one_did(trk, world, tmp_path, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    notes(trk, tmp_path, world["P"], "release-verify-job: deploy-check\n")
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["reason"]) == ("release-run", "job-not-success") and "deploy-check" in need["detail"]


def test_notes_without_a_job_line_fall_back_to_the_store_wide_job(trk, world, tmp_path, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    notes(trk, tmp_path, world["P"], "Nothing about a job here.\n")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_a_program_with_neither_job_is_told_where_to_name_one(trk, world, gh):
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert need["need"] == "release-verify-job" and "notes" in need["hint"]


# --- show does not call the close met while a run is unread (no gh from show) ---


def gate_of(trk, item_id):
    shown = ok(trk, "show", item_id, "--json")
    return json.loads(shown)["item"]["gate"]


def test_show_says_the_run_check_is_pending_while_a_deciding_run_exists(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world)
    gate = gate_of(trk, world["P"])
    assert [m["need"] for m in gate["missing"]] == ["release-run-check"]
    assert gh.calls() == []


def test_a_pending_run_check_does_not_stop_the_close_when_the_run_proves_the_release(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world)
    assert gate_of(trk, world["P"])["missing"]
    assert close(trk, world["P"]).code == 0
    assert [c.split()[0] for c in gh.calls()] == ["run", "repo", "api", "api"]


def test_show_has_no_pending_run_check_when_no_run_decides(trk, world, gh):
    named(trk, world["P"], "It holds. accept-by: michael")
    add(trk, world["P"], "human-accept", "--ref", "page", "--verdict", "pass", actor="michael")
    assert gate_of(trk, world["P"])["missing"] == []
    assert gh.calls() == []


def test_a_project_has_no_pending_run_check(trk, world, gh):
    project = ok(trk, "new", "project", "J", "--parent", world["P"], *DEST)
    ok(trk, "evidence", "add", project, "--kind", "test-run", "--gate", "close", "--criterion", "ac-1",
       "--ref", "x", "--verdict", "pass", "--note", "Ran.")
    assert "release-run-check" not in [m["need"] for m in gate_of(trk, project)["missing"]]


# --- the notes line is read leniently, and an empty one is a need (ledger-13) ---


@pytest.mark.parametrize("line", [
    f"release-verify-job: {JOB}",
    f"- release-verify-job: {JOB}",
    f"* `release-verify-job`: {JOB}",
    f"Release-Verify-Job: {JOB}",
    f"  RELEASE-VERIFY-JOB:   {JOB}   ",
    f"release-verify-job: `{JOB}`",
    f"`release-verify-job: {JOB}`",
    f"- `release-verify-job`: `{JOB}`",
])
def test_the_notes_job_line_is_read_in_its_usual_spellings(trk, world, tmp_path, gh, line):
    notes(trk, tmp_path, world["P"], f"The map.\n\n{line}\n")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_the_notes_job_line_keeps_the_job_name_whole(trk, world, tmp_path, gh):
    gh(jobs=(("verify deployed release", "success"),))
    notes(trk, tmp_path, world["P"], "release-verify-job: verify deployed release\n")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


@pytest.mark.parametrize("line", ["release-verify-job:", "- release-verify-job:    ", "release-verify-job: ``"])
def test_an_empty_notes_job_line_is_a_need_not_a_fallback(trk, world, tmp_path, gh, line):
    ok(trk, "config", "set", "release-verify-job", JOB)
    notes(trk, tmp_path, world["P"], f"{line}\n")
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["reason"]) == ("release-verify-job", "empty")
    assert gh.calls() == []


@pytest.mark.parametrize("line", [
    "**release-verify-job**: lint",
    "**release-verify-job:** lint",
    "1. release-verify-job: lint",
    "> release-verify-job: lint",
    "| release-verify-job | lint |",
    "release-verify-job: # nothing",
    "release-verify-job: build #2",
    f"release-verify-job: {JOB}  # the deploy check",
    "release-verify-job: `lint",
    "release-verify-job: *lint*",
    "release-verify-job: lint; echo",
    "Set release-verify-job: lint here",
    "release-verify-job lint",
])
def test_a_notes_line_that_mentions_the_setting_but_does_not_read_cleanly_is_a_need(trk, world, tmp_path, gh, line):
    ok(trk, "config", "set", "release-verify-job", JOB)
    notes(trk, tmp_path, world["P"], f"The map.\n{line}\n")
    gh(jobs=(("lint", "success"), (JOB, "success")))
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["reason"]) == ("release-verify-job", "unparsed")
    assert gh.calls() == []


def test_a_second_unclean_line_spoils_a_clean_first_one(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n**release-verify-job**: lint\n")
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert need["reason"] == "unparsed"


def test_two_lines_naming_different_jobs_are_unparsed(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\nrelease-verify-job: lint\n")
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert need["reason"] == "unparsed"


def test_two_lines_naming_the_same_job_are_fine(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n- Release-Verify-Job: `{JOB}`\n")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


# --- only a person names the job (ledger-14) ---


def test_a_job_an_agent_wrote_into_the_notes_does_not_count(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n", by=("--as", "worker"))
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["reason"]) == ("release-verify-job", "not-by-person")
    assert gh.calls() == []


def test_a_job_an_agent_wrote_does_not_fall_back_to_the_store_wide_one(trk, world, tmp_path, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    notes(trk, tmp_path, world["P"], "release-verify-job: lint\n", by=("--as", "worker"))
    gh(jobs=(("lint", "success"),))
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert need["need"] == "release-verify-job" and need["reason"] == "not-by-person"


def test_an_agent_changing_the_job_a_person_wrote_makes_it_not_count(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n")
    notes(trk, tmp_path, world["P"], "release-verify-job: lint\n", by=("--as", "worker"))
    gh(jobs=(("lint", "success"),))
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert need["need"] == "release-verify-job" and need["reason"] == "not-by-person"


def test_an_agent_editing_other_notes_leaves_the_persons_job_in_force(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n")
    notes(trk, tmp_path, world["P"], f"Edited by a worker.\nrelease-verify-job: {JOB}\n", by=("--as", "worker"))
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_an_agent_putting_a_job_line_above_the_persons_makes_it_not_count(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n")
    notes(trk, tmp_path, world["P"], f"release-verify-job: lint\nrelease-verify-job: {JOB}\n", by=("--as", "worker"))
    gh(jobs=(("lint", "success"),))
    run_row(trk, world)
    assert close(trk, world["P"]).code == 1


def test_a_persons_unrelated_rewrite_does_not_attest_a_line_an_agent_planted(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n", by=("--as", "worker"))
    notes(trk, tmp_path, world["P"], f"A person tidied the map.\nrelease-verify-job: {JOB}\n")
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["reason"]) == ("release-verify-job", "not-by-person")
    assert gh.calls() == []


def test_a_person_setting_the_line_attests_it_for_later_unrelated_rewrites(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n")
    notes(trk, tmp_path, world["P"], f"A person tidied the map.\nrelease-verify-job: {JOB}\n")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_a_person_changing_an_agents_planted_value_attests_the_new_one(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], "release-verify-job: lint\n", by=("--as", "worker"))
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_a_person_in_an_agent_session_is_not_a_person_for_the_notes_job(trk, world, tmp_path, gh, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")  # no $TRK_SESSION: --as michael names a person inside an agent session
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n", by=("--as", "michael"))
    monkeypatch.delenv("CLAUDECODE")
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["reason"]) == ("release-verify-job", "not-by-person")


def test_a_person_rewriting_the_notes_attests_the_new_job(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], "release-verify-job: lint\n", by=("--as", "worker"))
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_a_job_written_by_a_person_in_the_notes_at_creation_counts(trk, tmp_path, gh):
    start(trk)
    path = tmp_path / "notes.md"
    path.write_text(f"release-verify-job: {JOB}\n")
    program = ok(trk, "new", "program", "P", *DEST, "--repo", checkout(tmp_path, "r"), "--section", f"notes=@{path}")
    add(trk, program, "test-run", "--ref", RUN, "--sha", SHA, "--verdict", "pass")
    assert close(trk, program).code == 0


def test_an_agent_cannot_set_the_store_wide_job(trk, world):
    result = trk("config", "set", "release-verify-job", "lint", "--as", "worker", "--json")
    assert result.code == 1 and result.error()["rule"] == "job-needs-person"
    assert "lint" not in ok(trk, "config")


def test_claudecode_alone_makes_a_session_for_the_store_wide_job(trk, world, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    result = trk("config", "set", "release-verify-job", "lint", "--as", "michael", "--json")
    assert result.code == 1 and result.error()["rule"] == "job-needs-person"


def test_a_person_acting_inside_an_agent_session_cannot_set_the_store_wide_job(trk, world):
    result = trk("config", "set", "release-verify-job", "lint", "--as", "michael", "--session",
                 "11111111-2222-3333-4444-555555555555", "--json")
    assert result.code == 1 and result.error()["rule"] == "job-needs-person"


def test_a_store_wide_job_no_person_set_does_not_count(trk, trk_home, world, gh):
    """A value in the store with no change by a person behind it, as an earlier build might have left."""
    conn = sqlite3.connect(trk_home / "store.db")
    conn.execute("INSERT INTO meta (key, value) VALUES ('release_verify_job', ?)", (JOB,))
    conn.commit()
    conn.close()
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["reason"]) == ("release-verify-job", "not-by-person")


# --- only the repository's own default-branch run counts (ledger-15) ---


@pytest.mark.parametrize("seen", [
    {"branch": "feature/own-job"},
    {"branch": "feature/own-job", "event": "workflow_dispatch"},
    {"event": "pull_request"},
    {"event": "pull_request_target"},
    {"branch": "main-copy"},
    {"default_branch": "trunk"},
    {"branch": "releases/1.2"},
    {"branch": "release"},
    {"branch": "release/1.2", "event": "pull_request"},
    {"branch": "release/a/b/c"},
    {"branch": "release/x/y"},
    {"branch": "v1.2.3", "event": "pull_request", "tags": {"v1.2.3": tag()}},
    {"branch": "v9", "tags": {"v1.2.3": tag()}},
    {"branch": "v1.2", "tags": {"v1.2.3": tag()}},
])
def test_a_run_that_is_not_a_default_branch_release_run_is_refused(trk, world, gh, seen):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(**seen)
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["criterion"], need["reason"]) == ("release-run", "ac-1", "run-wrong-branch")


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
def test_a_default_branch_run_of_a_release_event_counts(trk, world, gh, event):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(event=event)
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_a_release_branch_run_counts_on_a_repo_whose_default_branch_is_elsewhere(trk, world, gh, event):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(branch="release/2026.10", default_branch="develop", event=event)
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_a_tag_run_counts_on_a_repo_whose_default_branch_is_elsewhere(trk, world, gh, event):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(branch="v1.2.3", default_branch="develop", event=event, tags={"v1.2.3": tag(), "v1.2.30": tag("b" * 40)})
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_a_tag_gh_cannot_look_up_is_sha_unverifiable(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(branch="v1.2.3", default_branch="develop", tag_fail="HTTP 502")
    run_row(trk, world)
    result = close(trk, world["P"])
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"
    assert "HTTP 502" in result.error()["message"]


def test_the_default_branch_is_whatever_gh_says_it_is(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(branch="trunk", default_branch="trunk")
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_a_default_branch_gh_cannot_tell_is_sha_unverifiable(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(default_fail="HTTP 403: rate limited", branch="feature/x")
    run_row(trk, world)
    result = close(trk, world["P"])
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"
    assert "403" in result.error()["message"]


def test_a_default_branch_failure_says_it_was_the_default_branch(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(default_fail="HTTP 403: rate limited")
    run_row(trk, world)
    message = close(trk, world["P"]).error()["message"]
    assert "refs of o/r" in message and "cannot read run" not in message


def test_a_local_origin_head_does_not_decide_the_default_branch(trk, world, tmp_path, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    subprocess.run(["git", "-C", str(tmp_path / "r"), "symbolic-ref", "refs/remotes/origin/HEAD",
                    "refs/remotes/origin/feature"], check=True)
    gh(branch="feature")
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert need["reason"] == "run-wrong-branch"


# --- a store-wide job is judged where it was written, not where it is read (ledger-24) ---


def test_a_store_wide_job_a_person_set_counts_when_an_agent_session_closes(trk, world, gh, monkeypatch):
    ok(trk, "config", "set", "release-verify-job", JOB)
    run_row(trk, world)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("TRK_SESSION", "11111111-2222-3333-4444-555555555555")
    result = trk("close", world["P"], "--resolution", "done", "--outcome", "Arrived.", "--as", "worker", "--json")
    assert result.code == 0, result.err


def test_a_notes_job_a_person_set_counts_when_an_agent_session_closes(trk, world, tmp_path, gh, monkeypatch):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n")
    run_row(trk, world)
    monkeypatch.setenv("CLAUDECODE", "1")
    result = trk("close", world["P"], "--resolution", "done", "--outcome", "Arrived.", "--as", "worker", "--json")
    assert result.code == 0, result.err


def test_an_underscore_spelling_of_the_setting_is_a_mention_too(trk, world, tmp_path, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    notes(trk, tmp_path, world["P"], "release_verify_job: lint\n")
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["reason"]) == ("release-verify-job", "unparsed")


def test_the_not_by_person_detail_says_the_write_that_sets_the_value(trk, world, tmp_path, gh):
    notes(trk, tmp_path, world["P"], f"release-verify-job: {JOB}\n", by=("--as", "worker"))
    run_row(trk, world)
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert "the write that sets or changes the value must be a person's" in need["detail"]


# --- a tag is the run's own commit and has no branch of its name (ledger-23) ---


def refused_as(trk, world, reason):
    (need,) = refusal(trk, "close", world["P"], "--resolution", "done", "--outcome", "Arrived.")["missing"]
    assert (need["need"], need["reason"]) == ("release-run", reason), need
    return need


def test_a_tag_pointing_at_another_commit_is_refused(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(branch="v1.0", default_branch="develop", tags={"v1.0": tag("c" * 40)})
    run_row(trk, world)
    assert "points at cccccccccccc" in refused_as(trk, world, "run-wrong-branch")["detail"]


def test_an_annotated_tag_is_peeled_to_its_commit(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(branch="v1.0", default_branch="develop", tags={"v1.0": {"type": "tag", "sha": "d" * 40, "commit": SHA}})
    run_row(trk, world)
    assert close(trk, world["P"]).code == 0


def test_an_annotated_tag_peeled_to_another_commit_is_refused(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(branch="v1.0", default_branch="develop", tags={"v1.0": {"type": "tag", "sha": "d" * 40, "commit": "c" * 40}})
    run_row(trk, world)
    refused_as(trk, world, "run-wrong-branch")


def test_a_branch_named_like_the_tag_is_refused(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(branch="v1.0", default_branch="develop", tags={"v1.0": tag()}, branches=["v1.0"])
    run_row(trk, world)
    assert "both a tag and a branch" in refused_as(trk, world, "run-wrong-branch")["detail"]


def test_a_tag_gh_cannot_peel_or_a_branch_listing_it_cannot_read_is_sha_unverifiable(trk, world, gh):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(branch="v1.0", default_branch="develop", tags={"v1.0": tag()}, heads_fail="HTTP 502")
    run_row(trk, world)
    result = close(trk, world["P"])
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"


# --- GitHub must restrict who may write the ref (ledger-23, ledger-25) ---

def pr(count=1, **more):
    """A `pull_request` rule as rules/branches reports it: strong by default (one approval, stale ones dismissed)."""
    return {"type": "pull_request", "parameters": {"required_approving_review_count": count,
                                                   "dismiss_stale_reviews_on_push": True, **more}}


def classic(enforce_admins=True, **more):
    return {"enforce_admins": {"enabled": enforce_admins}, **more}


BRANCH_RUNS = [("main", "main"), ("release/1.2", "develop")]  # (the run's branch, the default branch)


def protected_runs(trk, world, gh, **seen):
    ok(trk, "config", "set", "release-verify-job", JOB)
    gh(**seen)
    run_row(trk, world)


@pytest.mark.parametrize("branch, default", BRANCH_RUNS)
@pytest.mark.parametrize("rules", [[], ["deletion"], ["non_fast_forward"], ["deletion", "non_fast_forward"],
                                   ["required_status_checks"]])
def test_a_branch_no_rule_restricts_is_ref_unprotected(trk, world, gh, branch, default, rules):
    protected_runs(trk, world, gh, branch=branch, default_branch=default, rules={"*": rules})
    refused_as(trk, world, "ref-unprotected")


@pytest.mark.parametrize("branch, default, rules", [
    ("main", "main", ["update"]),
    ("main", "main", [pr()]),
    ("main", "main", ["creation", "update"]),
    ("release/1.2", "develop", ["creation", "update"]),
    ("release/1.2", "develop", ["creation", pr()]),
    ("release/1.2", "develop", ["creation", pr(2, dismiss_stale_reviews_on_push=False, require_last_push_approval=True)]),
])
def test_a_branch_a_rule_restricts_counts(trk, world, gh, branch, default, rules):
    protected_runs(trk, world, gh, branch=branch, default_branch=default, rules={"*": rules})
    assert close(trk, world["P"]).code == 0


@pytest.mark.parametrize("rules", [["update"], [pr()], ["creation"], ["update", pr()], ["deletion", "non_fast_forward"]])
def test_a_release_branch_without_both_a_creation_and_a_writer_restriction_is_ref_unprotected(trk, world, gh, rules):
    protected_runs(trk, world, gh, branch="release/1.2", default_branch="develop", rules={"*": rules})
    refused_as(trk, world, "ref-unprotected")


STRONG = {"required_approving_review_count": 1, "dismiss_stale_reviews": True}


@pytest.mark.parametrize("protection", [
    classic(restrictions={"users": []}, block_creations={"enabled": True}),
    classic(required_pull_request_reviews=STRONG, block_creations={"enabled": True}),
    classic(required_pull_request_reviews=STRONG),
    classic(restrictions={"users": [], "teams": []}),
])
def test_classic_protection_never_counts_for_a_release_branch(trk, world, gh, protection):
    protected_runs(trk, world, gh, branch="release/1.2", default_branch="develop", rules={"*": []},
                   classic={"release/1.2": protection})
    refused_as(trk, world, "ref-unprotected")


def test_a_release_branch_with_an_empty_segment_is_not_one(trk, world, gh):
    protected_runs(trk, world, gh, branch="release/", default_branch="develop")
    refused_as(trk, world, "run-wrong-branch")


def test_the_rules_of_the_runs_own_branch_are_the_ones_read(trk, world, gh):
    protected_runs(trk, world, gh, branch="release/1.2", default_branch="develop",
                   rules={"release/1.2": [], "*": ["creation", "update"]})
    refused_as(trk, world, "ref-unprotected")


@pytest.mark.parametrize("protection, counts", [
    (classic(required_pull_request_reviews=STRONG), True),
    (classic(required_pull_request_reviews={"required_approving_review_count": 1, "require_last_push_approval": True}),
     True),
    (classic(required_pull_request_reviews={**STRONG, "bypass_pull_request_allowances": {"users": [], "teams": [],
                                                                                          "apps": []}}), True),
    (classic(required_pull_request_reviews={**STRONG, "bypass_pull_request_allowances": {"users": [{"login": "me"}]}}),
     False),
    (classic(required_pull_request_reviews={**STRONG, "bypass_pull_request_allowances": {"teams": [{"slug": "x"}]}}),
     False),
    (classic(required_pull_request_reviews={**STRONG, "bypass_pull_request_allowances": {"apps": [{"slug": "x"}]}}),
     False),
    (classic(restrictions={"users": [], "teams": []}), False),
    (classic(restrictions={"users": [{"login": "me"}]}), False),
    (classic(restrictions={"users": []}, block_creations={"enabled": True}), False),
    (classic(required_pull_request_reviews={"required_approving_review_count": 0, "dismiss_stale_reviews": True}), False),
    (classic(required_pull_request_reviews={"required_approving_review_count": 1}), False),
    (classic(required_pull_request_reviews={"dismiss_stale_reviews": True}), False),
    (classic(False, required_pull_request_reviews=STRONG), False),
    (classic(False, restrictions={"users": []}), False),
    ({"required_pull_request_reviews": STRONG}, False),
    (classic(required_status_checks={"strict": True}), False),
    (classic(), False),
])
def test_classic_branch_protection_counts_only_through_strong_reviews_nobody_can_bypass(trk, world, gh, protection, counts):
    protected_runs(trk, world, gh, rules={"*": []}, classic={"main": protection})
    if counts:
        assert close(trk, world["P"]).code == 0
    else:
        refused_as(trk, world, "ref-unprotected")


@pytest.mark.parametrize("failure", ["rules_fail", "classic_fail"])
@pytest.mark.parametrize("text", ["HTTP 403: Resource not accessible", "HTTP 404: Not Found"])
def test_rules_gh_cannot_read_are_sha_unverifiable_never_a_pass(trk, world, gh, failure, text):
    seen = {"rules_fail": text} if failure == "rules_fail" else {"rules": {"*": []}, "classic_fail": text}
    protected_runs(trk, world, gh, **seen)
    result = close(trk, world["P"])
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"
    assert text in result.error()["message"]


def ruleset(**more):
    return {"target": "tag", "enforcement": "active", "include": ["~ALL"], "exclude": [], "types": ["creation", "update"],
            **more}


@pytest.mark.parametrize("rulesets, counts", [
    ([ruleset()], True),
    ([ruleset(include=["refs/tags/v*"])], True),
    ([ruleset(include=["refs/tags/**"])], True),
    ([ruleset(types=["creation"]), ruleset(types=["update"])], True),
    ([], False),
    ([ruleset(enforcement="evaluate")], False),
    ([ruleset(enforcement="disabled")], False),
    ([ruleset(target="branch")], False),
    ([ruleset(include=["refs/tags/release-*"])], False),
    ([ruleset(include=["refs/tags/*/*"])], False),
    ([ruleset(exclude=["refs/tags/v1.*"])], False),
    ([ruleset(types=["deletion", "non_fast_forward"])], False),
    ([ruleset(types=["creation"])], False),
    ([ruleset(types=["update"])], False),
])
def test_a_tag_counts_only_when_an_active_tag_ruleset_restricts_its_creation_and_update(trk, world, gh, rulesets, counts):
    protected_runs(trk, world, gh, branch="v1.2.3", default_branch="develop", tags={"v1.2.3": tag()},
                   tag_rulesets=rulesets)
    if counts:
        assert close(trk, world["P"]).code == 0
    else:
        refused_as(trk, world, "ref-unprotected")


def test_tag_rulesets_gh_cannot_read_are_sha_unverifiable(trk, world, gh):
    protected_runs(trk, world, gh, branch="v1.2.3", default_branch="develop", tags={"v1.2.3": tag()},
                   rulesets_fail="HTTP 403: Resource not accessible")
    result = close(trk, world["P"])
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"


def test_the_default_branch_run_of_a_protected_repo_asks_only_for_its_rules(trk, world, gh):
    protected_runs(trk, world, gh)
    assert close(trk, world["P"]).code == 0
    assert [c.split()[0] for c in gh.calls()] == ["run", "repo", "api", "api"]


# --- a rule counts only when it is strong and the token trk runs under cannot bypass it (ledger-29) ---


@pytest.mark.parametrize("rule, counts", [
    (pr(), True),
    (pr(0), False),
    (pr(1, dismiss_stale_reviews_on_push=False), False),
    (pr(1, dismiss_stale_reviews_on_push=False, require_last_push_approval=True), True),
    (pr(1, dismiss_stale_reviews_on_push=True, require_last_push_approval=False), True),
    ({"type": "pull_request", "parameters": {}}, False),
    ({"type": "pull_request"}, False),
])
def test_a_pull_request_rule_counts_only_with_an_approval_and_stale_approvals_dealt_with(trk, world, gh, rule, counts):
    protected_runs(trk, world, gh, rules={"*": [rule]})
    if counts:
        assert close(trk, world["P"]).code == 0
    else:
        refused_as(trk, world, "ref-unprotected")


@pytest.mark.parametrize("bypass, counts", [("never", True), ("always", False), ("pull_requests_only", False)])
@pytest.mark.parametrize("branch, default, rules", [
    ("main", "main", ["update"]),
    ("main", "main", [pr()]),
    ("release/1.2", "develop", ["creation", "update"]),
])
def test_a_rule_the_token_can_bypass_does_not_count(trk, world, gh, branch, default, rules, bypass, counts):
    protected_runs(trk, world, gh, branch=branch, default_branch=default, rules={"*": rules},
                   bypass_by_id={"100": bypass})
    if counts:
        assert close(trk, world["P"]).code == 0
    else:
        refused_as(trk, world, "ref-unprotected")


def test_a_creation_rule_from_a_bypassable_ruleset_does_not_make_a_release_branch_protected(trk, world, gh):
    protected_runs(trk, world, gh, branch="release/1.2", default_branch="develop",
                   rules={"*": ["update", {"type": "creation", "ruleset_id": 101}]}, bypass_by_id={"101": "always"})
    refused_as(trk, world, "ref-unprotected")


def test_rules_from_two_rulesets_the_token_cannot_bypass_add_up(trk, world, gh):
    protected_runs(trk, world, gh, branch="release/1.2", default_branch="develop",
                   rules={"*": ["update", {"type": "creation", "ruleset_id": 101}]}, bypass_by_id={"101": "never"})
    assert close(trk, world["P"]).code == 0


def test_an_organisation_ruleset_is_read_from_the_organisation(trk, world, gh):
    org = {"ruleset_source_type": "Organization", "ruleset_source": "acme", "ruleset_id": 7}
    protected_runs(trk, world, gh, rules={"*": [{"type": "update", **org}]}, bypass_by_id={"7": "always"})
    refused_as(trk, world, "ref-unprotected")
    assert any(c.startswith("api orgs/acme/rulesets/7") for c in gh.calls())


@pytest.mark.parametrize("seen", [{"rulesets_fail": "HTTP 403: Resource not accessible"}, {"bypass_missing": True}])
def test_a_ruleset_whose_bypass_cannot_be_read_is_sha_unverifiable(trk, world, gh, seen):
    protected_runs(trk, world, gh, **seen)
    result = close(trk, world["P"])
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"


@pytest.mark.parametrize("bypass", ["always", "pull_requests_only"])
def test_a_tag_ruleset_the_token_can_bypass_does_not_count(trk, world, gh, bypass):
    protected_runs(trk, world, gh, branch="v1.2.3", default_branch="develop", tags={"v1.2.3": tag()},
                   tag_rulesets=[ruleset(bypass=bypass)])
    refused_as(trk, world, "ref-unprotected")


def test_a_tag_ruleset_whose_bypass_cannot_be_read_is_sha_unverifiable(trk, world, gh):
    protected_runs(trk, world, gh, branch="v1.2.3", default_branch="develop", tags={"v1.2.3": tag()},
                   bypass_missing=True)
    result = close(trk, world["P"])
    assert result.code == 1 and result.error()["rule"] == "sha-unverifiable"
