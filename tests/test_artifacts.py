"""The artifact client (Page publisher spec, Artifact client) against the in-process fake service."""

import json

import pytest

from fake_artifacts import BASE_URL, TOKEN, TRACKER_GRANTS, FakeArtifactService
from trk import artifacts
from trk.artifacts import Client, ConnectionFailure, HttpFailure, ServiceError, ToolError, Unauthorized


class Sleeps(list):
    """The injected clock: records every backoff instead of sleeping."""

    def __call__(self, seconds):
        self.append(seconds)


@pytest.fixture
def fake():
    return FakeArtifactService()


@pytest.fixture
def sleeps():
    return Sleeps()


def client(fake, sleeps, token=TOKEN):
    return Client(BASE_URL, token, transport=fake, sleep=sleeps)


def page(handle_body="<p>one</p>"):
    return (f'<!doctype html><html><body><section data-comment-id="header"><h1>T</h1></section>'
            f'<section data-comment-id="tiles">{handle_body}</section></body></html>')


# ----- the handshake and the wire ------------------------------------------------------------


def test_first_call_initializes_then_notifies_and_echoes_the_session(fake, sleeps):
    c = client(fake, sleeps)
    c.get_publishing_guidance()
    methods = [method for method, _ in fake.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    init_headers = fake.requests[0][1]
    assert init_headers["Authorization"] == f"Bearer {TOKEN}"
    assert init_headers["Accept"] == "application/json, text/event-stream"
    assert "Mcp-Session-Id" not in init_headers
    assert fake.requests[1][1]["Mcp-Session-Id"] == "s-1"
    assert fake.requests[2][1]["Mcp-Session-Id"] == "s-1"
    c.get_publishing_guidance()
    assert [method for method, _ in fake.requests].count("initialize") == 1


def test_a_stateless_service_sends_no_session_and_none_is_echoed(sleeps):
    fake = FakeArtifactService(stateful=False)
    c = client(fake, sleeps)
    assert c.get_publishing_guidance()["guidanceVersion"] == "fake0001"
    assert all("Mcp-Session-Id" not in headers for _, headers in fake.requests)


@pytest.mark.parametrize("sse", [True, False], ids=["sse", "json"])
def test_results_parse_from_sse_data_lines_and_from_plain_json(sse, sleeps):
    fake = FakeArtifactService(sse=sse)
    result = client(fake, sleeps).publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])
    assert result["slug"] == "fake-0001"
    assert result["version"] == 1
    assert result["url"] == f"{BASE_URL}/artifacts/fake-0001"


def test_sse_parsing_joins_multi_line_data_skips_comments_and_picks_the_answer_by_id():
    stream = (
        ": keepalive\n\n"
        'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
        'data: {"jsonrpc":"2.0","id":7,\ndata: "result":{"ok":true}}\n\n'
    )
    assert artifacts.parse_sse(stream, 7) == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}


# ----- AC 5: session expiry, retries, missing grants ---------------------------------------------


def test_an_expired_session_reinitializes_once_and_retries(fake, sleeps):
    c = client(fake, sleeps)
    c.get_publishing_guidance()
    fake.fail_next("session-expired")
    c.list_my_artifacts()
    methods = [method for method, _ in fake.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call",
                       "tools/call", "initialize", "notifications/initialized", "tools/call"]
    assert fake.requests[-1][1]["Mcp-Session-Id"] == "s-2"
    assert sleeps == []


def test_a_session_rejected_twice_in_a_row_is_not_reinitialized_again(fake, sleeps):
    c = client(fake, sleeps)
    c.get_publishing_guidance()
    fake.fail_next("session-expired", times=2)
    with pytest.raises(ServiceError, match="session"):
        c.list_my_artifacts()
    assert [method for method, _ in fake.requests].count("initialize") == 2


def test_a_502_on_a_read_is_retried_after_5_20_and_60_seconds(fake, sleeps):
    c = client(fake, sleeps)
    fake.fail_next("502", tool="list_my_artifacts", times=3)
    assert c.list_my_artifacts()["artifacts"] == []
    assert sleeps == [5, 20, 60]


def test_a_read_that_keeps_failing_gives_up_after_three_retries(fake, sleeps):
    c = client(fake, sleeps)
    fake.fail_next("502", tool="get_artifact", times=4)
    with pytest.raises(HttpFailure) as caught:
        c.get_artifact("fake-0001", handles=["tiles"])
    assert caught.value.status == 502
    assert sleeps == [5, 20, 60]


def test_connection_errors_on_update_are_retried_like_a_502(fake, sleeps):
    c = client(fake, sleeps)
    slug = c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])["slug"]
    fake.fail_next("connection", tool="update_artifact")
    result = c.update_artifact(slug, base_version=1, edits=[{"section": "tiles", "replace": '<section data-comment-id="tiles">2</section>'}])
    assert result["version"] == 2
    assert sleeps == [5]


def test_publish_is_never_retried_blindly(fake, sleeps):
    c = client(fake, sleeps)
    fake.fail_next("502-after", tool="publish_artifact")
    with pytest.raises(HttpFailure) as caught:
        c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])
    assert caught.value.status == 502
    assert caught.value.ambiguous
    assert sleeps == []
    assert len(fake.artifacts) == 1  # the origin did publish: the caller recovers it by tag


def test_reply_is_never_retried_blindly(fake, sleeps):
    c = client(fake, sleeps)
    slug = c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])["slug"]
    comment = fake.add_comment(slug, "tiles", "hello")
    fake.fail_next("connection", tool="reply_to_artifact_comment")
    with pytest.raises(ConnectionFailure):
        c.reply_to_artifact_comment(slug, comment, "Recorded.", resolve=True)
    assert sleeps == []


def test_tool_not_allowed_names_the_missing_grant(sleeps):
    fake = FakeArtifactService(grants=TRACKER_GRANTS - {"list_artifact_comments"})
    c = client(fake, sleeps)
    with pytest.raises(ToolError) as caught:
        c.list_artifact_comments("fake-0001")
    assert caught.value.error == "tool_not_allowed"
    assert caught.value.missing_grant == "list_artifact_comments"
    assert "list_artifact_comments" in str(caught.value)
    assert sleeps == []  # a tool error is never retried


def test_get_publishing_guidance_is_allowed_with_no_grants(sleeps):
    fake = FakeArtifactService(grants=())
    assert client(fake, sleeps).get_publishing_guidance()["topic"] == "core"


def test_a_401_raises_unauthorized_without_retrying_or_leaking_the_token(fake, sleeps):
    c = client(fake, sleeps, token="tok_wrong-secret")
    with pytest.raises(Unauthorized) as caught:
        c.get_publishing_guidance()
    assert "tok_wrong-secret" not in str(caught.value)
    assert sleeps == []


# ----- tool results and errors -----------------------------------------------------------------


def test_a_conflict_parses_into_its_json_payload(fake, sleeps):
    c = client(fake, sleeps)
    slug = c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])["slug"]
    c.update_artifact(slug, base_version=1, body=page("<p>two</p>"))
    with pytest.raises(ToolError) as caught:
        c.update_artifact(slug, base_version=1, body=page("<p>three</p>"))
    error = caught.value
    assert error.error == "conflict"
    assert error.payload["currentVersion"] == 2
    assert error.payload["changedSections"] == ["tiles"]


def test_an_unknown_handle_in_a_patch_is_refused_and_nothing_is_written(fake, sleeps):
    c = client(fake, sleeps)
    slug = c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])["slug"]
    with pytest.raises(ToolError) as caught:
        c.update_artifact(slug, base_version=1, edits=[{"section": "nope", "replace": "x"}])
    assert caught.value.error == "edit_not_applied"
    assert len(fake.artifacts[slug]["versions"]) == 1


def test_publish_without_a_team_is_refused_before_any_call(fake, sleeps):
    c = client(fake, sleeps)
    with pytest.raises(ServiceError, match="team"):
        c.publish_artifact(title="P", body=page(), team="", tags=["trk:tr-1"])
    assert fake.requests == []


def test_publish_sends_team_and_tag_and_update_never_sends_team(fake, sleeps):
    c = client(fake, sleeps)
    slug = c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])["slug"]
    c.update_artifact(slug, base_version=1, body=page("<p>two</p>"), title="P2")
    published, = fake.tool_calls("publish_artifact")
    assert published["team"] == "tracker"
    assert published["tags"] == ["trk:tr-1"]
    assert published["contentType"] == "html"
    updated, = fake.tool_calls("update_artifact")
    assert "team" not in updated
    assert updated["title"] == "P2"
    assert fake.artifacts[slug]["visibility"] == "team"


def test_get_artifact_returns_the_named_handles_source(fake, sleeps):
    c = client(fake, sleeps)
    slug = c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])["slug"]
    result = c.get_artifact(slug, handles=["tiles"])
    assert result["sections"] == [{"id": "tiles", "source": '<section data-comment-id="tiles"><p>one</p></section>'}]


def test_list_my_artifacts_finds_a_page_by_its_tag(fake, sleeps):
    c = client(fake, sleeps)
    c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])
    rows = c.list_my_artifacts()["artifacts"]
    assert [row["tags"] for row in rows] == [["trk:tr-1"]]
    assert fake.tool_calls("list_my_artifacts") == [{"status": "all", "limit": 100, "offset": 0}]


def test_comments_carry_author_ids_and_replies_report_resolved(fake, sleeps):
    c = client(fake, sleeps)
    slug = c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])["slug"]
    comment = fake.add_comment(slug, "tiles", "Use metres.", author="Kim Reader", author_id="u-kim")
    listed = c.list_artifact_comments(slug)["comments"]
    assert [(row["id"], row["handle"], row["authorId"], row["authorKind"]) for row in listed] == \
        [(comment, "tiles", "u-kim", "human")]
    reply = c.reply_to_artifact_comment(slug, comment, "Recorded.", resolve=True)
    assert reply == {"replied": True, "commentId": comment, "replyId": reply["replyId"], "resolved": True,
                     "authorId": "u-owner", "authorKind": "assistant"}
    assert fake.tool_calls("list_artifact_comments")[0]["includeResolved"] is True


def test_every_comment_read_asks_for_scope_all(fake, sleeps):
    """A connector call defaults to scope claude, which hides every reviewer's answer (tr-8eq14)."""
    c = client(fake, sleeps)
    slug = c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])["slug"]
    c.list_artifact_comments(slug)
    assert [args["scope"] for args in fake.tool_calls("list_artifact_comments")] == ["all"]
    # list_comment_activity's scope is asserted on the wire in tests/test_pages_activity.py


def test_resolve_on_someone_elses_page_reports_resolved_false(fake, sleeps):
    c = client(fake, sleeps)
    slug = c.publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])["slug"]
    comment = fake.add_comment(slug, "tiles", "hello")
    fake.give_away(slug)
    reply = c.reply_to_artifact_comment(slug, comment, "Recorded.", resolve=True)
    assert reply["resolved"] is False
    assert "note" in reply


def test_a_plain_404_is_not_taken_for_an_expired_session(sleeps):
    fake = FakeArtifactService()
    c = Client(BASE_URL + "/wrong", TOKEN, transport=fake, sleep=sleeps)
    with pytest.raises(HttpFailure) as caught:
        c.get_publishing_guidance()
    assert caught.value.status == 404
    assert [method for method, _ in fake.requests] == ["initialize"]


def test_a_5xx_on_a_write_that_is_not_retried_is_ambiguous(fake, sleeps):
    fake.fail_next("500", tool="publish_artifact")
    with pytest.raises(HttpFailure) as caught:
        client(fake, sleeps).publish_artifact(title="P", body=page(), team="tracker", tags=["trk:tr-1"])
    assert (caught.value.status, caught.value.ambiguous) == (500, True)


def test_a_non_tool_json_rpc_error_is_a_service_error(fake, sleeps):
    c = client(fake, sleeps)
    with pytest.raises(ServiceError, match="Method not found"):
        c.rpc("resources/list", {})


def test_the_default_transport_refuses_redirects():
    handler = artifacts._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/") is None


def test_the_endpoint_is_the_mcp_path_under_the_base_url():
    assert Client("https://x.test/", TOKEN).endpoint == "https://x.test/api/artifacts/mcp"
    assert json.loads(artifacts.request_body(3, "tools/call", {"name": "n"})) == \
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "n"}}
