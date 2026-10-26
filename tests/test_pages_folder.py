"""The reference page adapter `trk-pages-folder` (trk.folder): `trk pages setup --adapter "trk-pages-folder
--dir PATH"` writes every page as an HTML file in a directory, with no credentials and no comments. The
adapter runs as a real child process, as trk runs any adapter."""

import json
import re
import shlex
import sys

import pytest

from test_pages_setup import ME, journal, tree  # noqa: F401 (fixtures)
from trk import adapter, folder, publisher
from trk.top import KEY as TOP


def command(out, *extra):
    return shlex.join([sys.executable, "-m", "trk.folder", "--dir", str(out), *extra])


@pytest.fixture
def out(tmp_path):
    return tmp_path / "pages"


@pytest.fixture
def live(trk, tree, out):
    result = trk("pages", "setup", "--adapter", command(out), "--home", tree["P"], "--json")
    assert result.code == 0, result.err
    return tree


def flush(trk, *extra):
    result = trk("pages", "flush", "--now", "--json", *extra)
    assert result.code == 0, result.err
    return result.json()


def test_setup_journals_the_adapter_and_needs_no_credentials(trk, trk_home, tree, out):
    result = trk("pages", "setup", "--adapter", command(out), "--home", tree["P"], "--json")
    assert result.code == 0, result.err
    done = result.json()
    assert (done["name"], done["capabilities"], done["expires"]) == ("folder", [], None)
    rows = [json.loads(row["data"]) for row in journal(trk_home) if row["change"] == "config.changed"]
    assert [row["key"] for row in rows[-2:]] == ["publish-adapter", "publish-home"]
    assert rows[-2]["new"] == command(out)
    assert out.is_dir()  # the check made it


def test_setup_needs_an_adapter(trk, tree):
    result = trk("pages", "setup", "--home", tree["P"])
    assert result.code == 2 and "--adapter" in result.err


def test_a_folder_adapter_without_a_directory_is_refused(trk, tree):
    result = trk("pages", "setup", "--adapter", shlex.join([sys.executable, "-m", "trk.folder"]), "--home", tree["P"],
                 "--json")
    assert result.code == 1 and result.error()["rule"] == "adapter-check-failed"
    assert "--dir" in result.error()["message"]


def test_a_program_that_cannot_run_is_refused(trk, tree):
    result = trk("pages", "setup", "--adapter", "no-such-adapter-anywhere --x", "--home", tree["P"], "--json")
    assert result.code == 1 and result.error()["rule"] == "adapter-check-failed"
    assert "cannot run no-such-adapter-anywhere" in result.error()["message"]


def test_a_flush_writes_every_page_with_links_between_them(trk, live, out):
    report = flush(trk)
    assert sorted(report["published"]) == sorted([*live.values(), ME, TOP])
    files = {path.stem for path in out.glob("*.html")}
    assert files == {*live.values(), ME.replace(":", "-"), "top"}  # the adapter slugs `top:` as `top`
    index = json.loads((out / folder.INDEX).read_text())
    assert index[live["P"]]["tag"] == f"trk:{live['P']}"
    j = (out / f"{live['J']}.html").read_text()
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
    assert trk("pages", "setup", "--adapter", command(out, "--base-url", "https://pages.example/trk/"),
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
    backend = folder.Backend(str(tmp_path))
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


def test_a_title_alone_is_a_new_version_on_the_current_one(tmp_path):
    backend = folder.Backend(str(tmp_path))
    made = backend.publish(title="T", body="<p>x</p>", team="tracker", tags=["trk:tr-x"])
    assert backend.update(made["slug"], base_version=1, title="T2")["version"] == 2
    with pytest.raises(publisher.Refused) as caught:
        backend.update(made["slug"], base_version=1, title="T3")
    assert caught.value.error == "conflict"
    assert json.loads((tmp_path / folder.INDEX).read_text())["tr-x"]["title"] == "T2"


def test_a_directory_that_cannot_be_written_fails_the_check(trk, tree, tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    result = trk("pages", "setup", "--adapter", command(blocked / "pages"), "--home", tree["P"], "--json")
    assert result.code == 1 and result.error()["rule"] == "adapter-check-failed"


def test_the_installed_script_speaks_the_protocol(tmp_path):
    client = adapter.Client(command(tmp_path / "pages"))
    assert client.check() == {"name": "folder", "capabilities": []}
    made = client.publish(title="T", body="<p>x</p>", team="tracker", tags=["trk:tr-x"])
    assert client.find_tagged("trk:tr-x") == made
    with pytest.raises(publisher.Refused) as caught:
        client.update(made["slug"], base_version=7, body="<p>y</p>")
    assert caught.value.error == "conflict" and caught.value.payload["currentVersion"] == 1
    with pytest.raises(publisher.Refused) as caught:
        client.list_comments(made["slug"])
    assert caught.value.error == "unknown_verb"


def test_status_says_whether_publishing_is_set_up_and_working(trk, tree, out, tmp_path, monkeypatch):
    monkeypatch.setenv("TRK_LAUNCH_AGENTS", str(tmp_path / "agents"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    before = trk("pages", "status", "--json")
    assert before.code == 0 and before.json()["set_up"] is False and "timer" in before.json()
    assert "not set up" in trk("pages", "status").out
    assert trk("pages", "setup", "--adapter", command(out), "--home", tree["P"]).code == 0
    waiting = trk("pages", "status", "--json").json()
    assert waiting["set_up"] and waiting["adapter"] and waiting["pages"] > 0
    assert waiting["dirty"] == waiting["pages"] and waiting["last_published_at"] is None
    flush(trk)
    done = trk("pages", "status", "--json").json()
    assert done["dirty"] == 0 and done["errors"] == 0 and done["last_published_at"]
    text = trk("pages", "status").out
    assert "publishing set up" in text and "0 waiting, 0 failing" in text and "timer" in text
