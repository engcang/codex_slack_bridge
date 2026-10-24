import json

import pytest
from conftest import TURN, A, B, bind, slackEvent

from codex_slack_bridge.config import BridgeError
from codex_slack_bridge.router import deliverOnce


def test_duplicate_event_is_dispatched_once(router):
    bind(router)
    first = router.slackEvent(slackEvent())
    assert router.slackEvent(slackEvent()) == first
    assert router.api("pc-a", "claim", {})["command"]["id"] == first
    assert router.api("pc-a", "claim", {})["command"] is None


@pytest.mark.parametrize("changes",
                         [{"bot_id": "B1"},
                          {"app_id": "A1"},
                          {"subtype": "message_changed"},
                          {"subtype": "message_deleted"},
                          {"subtype": "bot_message"},
                          {"user": "UNTRUSTED"},
                          {"channel": "C_OTHER"},
                          {"thread_ts": None},
                          {"thread_ts": "101.001"},
                          {"text": ""},
                          {"text": " "},
                          {"text": "x" * 32001}])
def test_ignored_events_never_create_commands(router,
                                              changes):
    bind(router)
    assert router.slackEvent(slackEvent(**changes)) is None
    assert not router.store_.status()["commands"]


def test_other_workspace_and_unbound_thread_ignored(router):
    bind(router)
    event = slackEvent()
    event["team_id"] = "T_OTHER"
    assert router.slackEvent(event) is None
    assert router.slackEvent(slackEvent(thread_ts="999.001")) is None


def test_multi_pc_same_uuid_isolated(router):
    bind(router)
    bind(router, _agent="pc-b", _thread=A, _request=B)
    c1 = router.slackEvent(slackEvent())
    c2 = router.slackEvent(slackEvent("Ev2", thread_ts="100.002"))
    assert router.api("pc-a", "claim", {})["command"]["id"] == c1
    assert router.api("pc-b", "claim", {})["command"]["id"] == c2
    with pytest.raises(BridgeError):
        router.api("pc-b", "result", {"command_id": c1, "status": "accepted", "detail": "accepted"})


def test_per_session_order_waits_for_actual_completion(router):
    bind(router)
    first = router.slackEvent(slackEvent())
    second = router.slackEvent(slackEvent("Ev2"))
    assert router.api("pc-a", "claim", {})["command"]["id"] == first
    router.api("pc-a", "result", {"command_id": first, "status": "accepted", "detail": "accepted"})
    assert router.api("pc-a", "claim", {})["command"] is None
    response = {"event_id": f"{A}:{TURN}",
                "thread_id": A,
                "turn_id": TURN,
                "text": "Done",
                "status": "completed",
                "command_ids": [first]}
    router.api("pc-a", "response", response)
    router.api("pc-a", "response", response)
    assert router.api("pc-a", "claim", {})["command"]["id"] == second
    with router.store_.lock_:
        assert (router.store_.db_.execute("SELECT count(*) FROM outbox WHERE id LIKE 'response:%'").fetchone()[0]
                == 1)


def test_late_ack_cannot_downgrade_completion(router):
    bind(router)
    cid = router.slackEvent(slackEvent())
    router.api("pc-a", "claim", {})
    router.api("pc-a",
               "response",
               {"event_id": f"{A}:{TURN}",
                "thread_id": A,
                "turn_id": TURN,
                "text": "Done",
                "status": "completed",
                "command_ids": [cid]})
    router.api("pc-a", "result", {"command_id": cid, "status": "accepted", "detail": "late"})
    assert router.store_.status()["commands"][0]["status"] == "completed"


def test_restart_does_not_replay_unacknowledged_claim(router):
    bind(router)
    cid = router.slackEvent(slackEvent())
    router.api("pc-a", "claim", {})
    router.store_.recoverRouter()
    router.slackEvent(slackEvent("Ev2"))
    assert router.api("pc-a", "claim", {})["command"] is None
    router.store_.resolveCommand(cid, "rejected")
    assert router.api("pc-a", "claim", {})["command"] is not None


def test_notify_idempotent_and_mismatch_rejected(router):
    payload = {"request_id": A,
               "thread_id": A,
               "title": "Test",
               "text": "Ready",
               "cursor": {"mode": "paginated", "ordinal": -1}}
    first = router.api("pc-a", "notify", payload)
    assert router.api("pc-a", "notify", payload) == first
    assert len(router.store_.status()["outbox"]) == 1
    with pytest.raises(BridgeError):
        router.api("pc-b", "notify", payload)


def test_unknown_slack_delivery_blocks_followups_until_resolved(router):
    payload = {"request_id": A,
               "thread_id": A,
               "title": "Test",
               "text": "Ready",
               "cursor": {"mode": "paginated", "ordinal": -1}}
    router.api("pc-a", "notify", payload)

    class LostAcknowledgement:
        def post(_self,
                 _output):
            return {"status": "uncertain", "detail": "Lost acknowledgement"}

    assert deliverOnce(router.store_, LostAcknowledgement())
    assert not deliverOnce(router.store_, LostAcknowledgement())
    output = router.store_.status()["outbox"][0]
    router.store_.outputResult(output["id"], "sent", _ts="100.001", _manual=True)
    assert router.slackEvent(slackEvent()) is not None


def test_output_chunks_keep_order_after_rate_limit(router):
    bind(router)
    payload = {"request_id": B,
               "thread_id": A,
               "title": "Test",
               "text": "x" * 8000,
               "cursor": {"mode": "paginated", "ordinal": -1}}
    router.api("pc-a", "notify", payload)
    first = router.store_.nextOutput()
    assert len(first["text"]) == 3500
    router.store_.outputResult(first["id"], "pending", _delay=60)
    assert router.store_.nextOutput() is None


def test_unbind_prevents_new_commands_without_interrupting_codex(router):
    bind(router)
    router.api("pc-a", "unbind", {"thread_id": A})
    assert router.slackEvent(slackEvent()) is None


def test_status_does_not_include_message_text(router):
    bind(router)
    router.slackEvent(slackEvent(text="private command contents"))
    assert "private command contents" not in json.dumps(router.store_.status())


def test_rebind_resets_only_observation_and_requires_resolved_commands(router,
                                                                       tmp_path):
    from codex_slack_bridge.store import Store

    original = bind(router)
    state = Store(tmp_path / "agent.sqlite3")
    state.saveCursor(original, {"mode": "paginated", "ordinal": 99})
    router.api("pc-a",
               "rebind",
               {"thread_id": A, "cursor": {"mode": "legacy", "offset": 800, "inode": 12}})
    new = router.api("pc-a", "bindings", {})["bindings"][0]
    assert new["root_ts"] == "100.001"
    assert state.cursor(new) == {"mode": "legacy", "offset": 800, "inode": 12}
    router.slackEvent(slackEvent())
    router.api("pc-a", "claim", {})
    with pytest.raises(BridgeError, match="outstanding"):
        router.api("pc-a",
                   "rebind",
                   {"thread_id": A, "cursor": {"mode": "paginated", "ordinal": 100}})
    state.close()
