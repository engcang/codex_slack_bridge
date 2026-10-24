import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
from conftest import TURN, A, B, paginate

from codex_slack_bridge.codex import REQUEST_PREFIX, Codex, readonly
from codex_slack_bridge.config import BridgeError


def append(_home,
           *_items):
    with (_home / "sessions/test.jsonl").open("a") as stream:
        for item in _items:
            stream.write(json.dumps({"type": "event_msg", "payload": item}) + "\n")


def test_reads_codex_database_readonly(home):
    db = readonly(home / "state_5.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM threads")
    db.close()


def test_legacy_only_forwards_new_completed_turns(home):
    codex = Codex(home)
    append(home, {"type": "task_complete", "turn_id": B, "last_agent_message": "Historical secret"})
    cursor = codex.snapshot(A)
    append(home,
           {"type": "task_started", "turn_id": TURN},
           {"type": "user_message", "message": f"{REQUEST_PREFIX}{B}\n\nHello"},
           {"type": "agent_message", "phase": "commentary", "message": "Still working"})
    events, cursor = codex.responses(A, cursor)
    assert events == []
    append(home, {"type": "task_complete", "turn_id": TURN, "last_agent_message": "Finished"})
    events, cursor = codex.responses(A, cursor)
    assert events[0]["text"] == "Finished"
    assert events[0]["command_ids"] == [B]
    assert codex.responses(A, cursor)[0] == []


def test_partial_line_does_not_advance_cursor(home):
    codex = Codex(home)
    cursor = codex.snapshot(A)
    path = home / "sessions/test.jsonl"
    payload = json.dumps({"type": "event_msg",
                          "payload": {"type": "task_complete", "turn_id": TURN, "last_agent_message": "Done"}})
    path.write_text(payload[:20])
    assert codex.responses(A, cursor) == ([], {**cursor, "requests": []})
    with path.open("a") as f:
        f.write(payload[20:] + "\n")
    assert codex.responses(A, cursor)[0][0]["text"] == "Done"


def test_truncated_history_refuses_replay(home):
    codex = Codex(home)
    append(home, {"type": "task_complete", "turn_id": TURN, "last_agent_message": "Old"})
    cursor = codex.snapshot(A)
    (home / "sessions/test.jsonl").write_text("")
    with pytest.raises(BridgeError, match="truncated"):
        codex.responses(A, cursor)


def test_mode_migration_refuses_silent_history_loss(home):
    codex = Codex(home)
    cursor = codex.snapshot(A)
    paginate(home)
    with pytest.raises(BridgeError, match="mode changed"):
        codex.responses(A, cursor)


def test_paginated_ignores_reasoning_tools_and_commentary(home):
    paginate(home)
    codex = Codex(home)
    cursor = codex.snapshot(A)
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        db.execute("INSERT INTO thread_turns VALUES(?,?,?,?)", (A, TURN, "inProgress", 1))
        for i, item in enumerate([{"type": "userMessage",
                                   "content": [{"type": "text", "text": f"{REQUEST_PREFIX}{B}\n\nHi"}]},
                                  {"type": "reasoning", "text": "private reasoning"},
                                  {"type": "commandExecution", "text": "private tool output"},
                                  {"type": "agentMessage", "phase": "commentary", "text": "Working"},
                                  {"type": "agentMessage", "phase": "final_answer", "text": "Done"}]):
            db.execute("INSERT INTO thread_items VALUES(?,?,?,?)",
                       (A, TURN, json.dumps(item), i + 2))
    progress, cursor = codex.responses(A, cursor)
    assert len(progress) == 1
    assert progress[0]["status"] == "in_progress"
    assert progress[0]["text"] == "Working"
    assert "private" not in json.dumps(progress)
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        db.execute("UPDATE thread_turns SET status='completed'")
    events, updated = codex.responses(A, cursor)
    assert events[0]["text"] == "Done"
    assert events[0]["command_ids"] == [B]
    assert codex.responses(A, updated)[0] == []


def test_inactive_owner_prevents_process_launch(home,
                                                monkeypatch):
    codex = Codex(home)
    monkeypatch.setattr(codex, "ownerPid", lambda _unused: None)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail_("Must not launch a process"))
    assert codex.queue(A, B, "Hello")["status"] == "rejected"


def test_queue_uses_literal_argv_and_explicit_uuid(home,
                                                   monkeypatch):
    codex = Codex(home, "/example/codex")
    monkeypatch.setattr(codex, "ownerPid", lambda _unused: 42)
    monkeypatch.setattr(codex, "executable", lambda: "/example/codex")
    calls = []

    def run(_argv,
            **_kwargs):
        calls.append((_argv, _kwargs))
        return SimpleNamespace(returncode=0, stdout=f"Queued message {TURN} for thread {A}.\n")

    monkeypatch.setattr(subprocess, "run", run)
    text = '$(touch /tmp/should-not-exist); `id` "quoted"\n--help'
    assert codex.queue(A, B, text)["status"] == "accepted"
    argv, kwargs = calls[0]
    assert argv == ["/example/codex",
                    "queue",
                    "--thread",
                    A,
                    "--message",
                    text]
    assert not kwargs.get("shell")
    assert "resume" not in argv


@pytest.mark.parametrize("outcome", ["timeout", "exit", "unrecognized"])
def test_ambiguous_dispatch_never_claims_rejection_or_retries(home,
                                                              monkeypatch,
                                                              outcome):
    codex = Codex(home)
    monkeypatch.setattr(codex, "ownerPid", lambda _unused: 42)
    monkeypatch.setattr(codex, "executable", lambda: "/example/codex")
    count = []

    def run(*_args,
            **_kwargs):
        count.append(1)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("codex", 30)
        return SimpleNamespace(returncode=1 if outcome == "exit" else 0, stdout="Unknown response")

    monkeypatch.setattr(subprocess, "run", run)
    assert codex.queue(A, B, "Hello")["status"] == "uncertain"
    assert len(count) == 1


def test_child_agent_cannot_be_registered(home):
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET source='subagent'")
    with pytest.raises(BridgeError, match="root conversation"):
        Codex(home).thread(A)


def test_registration_during_partial_write_starts_at_complete_line(home):
    codex = Codex(home)
    append(home, {"type": "task_started", "turn_id": TURN})
    path = home / "sessions/test.jsonl"
    complete_size = path.stat().st_size
    line = (json.dumps({"type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": TURN, "last_agent_message": "Done"}})
            + "\n")
    with path.open("a") as stream:
        stream.write(line[:25])
    cursor = codex.snapshot(A)
    assert cursor["offset"] == complete_size
    with path.open("a") as stream:
        stream.write(line[25:])
    assert codex.responses(A, cursor)[0][0]["text"] == "Done"


def test_snapshot_does_not_skip_an_unfinished_earlier_turn(home):
    paginate(home)
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        db.executemany("INSERT INTO thread_turns VALUES(?,?,?,?)",
                       [(A, B, "inProgress", 1),
                        (A, TURN, "completed", 10)])
    assert Codex(home).snapshot(A)["ordinal"] == -1


def test_sessions_order_newest_first_and_exclude_archived_and_child_threads(home,
                                                                            monkeypatch):
    with sqlite3.connect(home / "state_5.sqlite") as db:
        for index in range(1, 9):
            db.execute("INSERT INTO threads(id,name,title,cwd,source,rollout_path,history_mode,archived,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (f"00000000-0000-4000-8000-{index + 10:012d}", "Conversation", "", "/project", "vscode", "", "legacy", 0, index))
        db.execute("UPDATE threads SET archived=1 WHERE updated_at=8")
        db.execute("UPDATE threads SET source='subagent' WHERE updated_at=7")
    codex = Codex(home)
    monkeypatch.setattr(codex, "ownerPid", lambda _thread: None)
    sessions = codex.sessions()
    assert len(sessions) == 7
    assert sessions[0]["id"].endswith("000000000016")
    assert sessions[-1]["id"] == A


def test_paginated_plain_prompt_references_are_scoped_to_new_input(home):
    paginate(home)
    codex = Codex(home)
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        db.execute("INSERT INTO thread_turns VALUES(?,?,?,?)", (A, TURN, "inProgress", 1))
        db.execute("INSERT INTO thread_items VALUES(?,?,?,?)",
                   (A, TURN, json.dumps({"type": "userMessage", "content": [{"type": "text", "text": "Same prompt"}]}), 2))
    boundary = codex.inputCursor(A)
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        db.execute("INSERT INTO thread_items VALUES(?,?,?,?)",
                   (A, TURN, json.dumps({"type": "userMessage", "content": [{"type": "text", "text": "Same prompt"}]}), 3))
        db.execute("INSERT INTO thread_items VALUES(?,?,?,?)",
                   (A, TURN, json.dumps({"type": "agentMessage", "phase": "final_answer", "text": "Done"}), 4))
        db.execute("UPDATE thread_turns SET status='completed'")
    events, _ = codex.responses(A, {"mode": "paginated", "ordinal": -1})
    assert boundary == {"mode": "paginated", "position": 2}
    assert events[0]["inputs"] == [codex.inputReference("Same prompt", "paginated", 2), codex.inputReference("Same prompt", "paginated", 3)]
    assert "Same prompt" not in json.dumps(events)
