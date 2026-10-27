import json
import socket
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager

import pytest
from conftest import TURN, A, B, bind, paginate, slackEvent
from test_agent import FakeCodex, LocalClient
from test_codex import append

from codex_slack_bridge.agent import Agent
from codex_slack_bridge.codex import Codex
from codex_slack_bridge.config import BridgeError
from codex_slack_bridge.ipc import CodexIpc
from codex_slack_bridge.store import Store


def claim(_router):
    return _router.api("pc-a", "claim", {"steer": True})["command"]


def test_steer_bypasses_busy_task_and_queued_messages(router):
    bind(router)
    first = router.slackEvent(slackEvent())
    assert claim(router)["id"] == first
    router.api("pc-a", "result", {"command_id": first, "status": "accepted", "detail": "Accepted"})
    second = router.slackEvent(slackEvent("Ev2", text="Next task"))
    steer = router.slackEvent(slackEvent("Ev3", text="!steer  Change direction\nnow"))
    assert router.slackEvent(slackEvent("Ev3", text="!steer duplicate")) == steer
    assert router.api("pc-a", "claim", {})["command"] is None
    command = claim(router)
    assert (command["id"], command["mode"], command["text"]) == (steer, "steer", "Change direction\nnow")
    assert router.api("pc-b", "claim", {"steer": True})["command"] is None
    assert claim(router) is None
    router.api("pc-a", "result", {"command_id": steer, "status": "accepted", "detail": "Steered"})
    another = router.slackEvent(slackEvent("Ev4", text="!steer More context"))
    assert claim(router)["id"] == another
    router.api("pc-a", "result", {"command_id": another, "status": "rejected", "detail": "Unavailable"})
    assert claim(router) is None
    router.api("pc-a",
               "response",
               {"event_id": f"{A}:{TURN}",
                "thread_id": A,
                "turn_id": TURN,
                "status": "completed",
                "text": "Done",
                "command_ids": [first, steer]})
    assert claim(router)["id"] == second


@pytest.mark.parametrize("text", ["!steer", " !steer \n\t"])
def test_empty_steer_is_rejected_with_usage(router,
                                            text):
    bind(router)
    command = router.slackEvent(slackEvent(text=text))
    assert claim(router) is None
    row = router.store_.db_.execute("SELECT status,detail FROM commands WHERE id=?", (command,)).fetchone()
    assert row["status"] == "rejected"
    assert row["detail"] == "Usage: !steer <message>"


@pytest.mark.parametrize("text", [" !steering keep this ", "Discuss !steer syntax", "ordinary\ntext"])
def test_other_messages_remain_unchanged(router,
                                         text):
    bind(router)
    router.slackEvent(slackEvent(text=text))
    command = claim(router)
    assert (command["mode"], command["text"]) == ("queue", text)


class SteeringCodex(FakeCodex):
    def steer(_self,
              _thread_id,
              _command_id,
              _text):
        _self.calls_.append(("steer", _thread_id, _command_id, _text))
        return {"status": "accepted", "turn_id": TURN, "detail": "Steered"}


def test_steer_result_survives_ack_loss_and_matches_pc_turn(router,
                                                            tmp_path):
    bind(router)
    cid = router.slackEvent(slackEvent(text="!steer Change direction"))
    client, codex = LocalClient(router), SteeringCodex()
    path = tmp_path / "agent.sqlite3"
    state = Store(path)
    client.fail_ = "result"
    with pytest.raises(BridgeError):
        Agent(client, codex, state).tick()
    assert codex.calls_ == [("steer", A, cid, "Change direction")]
    state.close()
    state = Store(path)
    codex.events_ = [{"event_id": f"{A}:{TURN}",
                     "thread_id": A,
                     "turn_id": TURN,
                     "status": "completed",
                     "text": "Full result from a PC-originated turn",
                     "inputs": [],
                     "command_ids": []}]
    Agent(client, codex, state).tick()
    assert len(codex.calls_) == 1
    assert router.store_.status()["commands"][0]["status"] == "completed"
    assert state.matchInput({"thread_id": A, "turn_id": B, "inputs": []}) == []
    state.close()


def test_stale_steer_does_not_affect_a_later_task(router,
                                                  tmp_path):
    bind(router)
    cid = router.slackEvent(slackEvent(text="!steer Old instruction"))
    router.store_.db_.execute("UPDATE commands SET created_at=? WHERE id=?", (time.time() - 61, cid))
    state = Store(tmp_path / "agent.sqlite3")
    codex = SteeringCodex()
    Agent(LocalClient(router), codex, state).tick()
    assert codex.calls_ == []
    assert router.store_.status()["commands"][0]["status"] == "rejected"
    state.close()


def test_existing_database_commands_migrate_as_queue(tmp_path):
    path = tmp_path / "old.sqlite3"
    state = Store(path)
    state.db_.execute("ALTER TABLE commands DROP COLUMN mode")
    state.close()
    state = Store(path)
    assert next(row for row in state.db_.execute("PRAGMA table_info(commands)") if row["name"] == "mode")["dflt_value"] == "'queue'"
    state.close()


def active(_home):
    paginate(_home)
    with sqlite3.connect(_home / "thread_history_1.sqlite") as db:
        db.execute("INSERT INTO thread_turns VALUES(?,?,?,?)", (A, TURN, "inProgress", 1))


def receive(_socket,
            _size):
    data = b""
    while len(data) < _size:
        chunk = _socket.recv(_size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def send(_socket,
         _value):
    data = json.dumps(_value).encode()
    frame = struct.pack("<I", len(data)) + data
    _socket.sendall(frame[:2])
    _socket.sendall(frame[2:])


@contextmanager
def ipcServer(_home,
              _outcome="success"):
    path = _home / "ipc"
    path.mkdir(mode=0o700)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path / "ipc.sock"))
    (path / "ipc.sock").chmod(0o600)
    server.listen(1)
    server.settimeout(3)
    requests, errors = [], []

    def serve():
        try:
            with server.accept()[0] as peer:
                peer.settimeout(3)
                while True:
                    try:
                        size = struct.unpack("<I", receive(peer, 4))[0]
                        request = json.loads(receive(peer, size))
                    except EOFError:
                        break
                    if request["type"] == "client-discovery-response":
                        assert request["response"] == {"canHandle": False}
                        continue
                    requests.append(request)
                    method = request["method"]
                    response = {"type": "response",
                                "requestId": request["requestId"],
                                "resultType": "success",
                                "method": method,
                                "handledByClientId": "owner",
                                "result": {"clientId": "bridge"}}
                    if method == "thread-owner-discovery":
                        assert request["params"] == {"hostId": "local", "conversationId": A}
                        response["result"] = {"supportsUntrustedAppInput": True}
                    if method == "thread-follower-steer-turn":
                        assert request["targetClientId"] == "owner"
                        assert request["version"] == 1
                        response["result"] = {"result": {"turnId": TURN}}
                        if _outcome == "disconnect":
                            break
                        if _outcome == "wrong-owner":
                            response["handledByClientId"] = "other-window"
                        elif _outcome != "success":
                            response.update(resultType="error", error=_outcome)
                    send(peer, {"type": "broadcast", "method": "ignored"})
                    send(peer, {"type": "client-discovery-request", "requestId": "probe"})
                    send(peer, response)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield requests
    finally:
        thread.join(5)
        server.close()
        assert not thread.is_alive()
        assert not errors


@pytest.mark.parametrize(("outcome", "status"),
                         [("success", "accepted"),
                          ("disconnect", "uncertain"),
                          ("wrong-owner", "uncertain"),
                          ("request-timeout", "uncertain"),
                          ("request-version-mismatch", "rejected"),
                          ("no active turn to steer", "rejected")])
def test_existing_owner_ipc_protocol_without_resume(home,
                                                    monkeypatch,
                                                    outcome,
                                                    status):
    active(home)
    monkeypatch.setattr(Codex, "ownerPid", lambda *_: 123)
    with ipcServer(home, outcome) as requests:
        result = Codex(home).steer(A, B, "Focus on tests")
    assert result["status"] == status
    assert [r["method"] for r in requests] == ["initialize", "thread-owner-discovery", "thread-follower-steer-turn"]
    params = requests[-1]["params"]
    assert params["input"] == [{"type": "text", "text": "Focus on tests", "text_elements": []}]
    assert params["clientUserMessageId"] == B
    assert params["restoreMessage"]["context"]["prompt"] == "Focus on tests"
    if status == "accepted":
        assert result["turn_id"] == TURN


def test_idle_closed_and_changed_turn_never_steer(home,
                                                  monkeypatch):
    codex = Codex(home)
    monkeypatch.setattr(Codex, "ownerPid", lambda *_: None)
    assert codex.steer(A, B, "Instruction")["status"] == "rejected"
    monkeypatch.setattr(Codex, "ownerPid", lambda *_: 123)
    assert "No active" in codex.steer(A, B, "Instruction")["detail"]
    turns = iter([TURN, None])
    monkeypatch.setattr(Codex, "activeTurn", lambda *_: next(turns))
    with ipcServer(home) as requests:
        assert codex.steer(A, B, "Instruction")["status"] == "rejected"
    assert [r["method"] for r in requests] == ["initialize", "thread-owner-discovery"]


def test_legacy_active_turn_and_partial_line(home):
    codex = Codex(home)
    append(home, {"type": "task_started", "turn_id": TURN})
    assert codex.activeTurn(A) == TURN
    append(home, {"type": "turn_aborted", "turn_id": TURN})
    with (home / "sessions/test.jsonl").open("a") as stream:
        stream.write('{"type":"event_msg"')
    assert codex.activeTurn(A) is None


def test_insecure_ipc_socket_is_rejected(home):
    path = home / "ipc"
    path.mkdir(mode=0o755)
    path.chmod(0o755)
    with pytest.raises(BridgeError, match="private"):
        with CodexIpc(home, 1):
            pytest.fail("Insecure socket must not be used")
