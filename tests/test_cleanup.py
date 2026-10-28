from types import SimpleNamespace

import pytest
from conftest import A, B, bind, slackEvent

from codex_slack_bridge.router import SlackGateway, cleanupOnce, deliverOnce
from codex_slack_bridge.store import Store


def sentReplies(_router,
                _binding,
                _count,
                _start=200):
    timestamps = []
    for index in range(_count):
        key = ("progress", "response", "received", "result")[index % 4]
        with _router.store_.transaction() as db:
            _router.store_.outbox(db, _binding["id"], f"{key}:{_binding['id']}:{_start + index}", "Synthetic reply")
        row = _router.store_.nextOutput()
        ts = f"{_start + index}.000001"
        _router.store_.outputResult(row["id"], "sent", _ts=ts)
        timestamps.append(ts)
    return timestamps


def deletionGateway(_calls,
                    _status="deleted"):
    def delete(_message):
        _calls.append((_message["channel_id"], _message["slack_ts"]))
        return {"status": _status, "delay": 60}

    return SimpleNamespace(delete=delete)


def test_keeps_50_replies_including_progress_and_preserves_root_and_commands(router):
    binding = bind(router)
    timestamps = sentReplies(router, binding, 50)
    calls = []
    gateway = deletionGateway(calls)
    assert not cleanupOnce(router.store_, gateway)
    timestamps += sentReplies(router, binding, 3, 400)
    command = router.slackEvent(slackEvent())
    for index in range(3):
        assert cleanupOnce(router.store_, gateway)
    assert not cleanupOnce(router.store_, gateway)
    assert calls == [("C1", ts) for ts in timestamps[:3]]
    assert router.store_.bindings("pc-a")[0]["root_ts"] == "100.001"
    assert router.store_.status()["commands"][0]["id"] == command
    assert router.api("pc-a", "claim", {})["command"]["id"] == command
    assert router.store_.status()["cleanup"] == []
    assert router.store_.db_.execute("SELECT count(*) FROM outbox WHERE status='sent'").fetchone()[0] == 54
    #Codex: Deleting old Slack replies must not block later outbox delivery.
    posted = []
    assert deliverOnce(router.store_, SimpleNamespace(post=lambda _output: posted.append(_output) or {"status": "sent", "ts": "500.000001"}))
    assert posted[0]["id"].startswith("received:")


def test_cleanup_is_per_thread_and_skips_disabled_sessions(router):
    first = bind(router)
    second = bind(router, _agent="pc-b", _thread=A, _request=B)
    sentReplies(router, first, 52)
    sentReplies(router, second, 51, 400)
    router.api("pc-a", "unbind", {"thread_id": A})
    calls = []
    gateway = deletionGateway(calls)
    assert cleanupOnce(router.store_, gateway)
    assert calls == [("C1", "400.000001")]
    assert not cleanupOnce(router.store_, gateway)
    router.store_.rebind("pc-a", A, {"mode": "paginated", "ordinal": -1})
    assert cleanupOnce(router.store_, gateway)
    assert cleanupOnce(router.store_, gateway)
    assert not cleanupOnce(router.store_, gateway)
    assert calls[-2:] == [("C1", "200.000001"), ("C1", "201.000001")]


def test_split_messages_count_individually_and_uncertain_delivery_is_not_deleted(router):
    binding = bind(router)
    sentReplies(router, binding, 49)
    with router.store_.transaction() as db:
        router.store_.outbox(db, binding["id"], "long-answer", "x" * 7001)
    calls = []
    for ts in ("400.000001", "400.000002", "400.000003"):
        row = router.store_.nextOutput()
        router.store_.outputResult(row["id"], "sent", _ts=ts)
    with router.store_.transaction() as db:
        router.store_.outbox(db, binding["id"], "unknown-answer", "Unknown")
    row = router.store_.nextOutput()
    router.store_.outputResult(row["id"], "uncertain", _ts="101.000001")
    while cleanupOnce(router.store_, deletionGateway(calls)):
        pass
    assert calls == [("C1", "200.000001"), ("C1", "201.000001")]


def test_duplicate_timestamps_count_once_and_no_recorded_root_can_be_deleted(router):
    first = bind(router)
    bind(router, _agent="pc-b", _thread=A, _request=B)
    sentReplies(router, first, 50)
    for key, ts in [("duplicate", "200.000001"), ("own-root", "100.001"), ("other-root", "100.002")]:
        with router.store_.transaction() as db:
            router.store_.outbox(db, first["id"], key, "Recorded duplicate")
        row = router.store_.nextOutput()
        router.store_.outputResult(row["id"], "sent", _ts=ts)
    assert router.store_.nextCleanup() is None


def test_retention_uses_slack_timestamp_instead_of_queue_creation_order(router):
    binding = bind(router)
    sentReplies(router, binding, 50, 1000)
    sentReplies(router, binding, 1, 200)
    assert router.store_.nextCleanup()["slack_ts"] == "200.000001"


def test_cleanup_retries_after_restart_with_shared_cooldown(router,
                                                            monkeypatch,
                                                            tmp_path):
    clock = [1000.0]
    monkeypatch.setattr("codex_slack_bridge.store.time.time", lambda: clock[0])
    binding = bind(router)
    sentReplies(router, binding, 52)
    first = router.store_.nextCleanup()
    assert first["slack_ts"] == "200.000001"
    reopened = Store(tmp_path / "router.sqlite3")
    try:
        reopened.recoverRouter()
        assert reopened.nextCleanup() is None
        clock[0] += 61
        assert reopened.nextCleanup()["slack_ts"] == first["slack_ts"]
        reopened.cleanupResult("C1", first["slack_ts"], "pending", "Slack deletion rate limit", 120)
        clock[0] += 119
        assert reopened.nextCleanup() is None
        clock[0] += 2
        assert reopened.nextCleanup()["slack_ts"] == first["slack_ts"]
        reopened.cleanupResult("C1", first["slack_ts"], "deleted")
        assert reopened.nextCleanup()["slack_ts"] == "201.000001"
    finally:
        reopened.close()


def test_cleanup_failure_is_visible_and_does_not_block_other_replies(router):
    binding = bind(router)
    sentReplies(router, binding, 52)
    message = router.store_.nextCleanup()
    router.store_.cleanupResult("C1", message["slack_ts"], "failed", "Slack rejected deletion: cant_delete_message")
    assert router.store_.status()["cleanup"][0]["status"] == "failed"
    assert router.store_.nextCleanup()["slack_ts"] == "201.000001"


def test_existing_database_without_cleanup_table_is_upgraded(router,
                                                             tmp_path):
    binding = bind(router)
    sentReplies(router, binding, 51)
    router.store_.db_.execute("DROP TABLE slack_cleanup")
    upgraded = Store(tmp_path / "router.sqlite3")
    try:
        assert upgraded.nextCleanup()["slack_ts"] == "200.000001"
        assert upgraded.bindings("pc-a")[0]["root_ts"] == "100.001"
    finally:
        upgraded.close()


@pytest.mark.parametrize("error,expected",
                         [(None, "deleted"),
                          ("message_not_found", "deleted"),
                          ("ratelimited", "pending"),
                          ("cant_delete_message", "failed"),
                          ("missing_scope", "failed"),
                          ("invalid_auth", "failed"),
                          ("internal_error", "pending"),
                          ("private-secret", "pending"),
                          ("network", "pending"),
                          ("wrong_ack", "pending")])
def test_gateway_deletion_errors_are_safe_and_idempotent(monkeypatch,
                                                         error,
                                                         expected):
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.slack_response import SlackResponse

    calls = []

    def apiCall(_self,
                _method,
                **_kwargs):
        calls.append((_method, _kwargs["params"]))
        if error == "network":
            raise TimeoutError("private-secret")
        if error and error != "wrong_ack":
            response = SlackResponse(client=_self,
                                     http_verb="POST",
                                     api_url="https://slack.com/api/chat.delete",
                                     req_args={},
                                     data={"ok": False, "error": error},
                                     headers={"Retry-After": "120"},
                                     status_code=429 if error == "ratelimited" else 200)
            raise SlackApiError("private-secret", response)
        return {"ok": True, "channel": "C1", "ts": "999.001" if error == "wrong_ack" else "200.000001"}

    monkeypatch.setattr(WebClient, "api_call", apiCall)
    gateway = SlackGateway("xoxb-test")
    result = gateway.delete({"channel_id": "C1", "root_ts": "100.001", "slack_ts": "200.000001"})
    assert result["status"] == expected
    assert len(calls) == 1
    assert calls[0][0] == "chat.delete"
    assert calls[0][1]["channel"] == "C1"
    assert calls[0][1]["ts"] == "200.000001"
    assert "private-secret" not in str(result)
    if error == "ratelimited":
        assert result["delay"] == 120


@pytest.mark.parametrize("message",
                         [{"root_ts": "100.001", "slack_ts": "100.001"},
                          {"root_ts": "100.001", "slack_ts": "invalid"},
                          {"slack_ts": "200.000001"}])
def test_gateway_never_deletes_root_or_unknown_timestamp(monkeypatch,
                                                         message):
    from slack_sdk import WebClient

    def forbidden(*_args,
                  **_kwargs):
        raise AssertionError("No Slack request is allowed")

    monkeypatch.setattr(WebClient, "api_call", forbidden)
    assert SlackGateway("xoxb-test").delete({"channel_id": "C1", **message})["status"] == "failed"
