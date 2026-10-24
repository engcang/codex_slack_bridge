import sqlite3
from pathlib import Path

import pytest

from codex_slack_bridge.config import RouterConfig
from codex_slack_bridge.router import Router
from codex_slack_bridge.store import Store

A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"
TURN = "00000000-0000-4000-8000-000000000003"


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "router.sqlite3")
    yield instance
    instance.close()


@pytest.fixture
def router(store,
           tmp_path):
    config = RouterConfig("T1",
                          "C1",
                          frozenset({"U1"}),
                          frozenset({"C1"}),
                          {"pc-a": "TOKEN_A", "pc-b": "TOKEN_B"},
                          tmp_path,
                          port_=0)
    return Router(config, store)


def bind(_router,
         _agent="pc-a",
         _thread=A,
         _request=A,
         _cursor=None):
    payload = {"request_id": _request,
               "thread_id": _thread,
               "title": "Test conversation",
               "text": "Ready",
               "cursor": _cursor or {"mode": "paginated", "ordinal": -1}}
    binding = _router.api(_agent, "notify", payload)["binding"]
    row = _router.store_.nextOutput()
    _router.store_.outputResult(row["id"], "sent", _ts="100.001" if _agent == "pc-a" else "100.002")
    return binding


def slackEvent(_event_id="Ev1",
               **_changes):
    event = {"type": "message",
             "channel": "C1",
             "user": "U1",
             "ts": "101.001",
             "thread_ts": "100.001",
             "text": "Continue the task"}
    event.update(_changes)
    return {"type": "event_callback", "team_id": "T1", "event_id": _event_id, "event": event}


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "codex"
    path.mkdir()
    (path / "sessions").mkdir()
    rollout = path / "sessions" / "test.jsonl"
    rollout.write_text("")
    with sqlite3.connect(path / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT,name TEXT,title TEXT,cwd TEXT,source TEXT,rollout_path TEXT,history_mode TEXT,archived INTEGER)")
        db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)",
                   (A,
                    "Conversation",
                    "Original title",
                    str(tmp_path),
                    "vscode",
                    str(rollout),
                    "legacy",
                    0))
    with sqlite3.connect(path / "thread_history_1.sqlite") as db:
        db.executescript("""
        CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,rollout_ordinal INTEGER);
        CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_json TEXT,rollout_ordinal INTEGER);
        """)
    return path


def paginate(_home: Path):
    with sqlite3.connect(_home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET history_mode='paginated'")
