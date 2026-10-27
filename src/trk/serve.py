"""`trk pages serve`: this store's pages, rendered on request and served on a loopback port.

Michael, 2026-10-27: "can we ship trk with simple web server that uses a lcoalhost port to browse
visually?" — "like publishing but local".

It is the publisher's templates without the publisher. Each request renders the page from the store as it
stands (`render.render`, `top.render_page`, `needs_you.render_page`) and hands back the same self-contained
HTML a flush would send, with its links pointed back at this server instead of at an adapter's URLs
(`urls`, passed where `render.published_urls` otherwise reads the `pages` table). So:

- Nothing is published and nothing is written. The store opens as every read verb opens it and the
  connection is then `PRAGMA query_only`, which makes SQLite refuse a write on it whatever a page asks for.
- It does not touch, need or compete with publishing. A store publishes through one adapter at a time
  (`pages.adapter_command`), so a folder adapter would take the place of a real one; this takes the place
  of nothing and runs on a store where `pages setup` has never run.
- No flush, no `pages` rows, no cursor, no page bookkeeping: a page cannot be stale, because it is
  rendered when it is asked for.

What it serves, and nothing else:

    /                     the top-level page: every open root item (`top`)
    /tr-abc12             that item's page, closed items included
    /needs-you/<actor>    that person's Needs-you page

A lapsed lease still reads as live here, exactly as a published page shows it, because the server never
sweeps: `trk sweep` ends those leases (`render.in_progress`).

It binds a loopback address only, so it opens no port on the machine's network, and it runs in the
foreground: Ctrl-C stops it. Nothing installs it as a service (the host it runs on reaches it over an
ssh tunnel, `ssh -N -L <port>:127.0.0.1:<port> <host>`). Loaded only by its verb, like the page modules.
"""

import http.server
import socket
import sys
import threading
import urllib.parse

from . import actors, items, needs_you, render, store, top
from .errors import TrkError, refused

HOST = "127.0.0.1"  # loopback only: never a route in from anywhere else
DEFAULT_PORT = 8795
NEEDS_YOU_PATH = "needs-you/"
# A link is built from the Host the browser asked for when that is one of these, so a tunnel on another
# port (`-L 9000:127.0.0.1:8795`) still links itself; anything else falls back to this server's address.
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def urls(conn, base):
    """The page URLs this server answers, shaped like `render.published_urls`: `top:` for the landing page,
    every page-bearing item (closed ones too, which keep their page), and each person with a Needs-you
    page. `base` is the absolute origin every link starts with, so the pages work from a browser."""
    found = {render.TOP: f"{base}/"}
    for row, _level in items.outline(conn, include_closed=True):
        if render.page_kind(row) is not None:
            found[row["id"]] = f"{base}/{row['id']}"
    for actor_id in needs_you.people(conn):
        found[needs_you.key(actor_id)] = f"{base}/{NEEDS_YOU_PATH}{urllib.parse.quote(actor_id)}"
    return found


def page_for(conn, path, base):
    """The HTML of the page at `path`, every link in it pointing back at `base`. Raises the store's own
    errors for a path no page answers: not-found for an unknown item or actor, refused `no-page` for an
    ask, which has no page of its own."""
    links = urls(conn, base)
    rest = urllib.parse.unquote(path).strip("/")
    if not rest:
        return top.render_page(conn, links)
    if rest.startswith(NEEDS_YOU_PATH):
        actor_id = actors.get(conn, rest[len(NEEDS_YOU_PATH):])["id"]  # exit 3 when nobody holds that id
        return needs_you.render_page(conn, actor_id, links)
    return render.render(conn, rest, links)


def read_only():
    """The store, open the way every read verb opens it (so an older schema migrates once, as `trk show`
    would), with the connection then set `query_only`: SQLite refuses every write on it from here on.
    Lapsed leases are left alone (`sweep=False`), which is a read verb's choice too."""
    st = store.open_store(sweep=False)
    try:
        st.conn.execute("PRAGMA query_only = 1")
    except BaseException:
        st.close()
        raise
    return st


def base_for(host_header, port):
    """The origin a page's links start with: what the browser asked for, when its Host names a loopback
    address, else this server's own. A Host from anywhere else is never echoed into a page."""
    host = (host_header or "").strip()
    try:
        parts = urllib.parse.urlsplit(f"//{host}")
        name, asked = parts.hostname, parts.port
    except ValueError:
        name, asked = None, None
    if name in LOOPBACK_NAMES:
        return f"http://{host}"
    return f"http://{HOST}:{port}"


ERROR_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{status} · trk</title>
<style>{css}</style>
</head>
<body>
<main class="board">
<header class="head">
<h1>{status}</h1>
<p class="outcome">{message}</p>
<p><a href="{base}/">All work</a></p>
</header>
</main>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    """One request: render a page, or say plainly why there is none. Nothing here writes."""

    server_version = "trk"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._respond(body=True)

    def do_HEAD(self):
        self._respond(body=False)

    def _respond(self, body):
        path = urllib.parse.urlsplit(self.path).path
        base = base_for(self.headers.get("Host"), self.server.server_address[1])
        try:
            st = read_only()
        except TrkError as exc:
            self._send(503, self._error_page(503, exc.message, base), body)
            return
        try:
            html = page_for(st.conn, path, base)
        except TrkError as exc:
            status = 404 if exc.exit_code in (1, 3) else 500
            self._send(status, self._error_page(status, exc.message, base), body)
            return
        finally:
            st.close()
        self._send(200, html, body)

    def _error_page(self, status, message, base):
        text = {404: "404 Not found", 500: "500 Error", 503: "503 No store"}[status]
        return ERROR_PAGE.format(status=text, css=render.CSS, message=render._e(message), base=base)

    def _send(self, status, html, body):
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")  # the next request renders the store as it is then
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"trk serve: {self.address_string()} {fmt % args}\n")


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True  # a request never holds up the shutdown Ctrl-C asks for
    allow_reuse_address = True


def make_server(port=DEFAULT_PORT, host=HOST):
    """A server bound and ready, not yet serving. Port 0 binds a free one, which the tests read back from
    `server_address`. A port already in use is refused, not a traceback."""
    try:
        return Server((host, port), Handler)
    except OSError as exc:
        raise refused("port-in-use", f"cannot serve on {host}:{port}: {exc.strerror or exc}") from None


def serve(port=DEFAULT_PORT, host=HOST, out=None):
    """Serve until Ctrl-C. Prints where it is listening, then one line per request on stderr."""
    out = out or sys.stderr
    httpd = make_server(port, host)
    bound = httpd.server_address[1]
    print(f"trk serve: http://{host}:{bound}/ — read-only, renders on request; Ctrl-C to stop", file=out)
    print(f"trk serve: {store.store_dir()}, render version {render.RENDER_VERSION}", file=out)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\ntrk serve: stopped", file=out)
    finally:
        httpd.server_close()
    return bound


def in_thread(httpd):
    """`httpd` serving on a daemon thread, for a caller that makes its own requests (the tests)."""
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


def free_port(host=HOST):
    """A port nothing is listening on, for a caller that must name one before binding it."""
    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]
