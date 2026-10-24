"""The built-in `folder` page backend: `trk pages setup --backend folder` writes every page as an HTML
file in a directory, with no token and no comments."""

import json

import pytest

from test_pages_setup import ME, journal, tree  # noqa: F401 (fixtures)
from trk import folder, publisher


@pytest.fixture
def out(tmp_path):
    return tmp_path / "pages"


@pytest.fixture
def live(trk, tree, out):
    result = trk("pages", "setup", "--backend", "folder", "--dir", str(out), "--home", tree["P"], "--json")
    assert result.code == 0, result.err
    return tree


def flush(trk, *extra):
    result = trk("pages", "flush", "--now", "--json", *extra)
    assert result.code == 0, result.err
    return result.json()


def test_setup_journals_the_backend_and_the_directory_and_needs_no_token(trk, trk_home, tree, out):
    result = trk("pages", "setup", "--backend", "folder", "--dir", str(out), "--home", tree["P"], "--json")
    assert result.code == 0, result.err
    done = result.json()
    assert (done["backend"], done["dir"], done["token_file"], done["expires"]) == ("folder", str(out), None, None)
    keys = [json.loads(row["data"])["key"] for row in journal(trk_home) if row["change"] == "config.changed"]
    assert keys[-3:] == ["publish-backend", "publish-dir", "publish-home"]
    assert out.is_dir()  # the check made it


def test_setup_needs_a_directory(trk, tree):
    result = trk("pages", "setup", "--backend", "folder", "--home", tree["P"])
    assert result.code == 2 and "--dir" in result.err


def test_an_unknown_backend_is_refused(trk, tree):
    result = trk("pages", "setup", "--backend", "nope", "--dir", "/tmp/x", "--home", tree["P"], "--json")
    assert result.code == 1 and result.error()["rule"] == "no-backend"
    assert "folder" in result.error()["message"]


def test_a_flush_writes_every_page_with_links_between_them(trk, live, out):
    report = flush(trk)
    assert sorted(report["published"]) == sorted([*live.values(), ME])
    files = {path.stem for path in out.glob("*.html")}
    assert files == {*live.values(), ME.replace(":", "-")}
    index = json.loads((out / folder.INDEX).read_text())
    assert index[live["P"]]["tag"] == f"trk:{live['P']}"
    j = (out / f"{live['J']}.html").read_text()
    import re
    assert (out / f"{live['P']}.html").resolve().as_uri() in re.findall(r'href="([^"#]+)', j), re.findall(r'href="([^"]+)', j)


def test_an_edit_rewrites_only_its_pages_and_bumps_their_versions(trk, live, out):
    flush(trk)
    before = json.loads((out / folder.INDEX).read_text())
    assert trk("edit", live["T"], "--title", "T renamed").code == 0
    report = flush(trk)
    assert live["T"] in report["updated"]
    after = json.loads((out / folder.INDEX).read_text())
    assert after[live["T"]]["version"] > before[live["T"]]["version"]
    assert "T renamed" in (out / f"{live['T']}.html").read_text()


def test_a_served_directory_links_by_its_url(trk, tree, out):
    assert trk("pages", "setup", "--backend", "folder", "--dir", str(out), "--base-url", "https://pages.example/trk/",
               "--home", tree["P"]).code == 0
    flush(trk)
    assert f'href="https://pages.example/trk/{tree["P"]}.html"' in (out / f"{tree['J']}.html").read_text()


def test_pull_and_tick_never_read_comments(trk, live, out):
    flush(trk)
    pulled = trk("pages", "pull", "--json")
    assert pulled.code == 0 and pulled.json()["comments"] == [] and pulled.json()["activity"] is None
    assert trk("pages", "tick", "--json").code == 0
    tidy = trk("pages", "tidy", live["P"], "--json")
    assert tidy.code == 1 and tidy.error()["rule"] == "no-comments"


def test_a_patch_replaces_only_the_named_handles(tmp_path):
    backend = folder.Backend({"dir": str(tmp_path)})
    page = '<html><body><section data-comment-id="a"><p>one</p></section><section data-comment-id="b">two</section></body></html>'
    made = backend.publish(title="T", body=page, team="tracker", tags=["trk:tr-x"])
    done = backend.update(made["slug"], base_version=1, edits=[{"section": "b", "replace": '<section data-comment-id="b">2</section>'}])
    assert done["version"] == 2
    assert (tmp_path / "tr-x.html").read_text() == page.replace(">two<", ">2<")
    with pytest.raises(publisher.Refused) as caught:
        backend.update(made["slug"], base_version=1, body="<p>stale</p>")
    assert caught.value.error == "conflict" and caught.value.payload["currentVersion"] == 2
    assert backend.find_tagged("trk:tr-x") == {"slug": "tr-x", "url": made["url"], "version": 2}
    assert backend.sections("tr-x", handles=["a", "zz"]) == {
        "sections": [{"id": "a", "source": '<section data-comment-id="a"><p>one</p></section>'}], "missing": ["zz"]}


def test_a_directory_that_cannot_be_written_fails_the_check(trk, tree, tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    result = trk("pages", "setup", "--backend", "folder", "--dir", str(blocked / "pages"), "--home", tree["P"], "--json")
    assert result.code == 1 and result.error()["rule"] == "backend-check-failed"
