import pytest
from conftest import TURN, A, B, bind, slackEvent

from codex_slack_bridge.agent import Agent
from codex_slack_bridge.codex import Codex
from codex_slack_bridge.config import BridgeError
from codex_slack_bridge.store import Store


class LocalClient:
    def __init__(_self,
                 _router):
        _self.router_ = _router
        _self.fail_ = None

    def call(_self,
             _operation,
             _payload=None):
        result = _self.router_.api("pc-a", _operation, _payload or {})
        if _operation == _self.fail_:
            _self.fail_ = None
            raise BridgeError("Lost acknowledgement")
        return result


class FakeCodex:
    def __init__(_self):
        _self.calls_ = []
        _self.events_ = []

    def inputCursor(_self,
                    _thread_id):
        return {"mode": "paginated", "position": -1}

    def queue(_self,
              _thread_id,
              _command_id,
              _text):
        _self.calls_.append((_thread_id, _command_id, _text))
        return {"status": "accepted", "detail": "Accepted", "queue_id": TURN}

    def responses(_self,
                  _thread_id,
                  _cursor):
        return _self.events_, {"mode": "paginated", "ordinal": len(_self.events_)}


def test_lost_result_ack_does_not_execute_twice(router,
                                                tmp_path):
    bind(router)
    router.slackEvent(slackEvent())
    client, codex = LocalClient(router), FakeCodex()
    state = Store(tmp_path / "agent.sqlite3")
    agent = Agent(client, codex, state)
    client.fail_ = "result"
    with pytest.raises(BridgeError):
        agent.tick()
    agent.tick()
    assert len(codex.calls_) == 1
    state.close()


def test_crash_after_local_attempt_marker_is_not_replayed(router,
                                                          tmp_path):
    bind(router)
    cid = router.slackEvent(slackEvent())
    router.api("pc-a", "claim", {})
    state = Store(tmp_path / "agent.sqlite3")
    state.beginAttempt(cid)
    state.close()
    state = Store(tmp_path / "agent.sqlite3")
    codex = FakeCodex()
    Agent(LocalClient(router), codex, state).tick()
    assert codex.calls_ == []
    assert router.store_.status()["commands"][0]["status"] == "uncertain"
    state.close()


def test_response_ack_loss_keeps_cursor_and_router_deduplicates(router,
                                                                tmp_path):
    binding = bind(router)
    client, codex = LocalClient(router), FakeCodex()
    state = Store(tmp_path / "agent.sqlite3")
    codex.events_ = [{"event_id": f"{A}:{TURN}",
                      "thread_id": A,
                      "turn_id": TURN,
                      "status": "completed",
                      "worked": True,
                      "text": "Done",
                      "command_ids": []}]
    client.fail_ = "response"
    Agent(client, codex, state).tick()
    assert state.cursor(binding)["ordinal"] == -1
    Agent(client, codex, state).tick()
    assert state.cursor(binding)["ordinal"] == 1
    assert len([o for o in router.store_.status()["outbox"] if o["id"].startswith("response:")]) == 1
    state.close()


def test_plain_prompt_association_survives_restart_and_excludes_other_turns(tmp_path):
    path = tmp_path / "agent.sqlite3"
    state = Store(path)
    state.beginAttempt(B)
    state.trackInput(B, A, "Same prompt", {"mode": "legacy", "position": 100})
    state.finishAttempt(B, {"status": "accepted", "queue_id": TURN, "detail": "Accepted"})
    state.close()
    state = Store(path)
    event = {"thread_id": A, "turn_id": TURN, "inputs": [Codex.inputReference("Same prompt", "legacy", 90)]}
    assert state.matchInput(event) == []
    event["inputs"] = [Codex.inputReference("Different prompt", "legacy", 110)]
    assert state.matchInput(event) == []
    event["inputs"] = [Codex.inputReference("Same prompt", "legacy", 110)]
    assert state.matchInput({**event, "thread_id": B}) == []
    assert state.matchInput(event) == [B]
    assert state.matchInput({**event, "inputs": []}) == [B]
    assert state.matchInput({**event, "turn_id": B}) == []
    state.close()


def test_rejected_queue_never_associates_a_later_matching_pc_prompt(tmp_path):
    state = Store(tmp_path / "agent.sqlite3")
    state.beginAttempt(B)
    state.trackInput(B, A, "Same prompt", {"mode": "paginated", "position": 2})
    state.finishAttempt(B, {"status": "rejected", "detail": "Closed conversation"})
    assert state.matchInput({"thread_id": A, "turn_id": TURN, "inputs": [Codex.inputReference("Same prompt", "paginated", 3)]}) == []
    state.close()
