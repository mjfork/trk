"""A stub `gh` for tests/test_program_close.py: answers from the JSON file named by $FAKE_GH_ANSWER and logs each call to
$FAKE_GH_LOG. See the `gh` fixture there for the keys."""

import json
import os
import sys
import urllib.parse

answer = json.load(open(os.environ["FAKE_GH_ANSWER"]))
args = sys.argv[1:]
open(os.environ["FAKE_GH_LOG"], "a").write(" ".join(args) + "\n")


def fail(key, default="HTTP 500"):
    if answer.get(key):
        sys.stderr.write(answer[key])
        sys.exit(1)


def say(value):
    print(json.dumps(value))
    sys.exit(0)


def refs(kind, prefix, table):
    return [{"ref": f"refs/{kind}/{name}", "object": obj} for name, obj in table.items() if name.startswith(prefix)]


def ruleset_detail(number):
    """A tag ruleset (1..n, in `tag_rulesets` order) or, past those, any other ruleset id: only who can bypass it."""
    tags = answer.get("tag_rulesets", [])
    bypass = answer.get("bypass_by_id", {}).get(str(number), "never")
    detail = {}
    if number <= len(tags):
        r = tags[number - 1]
        bypass = r.get("bypass", "never")
        detail = {"conditions": {"ref_name": {"include": r["include"], "exclude": r.get("exclude", [])}},
                  "rules": [{"type": t} for t in r["types"]]}
    if not answer.get("bypass_missing"):
        detail["current_user_can_bypass"] = bypass
    return detail


if args[:2] == ["repo", "view"]:
    fail("default_fail")
    assert args[2].lower() == "o/r" and args[3:] == ["--json", "defaultBranchRef"], args
    say({"defaultBranchRef": {"name": answer["default_branch"]}})

if args[:1] == ["api"]:
    endpoint = urllib.parse.unquote(args[1])
    if endpoint.startswith("orgs/"):  # an organisation ruleset: orgs/<org>/rulesets/<id>
        fail("rulesets_fail")
        say(ruleset_detail(int(endpoint.rsplit("/", 1)[1])))
    assert endpoint.lower().startswith("repos/o/r/"), endpoint
    rest = endpoint[len("repos/o/r/"):]
    if rest.startswith("git/matching-refs/tags/"):
        fail("tag_fail")
        say(refs("tags", rest.split("/", 3)[3], {n: {"type": o["type"], "sha": o["sha"]}
                                                 for n, o in answer.get("tags", {}).items()}))
    if rest.startswith("git/matching-refs/heads/"):
        fail("heads_fail")
        say(refs("heads", rest.split("/", 3)[3], {n: {"type": "commit", "sha": "f" * 40}
                                                  for n in answer.get("branches", [])}))
    if rest.startswith("git/tags/"):
        sha = rest.split("/", 2)[2]
        say({"object": {"type": "commit", "sha": next(o["commit"] for o in answer["tags"].values() if o["sha"] == sha)}})
    if rest.startswith("rules/branches/"):
        fail("rules_fail")
        rules = answer.get("rules", {"*": ["creation", "update"]})
        entries = rules.get(rest.split("/", 2)[2], rules.get("*", []))
        say([{"ruleset_id": 100, "ruleset_source_type": "Repository", "ruleset_source": "o/r",
              **({"type": e} if isinstance(e, str) else e)} for e in entries])
    if rest.startswith("rulesets?"):
        fail("rulesets_fail")
        say([{"id": i, "target": r["target"], "enforcement": r["enforcement"]}
             for i, r in enumerate(answer.get("tag_rulesets", []), 1)])
    if rest.startswith("rulesets/"):
        fail("rulesets_fail")
        say(ruleset_detail(int(rest.split("/")[1])))
    if rest.startswith("branches/") and rest.endswith("/protection"):
        fail("classic_fail")
        classic = answer.get("classic", {})
        name = rest[len("branches/"):-len("/protection")]
        if name not in classic:
            sys.stderr.write("gh: Branch not protected (HTTP 404)")
            sys.exit(1)
        say(classic[name])
    raise AssertionError(endpoint)

assert args[:2] == ["run", "view"] and args[3] == "-R" and args[4].lower() == "o/r" and \
    args[5:] == ["--json", "conclusion,event,headBranch,headSha,jobs"], args
fail("fail")
skip = ("fail", "default_fail", "default_branch", "tags", "tag_fail", "heads_fail", "branches", "rules", "rules_fail",
        "tag_rulesets", "rulesets_fail", "classic", "classic_fail")
say({k: v for k, v in answer.items() if k not in skip})
