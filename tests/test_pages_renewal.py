"""Token expiry and the renewal ask (Page publisher spec: Token expiry, a backend's 401, the
renewal answer in `pages setup`; AC 10's third bullet), against the in-process fake page backend.
No real backend is ever called."""

import json

import pytest

from fake_backend import TOKEN, FakeBackend
from test_pages_flush import db, flush, live, page_row  # noqa: F401 (fixtures)
from test_pages_setup import EXPIRES, ME, fake, journal, meta, setup_argv, token_file, tree  # noqa: F401 (fixtures)
from trk import renewal

NEW_TOKEN = "tok_" + "n" * 40
GRANTS = FakeBackend.GRANTS


def renewal_asks(trk_home):
    conn = db(trk_home)
    try:
        return conn.execute("SELECT * FROM items WHERE form = 'ask' AND title = 'Mint a new tracker token' "
                            "ORDER BY rowid").fetchall()
    finally:
        conn.close()


def question(trk_home, ask_id):
    conn = db(trk_home)
    try:
        return conn.execute("SELECT body FROM sections WHERE item_id = ? AND handle = 'question'",
                            (ask_id,)).fetchone()["body"]
    finally:
        conn.close()


def rejected(fake):
    """The service stops accepting the configured token, as when it is revoked or has expired."""
    fake.token = NEW_TOKEN


def renew(trk, fake, tmp_path, home, *extra, expires="2027-10-01"):
    """Mint the new token into a fresh file and run setup with it."""
    path = tmp_path / "new-token"
    path.write_text(NEW_TOKEN + "\n")
    path.chmod(0o600)
    fake.token = NEW_TOKEN
    argv = list(setup_argv(path, home))
    argv[argv.index("--expires") + 1] = expires
    return trk(*argv, *extra)


# ----- AC 1: a 401 opens one ask and holds every page write; setup with a new token answers it --------


def test_a_401_opens_one_renewal_ask_under_the_home_and_holds_every_write(trk, trk_home, live, fake):
    since = journal(trk_home)[-1]["seq"]
    rejected(fake)
    report = flush(trk)
    assert report["held"] is True
    (ask,) = renewal_asks(trk_home)
    assert report["renewal_ask"] == ask["id"]
    assert ask["parent_id"] == live["P"] and ask["addressee_role"] == "publisher" and ask["asker"] == "publisher"
    assert ask["status"] == "open"
    opened = [row for row in journal(trk_home, since) if row["change"] == "ask.opened"]
    assert [(row["item"], row["actor"]) for row in opened] == [(ask["id"], "publisher")]
    assert meta(trk_home, "publish_renewal_ask") == ask["id"]
    assert fake.tool_calls("publish") == [] and fake.artifacts == {}
    assert all(row["dirty"] for row in trk("pages", "--json").json()["pages"])
    assert "401" in page_row(trk_home, live["P"])["last_error"]


def test_while_the_token_is_rejected_no_page_write_is_even_tried(trk, trk_home, live, fake):
    rejected(fake)
    flush(trk)
    fake.requests.clear()
    fake.token = TOKEN  # even a token that works again waits for setup
    report = flush(trk)
    assert fake.requests == []
    assert report["held"] is True and report["published"] == [] and report["failed"] == []
    assert len(renewal_asks(trk_home)) == 1


def test_a_second_401_opens_no_second_ask(trk, trk_home, live, fake, tmp_path):
    rejected(fake)
    flush(trk)
    # setup by an agent that may not answer clears the hold and leaves the ask open; the token is then rejected again
    assert renew(trk, fake, tmp_path, live["P"], "--as", "worker").code == 0
    fake.token = "tok_" + "x" * 40
    report = flush(trk)
    assert report["held"] is True
    assert len(renewal_asks(trk_home)) == 1 and report["renewal_ask"] == renewal_asks(trk_home)[0]["id"]


def test_a_401_while_an_expiry_ask_is_open_opens_no_second_ask(trk, trk_home, live, fake, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2027-09-20T09:00:00Z")
    rejected(fake)
    report = flush(trk)  # the expiry check opens the ask before the first call meets the 401
    assert report["held"] is True and fake.tool_calls("publish") == []
    (ask,) = renewal_asks(trk_home)
    assert report["renewal_ask"] == ask["id"]
    assert "HTTP 401" not in question(trk_home, ask["id"])


def test_setup_with_a_new_token_answers_the_ask_and_the_next_flush_catches_up(trk, trk_home, live, fake, tmp_path):
    rejected(fake)
    flush(trk)
    (ask,) = renewal_asks(trk_home)
    since = journal(trk_home)[-1]["seq"]
    result = renew(trk, fake, tmp_path, live["P"], "--as", "michael", "--json")
    assert result.code == 0, result.err
    assert result.json()["renewal_ask"] == {"id": ask["id"], "answered": True}
    (answered,) = [row for row in journal(trk_home, since) if row["change"] == "ask.answered"]
    data = json.loads(answered["data"])
    assert answered["item"] == ask["id"] and answered["actor"] == "michael"
    assert data["via"] == "cli" and data["outcome"] == "Token renewed"
    assert data["answer"] == "New tracker token configured, expires 2027-10-01."
    assert renewal_asks(trk_home)[0]["status"] == "closed"
    assert meta(trk_home, "publish_renewal_ask") == ""
    # the config rows and the answer are one transaction: the answer is journaled with the setup's rows
    assert {row["change"] for row in journal(trk_home, since)} == {"config.changed", "ask.answered"}
    report = flush(trk)
    assert report["held"] is False
    assert report["published"] == [live[k] for k in ("P", "J", "D", "T")] + [ME]
    assert not any(row["dirty"] for row in trk("pages", "--json").json()["pages"])


def test_setup_says_it_answered_the_ask(trk, trk_home, live, fake, tmp_path):
    rejected(fake)
    flush(trk)
    (ask,) = renewal_asks(trk_home)
    result = renew(trk, fake, tmp_path, live["P"])
    assert result.code == 0, result.err
    assert f"answered {ask['id']}" in result.out


def test_setup_with_the_rejected_token_fails_and_keeps_the_hold(trk, trk_home, live, fake, token_file):
    rejected(fake)
    flush(trk)
    result = trk(*setup_argv(token_file, live["P"]), "--json")
    assert result.code == 1 and result.error()["rule"] == "token-check-failed"
    assert renewal_asks(trk_home)[0]["status"] == "open"
    fake.token = TOKEN
    assert flush(trk)["held"] is True


# ----- AC 2: 14 days or less before expiry, a flush opens the same ask once -------------------------


@pytest.mark.parametrize("now,opens", [("2027-09-17T09:00:00Z", True), ("2027-09-16T23:00:00Z", False),
                                       ("2027-10-02T09:00:00Z", True)])
def test_a_flush_within_14_days_of_expiry_opens_the_ask_once(trk, trk_home, live, fake, monkeypatch, now, opens):
    monkeypatch.setenv("TRK_NOW", now)
    report = flush(trk)
    asks = renewal_asks(trk_home)
    assert len(asks) == (1 if opens else 0)
    assert report["renewal_ask"] == (asks[0]["id"] if opens else None)
    assert report["held"] is False and len(report["published"]) == 5  # the token still works: nothing is held
    flush(trk)
    assert len(renewal_asks(trk_home)) == len(asks)


def test_the_expiry_check_needs_no_due_page(trk, trk_home, live, fake, monkeypatch):
    flush(trk)
    monkeypatch.setenv("TRK_NOW", "2027-09-20T09:00:00Z")
    fake.requests.clear()
    report = flush(trk)
    assert report["renewal_ask"] == renewal_asks(trk_home)[0]["id"]
    assert fake.requests == []


def test_a_withdrawn_renewal_ask_is_opened_again_by_the_next_flush(trk, trk_home, live, fake, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2027-09-20T09:00:00Z")
    flush(trk)
    (first,) = renewal_asks(trk_home)
    assert trk("withdraw", first["id"], "--reason", "not now", "--as", "michael").code == 0
    report = flush(trk)
    asks = renewal_asks(trk_home)
    assert len(asks) == 2 and report["renewal_ask"] == asks[1]["id"] != first["id"]
    assert meta(trk_home, "publish_renewal_ask") == asks[1]["id"]


def test_setup_within_the_window_leaves_the_expiry_ask_open(trk, trk_home, live, fake, monkeypatch, token_file):
    monkeypatch.setenv("TRK_NOW", "2027-09-20T09:00:00Z")
    flush(trk)
    result = trk(*setup_argv(token_file, live["P"]), "--json")  # the same token, the same expiry
    assert result.code == 0, result.err
    assert result.json()["renewal_ask"] == {"id": renewal_asks(trk_home)[0]["id"], "answered": False,
                                            "reason": "expires-soon"}
    assert renewal_asks(trk_home)[0]["status"] == "open"


def test_a_closed_home_does_not_stop_the_flush(trk, trk_home, live, fake, monkeypatch):
    monkeypatch.setenv("TRK_NOW", "2027-09-20T09:00:00Z")
    conn = db(trk_home)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'publish_home'", (live["T"],))
    conn.execute("UPDATE items SET status = 'closed', resolution = 'wont_do' WHERE id = ?", (live["T"],))
    conn.commit()
    conn.close()
    report = flush(trk)
    assert report["renewal_ask"] is None and "closed" in report["renewal_ask_error"]
    assert len(report["published"]) == 5


# ----- AC 3: the question names the expiry date, the grants and the setup command --------------------


@pytest.mark.parametrize("cause", ["expiry", "401"])
def test_the_question_names_the_expiry_the_grants_and_the_setup_command(trk, trk_home, live, fake, monkeypatch,
                                                                          cause):
    if cause == "expiry":
        monkeypatch.setenv("TRK_NOW", "2027-09-20T09:00:00Z")
    else:
        rejected(fake)
    flush(trk)
    (ask,) = renewal_asks(trk_home)
    text = question(trk_home, ask["id"])
    assert EXPIRES in text
    assert all(f"`{grant}`" in text for grant in GRANTS)
    assert f"trk pages setup --backend fake --base-url {fake.base_url} --token-file" in text
    assert f"--home {live['P']}" in text and "--expires YYYY-MM-DD" in text
    assert ("HTTP 401" in text) == (cause == "401")
    assert TOKEN not in text and NEW_TOKEN not in text


def test_a_backend_without_grants_names_none(trk, trk_home, live, fake, monkeypatch):
    monkeypatch.setattr(FakeBackend, "GRANTS", ())
    rejected(fake)
    flush(trk)
    (ask,) = renewal_asks(trk_home)
    assert "ticked for" not in question(trk_home, ask["id"])


# ----- AC 4: setup by an actor that may not answer still succeeds, leaves the ask open, says so -------


def test_setup_by_an_agent_that_may_not_answer_leaves_the_ask_open_and_says_so(trk, trk_home, live, fake, tmp_path):
    rejected(fake)
    flush(trk)
    (ask,) = renewal_asks(trk_home)
    result = renew(trk, fake, tmp_path, live["P"], "--as", "worker")
    assert result.code == 0, result.err
    assert ask["id"] in result.out and "left open" in result.out and "worker may not answer" in result.out
    assert renewal_asks(trk_home)[0]["status"] == "open"
    assert meta(trk_home, "publish_renewal_ask") == ask["id"]
    json_result = renew(trk, fake, tmp_path, live["P"], "--as", "worker", "--json")
    assert json_result.json()["renewal_ask"] == {"id": ask["id"], "answered": False, "reason": "ask-permission"}
    # the token works and setup checked it: the hold is lifted even though the ask stays open
    report = flush(trk)
    assert report["held"] is False and len(report["published"]) == 5


def test_setup_with_no_renewal_ask_reports_none(trk, live, fake, tmp_path):
    result = renew(trk, fake, tmp_path, live["P"], "--json")
    assert result.json()["renewal_ask"] is None


# ----- review: a 401 on an update, and a setup that commits while a run is still sending -------------


def test_a_401_on_an_update_holds_too(trk, trk_home, live, fake):
    flush(trk)
    from gated import section_file

    body = section_file("what-to-build", "changed\n")
    assert trk("section", "set", live["T"], "what-to-build", "--file", str(body)).code == 0
    rejected(fake)
    report = flush(trk)
    assert report["held"] is True and report["updated"] == []
    assert len(renewal_asks(trk_home)) == 1 and "401" in page_row(trk_home, live["T"])["last_error"]


def test_a_401_from_before_a_setup_does_not_hold_the_new_token(trk, trk_home, live, fake, tmp_path, monkeypatch):
    from trk import store

    rejected(fake)
    flush(trk)
    assert renew(trk, fake, tmp_path, live["P"]).code == 0  # answers the ask and lifts the hold
    since = journal(trk_home)[-1]["seq"]
    monkeypatch.setenv("TRK_HOME", str(trk_home))
    st = store.open_store()
    try:  # a run that began with the old token meets its 401 only now
        report = renewal.rejected(st, fake({"base_url": fake.base_url}, TOKEN))
    finally:
        st.close()
    assert report == {"held": False, "renewal_ask": None}
    assert meta(trk_home, "publish_token_rejected") is None
    assert [row for row in journal(trk_home, since)] == []
    assert flush(trk)["held"] is False


# ----- republish (Edit conflicts' ledger-9): its 401 opens the renewal ask, and the hold refuses it ----


def test_a_401_to_republish_holds_and_opens_the_renewal_ask(trk, trk_home, live, fake):
    flush(trk)
    rejected(fake)
    result = trk("pages", "republish", live["T"], "--json")
    assert result.code == 1 and result.error()["rule"] == "republish-failed"
    (ask,) = renewal_asks(trk_home)
    assert ask["id"] in result.error()["message"]
    assert meta(trk_home, "publish_token_rejected") and "401" in page_row(trk_home, live["T"])["last_error"]
    assert flush(trk)["held"] is True


def test_republish_while_held_is_refused_before_any_call(trk, trk_home, live, fake):
    flush(trk)
    rejected(fake)
    flush(trk)  # nothing due: no call, no 401
    trk("pages", "republish", live["T"])
    fake.requests.clear()
    fake.token = TOKEN
    result = trk("pages", "republish", live["T"], "--json")
    assert result.code == 1 and "held" in result.error()["message"]
    assert fake.requests == [] and len(renewal_asks(trk_home)) == 1


def test_setup_answering_the_renewal_ask_tells_each_reply_thread(trk, trk_home, live, fake, monkeypatch, tmp_path):
    """tr-3vgrb ledger-6: a reply on the renewal ask's box hears how setup closed the ask."""
    monkeypatch.setenv("TRK_NOW", "2027-09-20T09:00:00Z")
    assert trk("actor", "link", "michael", "--service-user", "u-mf").code == 0
    flush(trk)  # opens the expiry ask and publishes its box on the home page
    (ask,) = renewal_asks(trk_home)
    c = fake.add_comment(page_row(trk_home, live["P"])["slug"], f"ask-{ask['id']}", "On it.", author="Michael Fork",
                         author_id="u-mf", created_at="2027-09-20T09:01:00.000Z")
    assert trk("pages", "pull").code == 0
    result = renew(trk, fake, tmp_path, live["P"], "--as", "michael", expires="2028-10-01")
    assert result.code == 0, result.err
    assert f"answered {ask['id']}" in result.out
    assert trk("pages", "pull").code == 0
    assert [r["message"] for r in fake.comments[c]["replies"]] == [
        "Reply received. publisher will confirm it as the answer or follow up here.",
        "Ask closed: answered by michael via the CLI."]
    assert fake.comments[c]["status"] == "open"
