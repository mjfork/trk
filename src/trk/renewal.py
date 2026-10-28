"""The adapter's credentials: their expiry, the hold and the renewal ask (Page publisher spec: Token expiry).

Loaded only by the `pages` verbs. Every flush and every pull checks the expiry the adapter's `describe`
gave at setup (an adapter that gives none has nothing to expire): at 14 days or less before it, or on any
exit 2 (the adapter is held: its credentials were rejected, or a permission or its configuration is
wrong), the publisher opens one "Fix the page adapter" ask under `publish_home`, addressed to role
`publisher`, unless `publish_renewal_ask` names one still open. An exit 2 also holds every page write
until setup succeeds: `publish_token_rejected` carries the moment of the refusal, and only a setup
whose live check passed clears it. That setup answers the open ask in its own transaction when the new
expiry is outside the window, and leaves it open (saying why) when it is not, or when its actor may not
answer.

The hold marker and the ask's id are publisher bookkeeping, written without journal rows; the ask
itself is journaled `ask.opened` by actor `publisher`, and its answer `ask.answered` by whoever ran setup.
"""

import shlex
from datetime import date, timedelta

from . import asks, clock
from .errors import TrkError
from .store import PUBLISHER

TITLE = "Fix the page adapter"
WINDOW = timedelta(days=14)
ANSWER_OUTCOME = "Adapter fixed"
# Why setup leaves an open renewal ask open.
EXPIRES_SOON, NO_PERMISSION = "expires-soon", "ask-permission"

ASK_KEY = "publish_renewal_ask"
REJECTED_KEY = "publish_token_rejected"
# Set beside REJECTED_KEY when trk could not start the adapter at all (`Held.cannot_run`), so the ask says
# so instead of blaming credentials. Only trk's own words are kept here: the ask's question is published.
CANNOT_RUN_KEY = "publish_adapter_cannot_run"
OLD_SUFFIX = "the page backend rejected the token (HTTP 401)"  # a hold's last_error from before adapters


def held(st):
    """True while an exit 2 holds every page write: from the refusal until a setup whose check passed."""
    return bool(st.meta(REJECTED_KEY))


def expires_soon(expires, today):
    """True at 14 days or less before `expires` (a YYYY-MM-DD date), and after it."""
    return date.fromisoformat(expires) - today <= WINDOW


def _today():
    """Today's date at `clock.now()` ($TRK_NOW in tests)."""
    return clock.now().date()


def check(st, client):
    """The check every flush and pull starts with: open the ask if page writes are held or the adapter's
    credentials expire within the window. Returns {held, renewal_ask, renewal_ask_error?} for the run's
    report."""
    is_held = held(st)
    expires = st.meta("publish_expires")
    if is_held or (expires and expires_soon(expires, _today())):
        return {"held": is_held, **ensure_ask(st, token_rejected=is_held)}
    return {"held": False, "renewal_ask": None}


def rejected(st, client, exc=None):
    """An exit 2 from `client`: hold every page write and make sure the ask is open. Returns the report
    fields as `check`. When a setup has run since `client` was made, nothing is held and no ask opens,
    so a setup that committed during the run is never undone by the old setup's refusal; the next run
    tries the new one.

    `exc` is the `Held` that was raised, where the caller has it: a spawn failure's own message goes into
    the ask, so a command the timer cannot run is not reported as a credentials problem."""
    from . import pages  # pages imports this module

    cannot_run = getattr(exc, "cannot_run", None)
    with st.write_lock():
        if client.setup_id != st.meta(pages.SETUP_KEY):
            return {"held": held(st), "renewal_ask": _open_ask(st.conn, st.meta(ASK_KEY))}
        _set_meta(st.conn, REJECTED_KEY, clock.stamp(clock.now()))
        if cannot_run:
            _set_meta(st.conn, CANNOT_RUN_KEY, cannot_run)
        else:
            st.conn.execute("DELETE FROM meta WHERE key = ?", (CANNOT_RUN_KEY,))
    return {"held": True, **ensure_ask(st, token_rejected=True)}


def ensure_ask(st, *, token_rejected):
    """The open renewal ask's id, opening one unless `publish_renewal_ask` names one still open.

    A refusal (the home item closed since setup, the `publisher` role gone) never stops the run that
    found it: it comes back as `renewal_ask_error`, and the next run tries again. An exit 2's ask is not
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
    """The ask's question: why it opened, what trk could not run where that is the cause, the expiry date
    and the setup command."""
    expires = st.meta("publish_expires")
    command = st.meta("publish_adapter")
    cannot_run = st.meta(CANNOT_RUN_KEY)
    if not token_rejected:
        why = f"The page adapter's credentials expire on {expires}."
    elif cannot_run:
        why = (f"trk could not start the page adapter, so every page write is held (exit 2): "
               f"{cannot_run.rstrip('.')}. The command may be right in a shell and wrong for the timer, which "
               f"runs with a bare PATH: setup stores the resolved path of the adapter it checked, so running "
               f"setup again where the command works is usually the fix.")
    else:
        why = ("The page adapter refused (exit 2): its credentials were rejected, or a permission or its "
               "configuration is wrong. Page writes are held until setup succeeds; the pages stay dirty meanwhile."
               + (f" Its credentials were set to expire on {expires}." if expires else ""))
    return (f"{why}\n\n"
            f"Put that right where the adapter keeps it, or fix the command so it can run "
            f"(`trk pages --errors` shows its last message), then run:\n\n"
            f"    trk pages setup --adapter {shlex.quote(command or 'COMMAND')} --home {st.meta('publish_home')}\n\n"
            f"Setup checks the adapter and answers this ask; the next tick catches every page up.\n")


def on_setup(tx, actor, expires):
    """Inside setup's transaction, after its check passed: lift the hold (and the failure pause of every
    page the refusal failed) and answer the open renewal ask when the new expiry is outside the window,
    or when the adapter gives none (`expires` None). Returns None when no renewal ask is open, else
    {id, answered[, reason]}; reason EXPIRES_SOON or NO_PERMISSION says why it stays open."""
    conn = tx.conn
    if held(tx.store):
        # A page the refusal failed didn't fail by itself: it is due again at once, not after the failure
        # pause, so the next tick catches every page up.
        from .publisher import Held  # the hot path never loads the adapter client

        for suffix in (Held.SUFFIX, OLD_SUFFIX):
            conn.execute("UPDATE pages SET last_error = NULL, last_error_at = NULL WHERE last_error LIKE ?",
                         (f"%{suffix}",))
    conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (REJECTED_KEY, CANNOT_RUN_KEY))
    ask_id = _open_ask(conn, tx.store.meta(ASK_KEY))
    if ask_id is None:
        return None
    if expires is not None and expires_soon(expires, tx.now.date()):
        return {"id": ask_id, "answered": False, "reason": EXPIRES_SOON}
    try:  # the permission check comes before anything `answer` writes
        asks.answer(tx, ask_id, ANSWER_OUTCOME, f"The page adapter checks out; its credentials expire {expires}."
                    if expires else "The page adapter checks out; it gives no expiry.")
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
