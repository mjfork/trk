"""`trk pages serve`: the pages served read-only on a loopback port, rendered as each one is asked for
(Page publisher spec: Browsing them locally). Michael, 2026-10-27: "like publishing but local"."""

import argparse
import re
import socket
import sqlite3
import urllib.error
import urllib.request

import pytest

from fake_backend import BASE_URL
from gated import DEST, ticket
from test_pages_flush import flush, live  # noqa: F401 (fixtures)
from test_pages_setup import ME, fake, setup_argv, token_file, tree  # noqa: F401 (fixtures)
from trk import render, serve
from trk.cli import build_parser
from trk.errors import TrkError


@pytest.fixture
def running():
    """A server bound to a free loopback port, serving on a daemon thread; stopped after the test."""
    httpd = serve.make_server(port=0)
    serve.in_thread(httpd)
    try:
        yield f"http://{serve.HOST}:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def question(tmp_path):
    path = tmp_path / "question.md"
    path.write_text("Which way?\n")
    return f"@{path}"


def get(base, path="/", host=None):
    """(status, body) of one GET; a 4xx or 5xx comes back like any other answer, not as an exception."""
    request = urllib.request.Request(f"{base}{path}")
    if host:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, reply.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def title_of(body):
    match = re.search(r"<title>(.*?)</title>", body, re.S)
    return match.group(1) if match else None


def links(body):
    return sorted(set(re.findall(r'href="([^"]*)"', body)))


def crumbs(body):
    """The crumb trail's hrefs, which is how a page points back at the top-level page."""
    match = re.search(r'<nav class="crumbs">(.*?)</nav>', body, re.S)
    return [] if match is None else re.findall(r'href="([^"]*)"', match.group(1))


def ok(trk, *argv):
    result = trk(*argv)
    assert result.code == 0, (argv, result.err)
    return result.out.strip()


def test_the_landing_page_is_the_top_level_page(trk, running, tree):
    status, body = get(running)
    assert status == 200
    assert title_of(body) == render.TOP_LABEL
    assert f"{running}/{tree['P']}" in links(body), "the open root links its own page on this server"


def test_an_items_page_is_served_at_its_id_and_crumbs_back_to_the_top(trk, running, tree):
    for path in (f"/{tree['J']}", f"/{tree['J']}/"):  # a trailing slash is the same page
        status, body = get(running, path)
        assert status == 200, path
        assert "J" in title_of(body)
        assert crumbs(body)[0] == f"{running}/", "the first crumb is the top-level page on this server"


def test_a_persons_needs_you_page_is_served(trk, running, tree):
    status, body = get(running, "/needs-you/michael")
    assert status == 200
    assert title_of(body) == "Needs you: michael"


def test_it_serves_a_store_that_has_never_published(trk, running, tree):
    """The point of the verb: no adapter, no `pages` row, no flush, and the pages still link each other."""
    assert trk("pages", "status", "--json").json()["set_up"] is False, "nothing is set up on this store"
    status, body = get(running)
    assert status == 200
    assert f"{running}/{tree['P']}" in links(body)
    status, body = get(running, f"/{tree['P']}")
    assert status == 200
    assert f"{running}/{tree['J']}" in links(body), "its project links through to its own served page"


def test_links_point_at_this_server_not_at_the_published_urls(trk, running, live):
    """A published store keeps its real page URLs; the server's own pages never carry them."""
    flush(trk)
    status, body = get(running)
    assert status == 200
    assert BASE_URL not in body, "a served page never links the adapter's host"
    assert f"{running}/{live['P']}" in links(body)


def test_an_ask_has_no_page_of_its_own(trk, running, tree, question):
    ask = trk("ask", tree["T"], "--to", "product", "Which way?", "--question", question, "--as", "michael",
              "--json").json()["item"]["id"]
    status, body = get(running, f"/{ask}")
    assert status == 404
    assert "asks have no page of their own" in body


def test_an_unknown_path_is_a_404_that_points_home(trk, running, tree):
    status, body = get(running, "/tr-zzzzz")
    assert status == 404
    assert f"{running}/" in links(body), "the error page offers the way back to All work"
    assert get(running, "/needs-you/nobody")[0] == 404
    assert get(running, "/not-a-page")[0] == 404


def test_a_closed_item_keeps_its_page(trk, running, tree):
    ok(trk, "close", tree["D"], "--resolution", "wont-do", "--outcome", "Not now.", "--as", "michael")
    assert get(running, f"/{tree['D']}")[0] == 200, "a closed item keeps its page, as the publisher's does"


def test_the_server_cannot_write_to_the_store(trk, tree):
    st = serve.read_only()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            st.conn.execute("INSERT INTO meta (key, value) VALUES ('serve', 'no')")
    finally:
        st.close()


def test_the_url_map_covers_every_page_the_server_answers(trk, tree):
    st = serve.read_only()
    try:
        found = serve.urls(st.conn, "http://here")
    finally:
        st.close()
    assert found[render.TOP] == "http://here/"
    assert found[tree["P"]] == f"http://here/{tree['P']}"
    assert found[render.NEEDS_YOU + "michael"] == "http://here/needs-you/michael"


def test_links_follow_a_loopback_host_so_a_tunnel_on_another_port_works(trk, running, tree):
    _status, body = get(running, "/", host="localhost:9000")
    found = links(body)
    assert found, "the top-level page links its root and its people"
    assert all(link.startswith("http://localhost:9000/") for link in found), found


def test_a_host_that_is_not_loopback_is_never_echoed_into_a_page(trk, running, tree):
    _status, body = get(running, "/", host="pages.example.test")
    assert "pages.example.test" not in body
    found = links(body)
    assert found and all(link.startswith(f"{running}/") for link in found), found


@pytest.mark.parametrize("host, expected", [
    ("localhost:9000", "http://localhost:9000"),
    ("127.0.0.1:8795", "http://127.0.0.1:8795"),
    ("[::1]:8795", "http://[::1]:8795"),
    ("pages.example.test", "http://127.0.0.1:7"),
    ("", "http://127.0.0.1:7"),
    (None, "http://127.0.0.1:7"),
    ("127.0.0.1:not-a-port", "http://127.0.0.1:7"),
])
def test_the_base_a_page_links_from(host, expected):
    assert serve.base_for(host, 7) == expected


def test_a_port_already_in_use_is_refused_not_a_traceback():
    with socket.socket() as taken:
        taken.bind((serve.HOST, 0))
        taken.listen(1)
        with pytest.raises(TrkError) as caught:
            serve.make_server(port=taken.getsockname()[1])
    assert caught.value.rule == "port-in-use"
    assert caught.value.exit_code == 1


def test_the_verbs_default_port_is_the_servers():
    """`cli` spells the default so building the parser imports no server; the two must not drift."""
    action = next(a for a in verb_parser("pages", "serve")._actions if a.dest == "port")
    assert action.default == serve.DEFAULT_PORT


def verb_parser(*words):
    """The subparser of `trk <words...>`."""
    parser = build_parser()
    for word in words:
        subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        parser = subparsers.choices[word]
    return parser


def test_head_answers_with_the_pages_length_and_no_body(trk, running, tree):
    request = urllib.request.Request(f"{running}/", method="HEAD")
    with urllib.request.urlopen(request, timeout=10) as reply:
        assert reply.status == 200
        assert int(reply.headers["Content-Length"]) > 0
        assert reply.read() == b""


def test_a_free_port_is_one_nothing_is_listening_on():
    port = serve.free_port()
    httpd = serve.make_server(port=port)
    httpd.server_close()
