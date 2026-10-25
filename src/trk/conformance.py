"""`python -m trk.conformance -- COMMAND`: check that a page adapter keeps the protocol (docs/adapters.md).

It runs the adapter as trk would and writes one throwaway page, tagged `trk:conformance-<random>`, so
point it at a scratch directory, team or space. Each check prints `ok` or `FAIL` with what it saw; the
exit code is 0 when every check passed, else 1.

    python -m trk.conformance -- trk-pages-folder --dir /tmp/pages-check
"""

import json
import shlex
import sys
import uuid

from . import adapter
from .publisher import Refused, ServiceError

PAGE = ('<!doctype html><html><head><title>conformance</title></head><body>'
        '<section data-comment-id="a"><p>one</p></section>'
        '<section data-comment-id="b"><p>two</p></section></body></html>')


class Failed(Exception):
    pass


def expect(condition, message):
    if not condition:
        raise Failed(message)


def refused_with(error, call):
    """The Refused `call()` raises, which must carry `error`."""
    try:
        call()
    except Refused as exc:
        expect(exc.error == error, f"refused with {exc.error!r}, not {error!r}")
        return exc
    except ServiceError as exc:
        raise Failed(f"failed ({exc}), not refused with exit 4 and {error!r}") from None
    raise Failed(f"succeeded; it must be refused with exit 4 and {error!r}")


def checks(client, tag):
    """(name, function) pairs, run in order; later ones use what earlier ones made."""
    state = {}

    def describe():
        answer = client.call("describe", {})
        expect(answer.get("protocol") == adapter.PROTOCOL, f"protocol is {answer.get('protocol')!r}")
        expect(isinstance(answer.get("name"), str) and answer["name"], "`name` must be a non-empty string")
        caps = answer.get("capabilities")
        expect(isinstance(caps, list) and all(isinstance(c, str) for c in caps), "`capabilities` must be a list")
        expect("activity" not in caps or "comments" in caps, "`activity` needs `comments`")
        state["caps"] = set(caps)

    def check():
        client.call("check", {})

    def other_protocol():
        code, _out, _err = (client._runner or adapter._runner)(
            [*client.argv, "describe"], json.dumps({"protocol": adapter.PROTOCOL + 1}).encode())
        expect(code == adapter.HELD, f"a request of protocol {adapter.PROTOCOL + 1} exits {code}, not 2")

    def find_nothing():
        expect(client.find_tagged(tag) is None, "a tag never published must give `page: null`")

    def publish():
        page = client.publish(title="conformance", body=PAGE, team="tracker", tags=[tag])
        expect(page["version"] >= 1, "the first version must be 1 or more")
        state["page"] = page

    def find():
        found = client.find_tagged(tag)
        expect(found is not None and found["slug"] == state["page"]["slug"], f"find gave {found!r}")
        expect(found["version"] == state["page"]["version"], "find must give the current version")

    def update_whole():
        page = state["page"]
        version = client.update(page["slug"], base_version=page["version"], body=PAGE.replace("two", "2"))["version"]
        expect(version > page["version"], f"a write must give a higher version, not {version}")
        page["version"] = version

    def stale():
        page = state["page"]
        exc = refused_with("conflict", lambda: client.update(page["slug"], base_version=page["version"] - 1,
                                                             body="<p>stale</p>"))
        current = (exc.payload or {}).get("currentVersion")
        expect(current == page["version"], f"a conflict must carry currentVersion {page['version']}, not {current!r}")
        expect(isinstance((exc.payload or {}).get("changedSections", []), list), "changedSections must be a list")

    def title():
        page = state["page"]
        version = client.update(page["slug"], base_version=page["version"], title="conformance 2")["version"]
        expect(version >= page["version"], f"a title alone gave version {version}, below {page['version']}")
        page["version"] = version

    def patch():
        page = state["page"]
        edit = {"section": "b", "replace": '<section data-comment-id="b"><p>three</p></section>'}
        page["version"] = client.update(page["slug"], base_version=page["version"], edits=[edit])["version"]
        got = client.sections(page["slug"], handles=["a", "b", "zz"])
        sources = {s.get("id"): s.get("source") for s in got["sections"]}
        expect("three" in (sources.get("b") or ""), f"section b after the patch is {sources.get('b')!r}")
        expect("one" in (sources.get("a") or ""), "a patch must leave the other sections alone")
        expect(got.get("missing") == ["zz"], f"missing is {got.get('missing')!r}, not ['zz']")

    def unknown_section():
        page = state["page"]
        refused_with("edit_not_applied", lambda: client.update(
            page["slug"], base_version=page["version"], edits=[{"section": "zz", "replace": "<p>x</p>"}]))

    def not_found():
        refused_with("not_found", lambda: client.update("no-such-page-" + uuid.uuid4().hex[:8], base_version=1,
                                                        body="<p>x</p>"))

    def comments():
        if "comments" not in state["caps"]:
            refused_with("unknown_verb", lambda: client.list_comments(state["page"]["slug"]))
            return
        listed = client.list_comments(state["page"]["slug"])
        expect(isinstance(listed["comments"], list), "`comments` must be a list")

    def activity():
        if "activity" not in state["caps"]:
            return
        log = client.activity(None, limit=10)
        expect(isinstance(log["events"], list), "`events` must be a list")
        refused_with("invalid_cursor", lambda: client.activity("not-a-cursor-" + uuid.uuid4().hex, limit=10))

    return [("describe", describe), ("check", check), ("another protocol is exit 2", other_protocol),
            ("find before publish", find_nothing), ("publish", publish), ("find", find),
            ("update, whole body", update_whole), ("update on a stale version", stale),
            ("title alone", title),
            ("patch and sections", patch), ("patch of an unknown section", unknown_section),
            ("update of an unknown page", not_found), ("comments", comments), ("activity", activity)]


def run(command, *, runner=None, out=None):
    """Every check against the adapter `command`; True when all passed."""
    out = out or sys.stdout
    client = adapter.Client(command, runner=runner)
    tag = f"trk:conformance-{uuid.uuid4().hex[:10]}"
    passed = True
    for name, check in checks(client, tag):
        try:
            check()
        except (Failed, ServiceError, KeyError, TypeError) as exc:
            print(f"FAIL  {name}: {exc}", file=out)
            passed = False
            if name in ("describe", "check", "publish"):
                break  # nothing after it can run
        else:
            print(f"ok    {name}", file=out)
    return passed


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return 0 if run(shlex.join(argv)) else 1


if __name__ == "__main__":
    sys.exit(main())
