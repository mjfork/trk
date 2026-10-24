"""The `tracker` token's expiry and the renewal ask (Page publisher spec: Token expiry).

Loaded only by the `pages` verbs, and only for a page backend that takes a token. Every flush and every
pull checks the token's
expiry: at 14 days or less before it, or on any 401, the publisher opens one "Mint a new tracker
token" ask under `publish_home`, addressed to role `publisher`, unless `publish_renewal_ask` names
one still open. A 401 also holds every page write until setup succeeds: `publish_token_rejected`
carries the moment of the 401, and only a setup whose live check passed clears it. That setup
answers the open ask in its own transaction when the new token's expiry is outside the window, and
leaves it open (saying why) when it is not, or when its actor may not answer.

The hold marker and the ask's id are publisher bookkeeping, written without journal rows; the ask
itself is journaled `ask.opened` by actor `publisher`, and its answer `ask.answered` by whoever ran setup.
"""

from datetime import date, timedelta

from . import asks, clock
from .errors import TrkError
from .store import PUBLISHER

TITLE = "Mint a new tracker token"
WINDOW = timedelta(days=14)
ANSWER_OUTCOME = "Token renewed"
# Why setup leaves an open renewal ask open.
EXPIRES_SOON, NO_PERMISSION = "expires-soon", "ask-permission"

ASK_KEY = "publish_renewal_ask"
REJECTED_KEY = "publish_token_rejected"


def held(st):
    """True while a 401 holds every page write: from the 401 until a setup whose token check passed."""
    return bool(st.meta(REJECTED_KEY))


def expires_soon(expires, today):
    """True at 14 days or less before `expires` (a YYYY-MM-DD date), and after it."""
    return date.fromisoformat(expires) - today <= WINDOW


def _today():
    """Today's date at `clock.now()` ($TRK_NOW in tests)."""
    return clock.now().date()


def check(st, client):
    """The check every flush and pull starts with: open the ask if the token is held or expires within
    the window. Returns {held, renewal_ask, renewal_ask_error?} for the run's report. A backend without a
    token has nothing to check."""
    if not client.TOKEN:
        return {"held": False, "renewal_ask": None}
    is_held = held(st)
    if is_held or expires_soon(st.meta("publish_token_expires"), _today()):
        return {"held": is_held, **ensure_ask(st, token_rejected=is_held)}
    return {"held": False, "renewal_ask": None}


def rejected(st, client):
    """A 401 to `client`: hold every page write and make sure the ask is open. Returns the report fields
    as `check`. When the token file no longer holds the token that was rejected, a new one has been
    written (and maybe set up) since this run began: nothing is held and no ask opens, so a setup that
    committed during the run is never undone by the old token's 401; the next run tries the new one."""
    with st.write_lock():
        if not client.uses(_current_token(st)):
            return {"held": held(st), "renewal_ask": _open_ask(st.conn, st.meta(ASK_KEY))}
        _set_meta(st.conn, REJECTED_KEY, clock.stamp(clock.now()))
    return {"held": True, **ensure_ask(st, token_rejected=True)}


def _current_token(st):
    """The token a run starting now would send; None when it can't be read (then the 401 holds)."""
    from . import pages  # pages imports this module

    try:
        return pages.read_token(pages.token_file_path(st.meta("publish_token_file")), foreground=True)
    except TrkError:
        return None


def ensure_ask(st, *, token_rejected):
    """The open renewal ask's id, opening one unless `publish_renewal_ask` names one still open.

    A refusal (the home item closed since setup, the `publisher` role gone) never stops the run that
    found it: it comes back as `renewal_ask_error`, and the next run tries again. A 401's ask is not
    opened once a setup has lifted the hold meanwhile."""
    try:
        with st.transaction(PUBLISHER) as tx:
            current = _open_ask(tx.conn, tx.store.meta(ASK_KEY))
            if current is not None or (token_rejected and not held(tx.store)):
                return {"renewal_ask": current}
            ask_id = asks.open_ask(tx, tx.store.meta("publish_home"), PUBLISHER, TITLE,
                                   question(tx.store, token_rejected=token_rejected))
            _set_meta(tx.conn, ASK_KEY, ask_id)
            return {"renewal_ask": ask_id}
    except TrkError as exc:
        return {"renewal_ask": None, "renewal_ask_error": exc.message}


def question(st, *, token_rejected):
    """The ask's question: why it opened, the expiry date, the grants and the setup command."""
    from . import pages  # pages imports this module

    expires = st.meta("publish_token_expires")
    backend = pages.backend_name(st)
    why = (f"The page backend rejected the `tracker` token (HTTP 401). Page writes are held until setup "
           f"succeeds with a new token; the pages stay dirty meanwhile. The token was set to expire on {expires}."
           if token_rejected else f"The `tracker` token expires on {expires}.")
    try:
        granted = pages.backend_class(backend).GRANTS
    except TrkError:
        granted = ()
    grants = f" ticked for {', '.join(f'`{grant}`' for grant in granted)}" if granted else ""
    command = (f"trk pages setup --backend {backend} --base-url {st.meta('publish_base_url')} "
               f"--token-file {st.meta('publish_token_file')} --expires YYYY-MM-DD --home {st.meta('publish_home')}")
    return (f"{why}\n\n"
            f"Mint a new `tracker` token (365 days){grants}. Write it to the token file "
            f"(mode 0600), then run, with the new token's expiry date:\n\n"
            f"    {command}\n\n"
            f"Setup checks the new token and answers this ask; the next tick catches every page up.\n")


def on_setup(tx, actor, expires):
    """Inside setup's transaction, after its token check passed: lift the hold (and the failure pause of
    every page the 401 failed) and answer the open
    renewal ask when the new expiry is outside the window, or when the new backend takes no token (`expires`
    None). Returns None when no renewal ask is open,
    else {id, answered[, reason]}; reason EXPIRES_SOON or NO_PERMISSION says why it stays open."""
    conn = tx.conn
    if held(tx.store):
        # A page the 401 failed didn't fail by itself: it is due again at once, not after the failure pause,
        # so the next tick catches every page up.
        from .publisher import Unauthorized  # the hot path never loads the backends

        conn.execute("UPDATE pages SET last_error = NULL, last_error_at = NULL WHERE last_error LIKE ?",
                     (f"%{Unauthorized.SUFFIX}",))
    conn.execute("DELETE FROM meta WHERE key = ?", (REJECTED_KEY,))
    ask_id = _open_ask(conn, tx.store.meta(ASK_KEY))
    if ask_id is None:
        return None
    if expires is not None and expires_soon(expires, tx.now.date()):
        return {"id": ask_id, "answered": False, "reason": EXPIRES_SOON}
    try:  # the permission check comes before anything `answer` writes
        asks.answer(tx, ask_id, ANSWER_OUTCOME, f"New tracker token configured, expires {expires}." if expires
                    else "Publishing moved to a page backend that takes no token.")
    except TrkError as exc:
        if exc.rule != NO_PERMISSION:
            raise
        return {"id": ask_id, "answered": False, "reason": NO_PERMISSION}
    _set_meta(conn, ASK_KEY, "")
    return {"id": ask_id, "answered": True}


def _open_ask(conn, ask_id):
    """`ask_id` when it names an ask still open, else None."""
    if not ask_id:
        return None
    row = conn.execute("SELECT status FROM items WHERE id = ? AND form = 'ask'", (ask_id,)).fetchone()
    return ask_id if row is not None and row["status"] == "open" else None


def _set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))
