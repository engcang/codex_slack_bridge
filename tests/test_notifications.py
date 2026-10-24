import json
import sqlite3
from dataclasses import replace

import pytest
from conftest import TURN, A, B, bind, paginate, slackEvent
from test_codex import append

from codex_slack_bridge.codex import Codex
from codex_slack_bridge.router import deliverOnce


def event(_worked=False,
          _commands=None):
    return Codex.event(A, TURN, "completed", "Result " + "x" * 2000, set(_commands or []), _worked)


def outputs(_router):
    return [dict(row) for row in _router.store_.db_.execute("SELECT * FROM outbox WHERE kind!='root'")]


def test_local_chat_silent_local_work_and_slack_reply_complete(router):
    bind(router)
    router.api("pc-a", "response", event())
    assert outputs(router) == []
    local = event(True)
    local["turn_id"] = B
    local["event_id"] = f"{A}:{B}"
    router.api("pc-a", "response", local)
    text = outputs(router)[0]["text"]
    assert text == "PC task completed.\n\n" + local["text"]
    command = router.slackEvent(slackEvent())
    router.api("pc-a", "claim", {})
    reply = event(False, [command])
    reply["turn_id"] = A
    reply["event_id"] = f"{A}:{A}"
    router.api("pc-a", "response", reply)
    assert outputs(router)[-1]["text"] == reply["text"]
    assert router.store_.status()["commands"][0]["status"] == "completed"


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
@pytest.mark.parametrize("from_slack", [False, True])
def test_full_final_answer_is_delivered_in_order_without_truncation(router,
                                                                    status,
                                                                    from_slack):
    bind(router)
    commands = set()
    if from_slack:
        commands.add(router.slackEvent(slackEvent()))
        router.api("pc-a", "claim", {})
    text = "  Full result ✓\n" * 600 + "LAST LINE\n  "
    response = Codex.event(A, TURN, status, text, commands, True)
    router.api("pc-a", "response", response)
    router.api("pc-a", "response", response)
    delivered = []

    class Slack:
        def post(_self,
                 _output):
            delivered.append(_output)
            return {"status": "sent", "ts": f"200.{len(delivered):06d}"}

    while deliverOnce(router.store_, Slack()):
        pass
    chunks = [row["text"] for row in delivered if row["id"].startswith("response:")]
    assert len(chunks) > 1
    assert all(len(chunk) <= 3500 for chunk in chunks)
    if not from_slack:
        expected = f"PC task {status}.\n\n{text}"
    elif status != "completed":
        expected = f"Codex turn {status}.\n\n{text}".rstrip()
    else:
        expected = text
    assert "".join(chunks) == expected
    assert all(row["root_ts"] == "100.001" for row in delivered)


def test_unrelated_command_reference_does_not_mirror_local_chat(router):
    bind(router)
    router.api("pc-a", "response", event(False, [B]))
    assert outputs(router) == []


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_webhook_mode_silences_excerpts_but_completes_slack_commands(router,
                                                                     status):
    router.config_ = replace(router.config_, automatic_notifications_=False)
    bind(router)
    router.api("pc-a", "response", Codex.progress(A, TURN, 10, "Local work", set()))
    router.api("pc-a", "response", event(True))
    assert outputs(router) == []
    command = router.slackEvent(slackEvent())
    router.api("pc-a", "claim", {})
    acknowledgements = outputs(router)
    progress = Codex.progress(A, B, 11, "Slack work", {command})
    router.api("pc-a", "response", progress)
    router.api("pc-a", "response", progress)
    assert outputs(router) == acknowledgements
    assert router.store_.status()["commands"][0]["status"] == "claimed"
    final = Codex.event(A, B, status, "Full answer " + "x" * 2000, {command}, True)
    router.api("pc-a", "response", final)
    router.api("pc-a", "response", final)
    assert len(outputs(router)) == len(acknowledgements) + 1
    assert outputs(router)[-1]["text"].endswith(final["text"])
    assert router.store_.status()["commands"][0]["status"] == "completed"


def test_progress_throttled_durable_and_never_completes_command(router,
                                                                monkeypatch):
    bind(router)
    command = router.slackEvent(slackEvent())
    router.api("pc-a", "claim", {})
    clock = [1000.0]
    monkeypatch.setattr("codex_slack_bridge.store.time.time", lambda: clock[0])
    first = Codex.progress(A, TURN, 10, "Working", {command})
    router.api("pc-a", "response", first)
    router.api("pc-a", "response", first)
    router.api("pc-a", "response", Codex.progress(A, TURN, 11, "Too soon", {command}))
    assert len([row for row in outputs(router) if row["id"].startswith("progress:")]) == 1
    assert router.store_.status()["commands"][0]["status"] == "claimed"
    clock[0] += 121
    router.api("pc-a", "response", Codex.progress(A, TURN, 12, "Tests finished", {command}))
    assert len([row for row in outputs(router) if row["id"].startswith("progress:")]) == 2
    router.api("pc-a", "response", event(True, [command]))
    clock[0] += 121
    router.api("pc-a", "response", Codex.progress(A, TURN, 13, "Stale", {command}))
    assert outputs(router)[-1]["text"] == event()["text"]


def test_legacy_progress_observer_tracks_work_without_exporting_tools(home):
    codex = Codex(home)
    cursor = codex.snapshot(A)
    append(home, {"type": "task_started", "turn_id": TURN})
    with (home / "sessions/test.jsonl").open("a") as stream:
        stream.write(json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call", "input": "PRIVATE TOOL INPUT"}}) + "\n")
    append(home, {"type": "agent_message", "phase": "commentary", "message": "Implementation finished"})
    events, cursor = codex.responses(A, cursor)
    assert len(events) == 1
    assert events[-1]["status"] == "in_progress"
    assert events[-1]["text"] == "Implementation finished"
    assert "PRIVATE" not in json.dumps(events)
    assert codex.responses(A, cursor)[0] == []
    append(home, {"type": "task_complete", "turn_id": TURN, "last_agent_message": "Done"})
    events, cursor = codex.responses(A, cursor)
    assert events[0]["worked"] is True
    append(home, {"type": "task_started", "turn_id": B}, {"type": "task_complete", "turn_id": B, "last_agent_message": "Just chatting"})
    assert codex.responses(A, cursor)[0][0]["worked"] is False


@pytest.mark.parametrize("finished", [False, True])
def test_paginated_work_and_progress_survive_polling(home,
                                                     finished):
    paginate(home)
    codex = Codex(home)
    cursor = codex.snapshot(A)
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        db.execute("INSERT INTO thread_turns VALUES(?,?,?,?)", (A, TURN, "completed" if finished else "inProgress", 1))
        for ordinal, item in enumerate([{"type": "fileChange", "changes": "PRIVATE PATCH"},
                                        {"type": "agentMessage", "phase": "commentary", "text": "Implementation finished"},
                                        {"type": "agentMessage", "phase": "final_answer", "text": "Done"}],
                                       2):
            db.execute("INSERT INTO thread_items VALUES(?,?,?,?)", (A, TURN, json.dumps(item), ordinal))
    events, cursor = codex.responses(A, cursor)
    assert len(events) == 1
    assert events[0]["worked"] is True
    assert events[0]["status"] == ("completed" if finished else "in_progress")
    assert "PRIVATE" not in json.dumps(events)
    assert codex.responses(A, cursor)[0] == []
