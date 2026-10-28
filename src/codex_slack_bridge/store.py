from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .config import BridgeError

SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, thread_id TEXT NOT NULL,
 title TEXT NOT NULL, channel_id TEXT NOT NULL, root_ts TEXT,
 cursor_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, generation INTEGER NOT NULL DEFAULT 1,
 UNIQUE(agent_id,thread_id), UNIQUE(channel_id,root_ts)
);
CREATE TABLE IF NOT EXISTS notifications (
 id TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES bindings(id)
);
CREATE TABLE IF NOT EXISTS commands (
 id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
 binding_id TEXT NOT NULL REFERENCES bindings(id), user_id TEXT NOT NULL,
 text TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
 queue_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS command_binding_status ON commands(binding_id,status);
CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS progress (
 binding_id TEXT NOT NULL, turn_id TEXT NOT NULL, sent_at REAL NOT NULL,
 PRIMARY KEY(binding_id,turn_id)
);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES bindings(id),
 kind TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 detail TEXT NOT NULL DEFAULT '', slack_ts TEXT, available_at REAL NOT NULL DEFAULT 0,
 created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_binding_status ON outbox(binding_id,status,kind);
CREATE TABLE IF NOT EXISTS slack_cleanup (
 channel_id TEXT NOT NULL, slack_ts TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', available_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(channel_id,slack_ts)
);
CREATE TABLE IF NOT EXISTS cursors (binding_id TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
 command_id TEXT PRIMARY KEY, result_json TEXT, reported INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS input_links (
 command_id TEXT PRIMARY KEY REFERENCES attempts(command_id), thread_id TEXT NOT NULL,
 prompt_hash TEXT NOT NULL, cursor_json TEXT NOT NULL, turn_id TEXT
);
"""


class Store:
    """Only Bridge-owned databases are ever opened for writing."""

    def __init__(_self,
                 _path: Path):
        _path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _self.db_ = sqlite3.connect(_path, check_same_thread=False, timeout=10, isolation_level=None)
        _path.chmod(0o600)
        _self.db_.row_factory = sqlite3.Row
        _self.db_.execute("PRAGMA journal_mode=WAL")
        _self.db_.execute("PRAGMA foreign_keys=ON")
        _self.db_.executescript(SCHEMA)
        if "mode" not in {row["name"] for row in _self.db_.execute("PRAGMA table_info(commands)")}:
            _self.db_.execute("ALTER TABLE commands ADD COLUMN mode TEXT NOT NULL DEFAULT 'queue'")
        _self.lock_ = threading.RLock()

    def close(_self):
        _self.db_.close()

    @contextmanager
    def transaction(_self):
        with _self.lock_:
            _self.db_.execute("BEGIN IMMEDIATE")
            try:
                yield _self.db_
                _self.db_.execute("COMMIT")
            except BaseException:
                _self.db_.execute("ROLLBACK")
                raise

    @staticmethod
    def outbox(_db,
               _binding_id: str,
               _key: str,
               _text: str,
               _kind: str = "reply"):
        #Codex: Slack recommends short messages; deterministic chunks survive receipt retries.
        chunks = [_text[i : i + 3500] for i in range(0, len(_text), 3500)] or [""]
        for i, chunk in enumerate(chunks):
            _db.execute("INSERT OR IGNORE INTO outbox(id,binding_id,kind,text,created_at) VALUES(?,?,?,?,?)",
                        (f"{_key}:{i}", _binding_id, _kind if i == 0 else "reply", chunk, time.time()))

    def notify(_self,
               _agent_id: str,
               _request: dict,
               _channel: str) -> dict:
        with _self.transaction() as db:
            previous = db.execute("SELECT binding_id FROM notifications WHERE id=?",
                                  (_request["request_id"],)).fetchone()
            if previous:
                binding = db.execute("SELECT * FROM bindings WHERE id=?", (previous[0],)).fetchone()
                if binding["agent_id"] != _agent_id or binding["thread_id"] != _request["thread_id"]:
                    raise BridgeError("Notification identity conflict")
                return _self.binding(binding)
            binding = db.execute("SELECT * FROM bindings WHERE agent_id=? AND thread_id=?",
                                 (_agent_id, _request["thread_id"])).fetchone()
            fresh = binding is None
            if fresh:
                binding_id = str(uuid4())
                db.execute("INSERT INTO bindings(id,agent_id,thread_id,title,channel_id,cursor_json) VALUES(?,?,?,?,?,?)",
                           (binding_id,
                            _agent_id,
                            _request["thread_id"],
                            _request["title"],
                            _channel,
                            json.dumps(_request["cursor"])))
                binding = db.execute("SELECT * FROM bindings WHERE id=?", (binding_id,)).fetchone()
            elif binding["channel_id"] != _channel or not binding["enabled"]:
                raise BridgeError("This conversation is bound to another channel or was disabled")
            binding_id = binding["id"]
            db.execute("INSERT INTO notifications VALUES(?,?)", (_request["request_id"], binding_id))
            text = _request["text"]
            if fresh:
                text = f"Codex: {_request['title']}\nPC: {_agent_id}\n\n{text}"
            _self.outbox(db,
                         binding_id,
                         f"notify:{_request['request_id']}",
                         text,
                         "root" if fresh else "reply")
            return _self.binding(binding)

    @staticmethod
    def binding(_row) -> dict:
        result = dict(_row)
        result["cursor"] = json.loads(result.pop("cursor_json"))
        return result

    def bindings(_self,
                 _agent_id: str) -> list[dict]:
        with _self.lock_:
            return [_self.binding(r)
                    for r in _self.db_.execute("SELECT * FROM bindings WHERE agent_id=? AND enabled=1",
                                               (_agent_id,))]

    def ingest(_self,
               _event_id: str,
               _channel: str,
               _root_ts: str,
               _user: str,
               _text: str) -> str | None:
        with _self.transaction() as db:
            old = db.execute("SELECT id FROM commands WHERE event_id=?", (_event_id,)).fetchone()
            if old:
                return old[0]
            binding = db.execute("SELECT * FROM bindings WHERE channel_id=? AND root_ts=? AND enabled=1",
                                 (_channel, _root_ts)).fetchone()
            if not binding:
                return None
            command_id, now = str(uuid4()), time.time()
            parts = _text.lstrip().split(maxsplit=1)
            mode = "steer" if parts and parts[0] == "!steer" else "queue"
            if mode == "steer":
                _text = parts[1].strip() if len(parts) == 2 else ""
            status = "pending" if _text else "rejected"
            detail = "Usage: !steer <message>" if not _text else ""
            db.execute("INSERT INTO commands(id,event_id,binding_id,user_id,text,status,created_at,updated_at,mode,detail) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (command_id, _event_id, binding["id"], _user, _text, status, now, now, mode, detail))
            _self.outbox(db,
                         binding["id"],
                         f"received:{command_id}",
                         detail or (f"Steer received; contacting {binding['agent_id']}." if mode == "steer" else f"Received; waiting for {binding['agent_id']}."))
            return command_id

    def claim(_self,
              _agent_id: str,
              _steer: bool = False) -> dict | None:
        with _self.transaction() as db:
            row = db.execute("SELECT c.*,b.thread_id FROM commands c JOIN bindings b ON b.id=c.binding_id "
                             "WHERE b.agent_id=? AND b.enabled=1 AND c.status='pending' AND (c.mode='queue' OR ?) AND NOT EXISTS "
                             "(SELECT 1 FROM commands busy WHERE busy.binding_id=c.binding_id "
                             "AND (busy.status='claimed' OR (c.mode='queue' AND busy.status IN ('accepted','uncertain')))) "
                             "ORDER BY (c.mode='steer') DESC,c.created_at,c.rowid LIMIT 1",
                             (_agent_id, _steer)).fetchone()
            if not row:
                return None
            db.execute("UPDATE commands SET status='claimed',updated_at=? WHERE id=?",
                       (time.time(), row["id"]))
            return dict(row)

    def result(_self,
               _agent_id: str,
               _command_id: str,
               _result: dict):
        with _self.transaction() as db:
            row = db.execute("SELECT c.* FROM commands c JOIN bindings b ON b.id=c.binding_id WHERE c.id=? AND b.agent_id=?",
                             (_command_id, _agent_id)).fetchone()
            if not row:
                raise BridgeError("Unknown command for this Agent")
            if row["status"] in ("completed", "rejected"):
                return
            if row["status"] == "pending":
                raise BridgeError("Command has not been claimed")
            db.execute("UPDATE commands SET status=?,detail=?,queue_id=?,updated_at=? WHERE id=?",
                       (_result["status"],
                        _result["detail"],
                        _result.get("queue_id"),
                        time.time(),
                        _command_id))
            _self.outbox(db, row["binding_id"], f"result:{_command_id}", _result["detail"])

    def response(_self,
                 _agent_id: str,
                 _event: dict,
                 _automatic_notifications: bool = True):
        with _self.transaction() as db:
            binding = db.execute("SELECT * FROM bindings WHERE agent_id=? AND thread_id=? AND enabled=1",
                                 (_agent_id, _event["thread_id"])).fetchone()
            if not binding:
                raise BridgeError("Unknown or disabled conversation")
            receipt = f"{_agent_id}:{_event['event_id']}"
            if db.execute("SELECT 1 FROM receipts WHERE id=?", (receipt,)).fetchone():
                return
            db.execute("INSERT INTO receipts VALUES(?)", (receipt,))
            if _event["status"] == "in_progress":
                if not _automatic_notifications:
                    return
                terminal = f"{_agent_id}:{_event['thread_id']}:{_event['turn_id']}"
                if db.execute("SELECT 1 FROM receipts WHERE id=?", (terminal,)).fetchone():
                    return
                last = db.execute("SELECT sent_at FROM progress WHERE binding_id=? AND turn_id=?",
                                  (binding["id"], _event["turn_id"])).fetchone()
                now = time.time()
                if not _event.get("worked") or not _event["text"].strip() or (last and now - last[0] < 120):
                    return
                db.execute("INSERT OR REPLACE INTO progress VALUES(?,?,?)", (binding["id"], _event["turn_id"], now))
                _self.outbox(db, binding["id"], f"progress:{receipt}", "Progress: " + _event["text"][:600])
                return
            from_slack = False
            for command in _event.get("command_ids", []):
                if db.execute("SELECT 1 FROM commands WHERE id=? AND binding_id=?", (command, binding["id"])).fetchone():
                    from_slack = True
                db.execute("UPDATE commands SET status='completed',detail=?,updated_at=? "
                           "WHERE id=? AND binding_id=? AND status IN ('claimed','accepted','uncertain')",
                           (f"Codex turn {_event['status']}", time.time(), command, binding["id"]))
            text = _event["text"]
            if not from_slack:
                if not _automatic_notifications or not _event.get("worked"):
                    return
                text = f"PC task {_event['status']}." + (f"\n\n{text}" if text else "")
                _self.outbox(db, binding["id"], f"response:{receipt}", text)
                return
            if _event["status"] != "completed":
                text = f"Codex turn {_event['status']}.\n\n{text}".rstrip()
            if not text and _event.get("command_ids"):
                text = "Codex completed the turn without a text response."
            if text:
                _self.outbox(db, binding["id"], f"response:{receipt}", text)

    def nextOutput(_self) -> dict | None:
        with _self.transaction() as db:
            row = db.execute("SELECT o.*,b.channel_id,b.root_ts FROM outbox o JOIN bindings b ON b.id=o.binding_id "
                             "WHERE b.enabled=1 AND o.status='pending' AND o.available_at<=? AND (o.kind='root' OR b.root_ts IS NOT NULL) "
                             "AND NOT EXISTS (SELECT 1 FROM outbox earlier WHERE earlier.binding_id=o.binding_id "
                             "AND earlier.rowid<o.rowid AND earlier.status!='sent') "
                             "ORDER BY o.created_at,o.rowid LIMIT 1",
                             (time.time(),)).fetchone()
            if row:
                db.execute("UPDATE outbox SET status='sending' WHERE id=?", (row["id"],))
                return dict(row)
            return None

    def outputResult(_self,
                     _output_id: str,
                     _status: str,
                     *,
                     _ts: str | None = None,
                     _detail: str = "",
                     _delay: float = 0,
                     _manual: bool = False):
        with _self.transaction() as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (_output_id,)).fetchone()
            if not row:
                raise BridgeError("Unknown output ID")
            if row["status"] == "sent":
                return
            if _manual and row["status"] not in ("failed", "uncertain"):
                raise BridgeError("Only failed or uncertain outputs can be manually resolved")
            if _status == "sent" and not _ts:
                raise BridgeError("A Slack message timestamp is required")
            db.execute("UPDATE outbox SET status=?,slack_ts=?,detail=?,available_at=? WHERE id=?",
                       (_status, _ts, _detail, time.time() + _delay, _output_id))
            if _status == "sent" and row["kind"] == "root":
                db.execute("UPDATE bindings SET root_ts=? WHERE id=?", (_ts, row["binding_id"]))

    def recoverRouter(_self):
        with _self.transaction() as db:
            db.execute("UPDATE commands SET status='uncertain',detail='Router restarted during dispatch; inspect the conversation' WHERE status='claimed'")
            db.execute("UPDATE outbox SET status='uncertain',detail='Router restarted during Slack transmission; inspect Slack' WHERE status='sending'")

    def nextCleanup(_self) -> dict | None:
        with _self.transaction() as db:
            now = time.time()
            #Codex: Share the deletion cooldown across threads, including after a restart.
            if db.execute("SELECT 1 FROM slack_cleanup WHERE status='pending' AND available_at>? LIMIT 1", (now,)).fetchone():
                return None
            row = db.execute("WITH replies AS ("
                             "SELECT DISTINCT b.id AS binding_id,b.channel_id,b.root_ts,o.slack_ts "
                             "FROM outbox o JOIN bindings b ON b.id=o.binding_id "
                             "WHERE b.enabled=1 AND b.root_ts IS NOT NULL AND o.kind='reply' "
                             "AND o.status='sent' AND o.slack_ts IS NOT NULL "
                             "AND NOT EXISTS (SELECT 1 FROM bindings root WHERE root.channel_id=b.channel_id AND root.root_ts=o.slack_ts)"
                             "), ranked AS ("
                             "SELECT *,ROW_NUMBER() OVER (PARTITION BY binding_id ORDER BY CAST(slack_ts AS REAL) DESC) AS position FROM replies"
                             ") SELECT r.* FROM ranked r LEFT JOIN slack_cleanup c "
                             "ON c.channel_id=r.channel_id AND c.slack_ts=r.slack_ts "
                             "WHERE r.position>50 AND (c.status IS NULL OR c.status='pending') "
                             "ORDER BY CAST(r.slack_ts AS REAL),r.binding_id LIMIT 1").fetchone()
            if not row:
                return None
            #Codex: Deleting is idempotent: retry the same timestamp if the acknowledgement is lost.
            db.execute("INSERT INTO slack_cleanup(channel_id,slack_ts,status,available_at) VALUES(?,?,'pending',?) "
                       "ON CONFLICT(channel_id,slack_ts) DO UPDATE SET available_at=excluded.available_at",
                       (row["channel_id"], row["slack_ts"], now + 60))
            return dict(row)

    def cleanupResult(_self,
                      _channel: str,
                      _ts: str,
                      _status: str,
                      _detail: str = "",
                      _delay: float = 0):
        if _status not in ("deleted", "pending", "failed"):
            raise BridgeError("Invalid cleanup status")
        with _self.transaction() as db:
            db.execute("UPDATE slack_cleanup SET status=?,detail=?,available_at=? WHERE channel_id=? AND slack_ts=?",
                       (_status, _detail, time.time() + _delay, _channel, _ts))

    def expireClaims(_self,
                     _age: float = 120):
        with _self.transaction() as db:
            db.execute("UPDATE commands SET status='uncertain',detail='Agent acknowledgement missing; no automatic retry' WHERE status='claimed' AND updated_at<?",
                       (time.time() - _age,))

    def resolveCommand(_self,
                       _command_id: str,
                       _status: str):
        if _status not in ("completed", "rejected"):
            raise BridgeError("Resolve commands as completed or rejected")
        with _self.transaction() as db:
            count = db.execute("UPDATE commands SET status=?,detail='Manually resolved by operator',updated_at=? "
                               "WHERE id=? AND status IN ('claimed','accepted','uncertain')",
                               (_status, time.time(), _command_id)).rowcount
            if not count:
                raise BridgeError("Command does not require resolution")

    def status(_self) -> dict:
        with _self.lock_:
            return {"bindings": [dict(r)
                                 for r in _self.db_.execute("SELECT id,agent_id,thread_id,title,channel_id,root_ts,enabled FROM bindings")],
                    "commands": [dict(r)
                                 for r in _self.db_.execute("SELECT id,binding_id,status,detail,queue_id FROM commands ORDER BY created_at DESC LIMIT 50")],
                    "outbox": [dict(r)
                               for r in _self.db_.execute("SELECT id,kind,status,detail FROM outbox WHERE status!='sent' ORDER BY created_at LIMIT 50")],
                    "cleanup": [dict(r)
                                for r in _self.db_.execute("SELECT channel_id,slack_ts,status,detail FROM slack_cleanup WHERE status!='deleted' LIMIT 50")]}

    def disable(_self,
                _agent_id: str,
                _thread_id: str):
        with _self.transaction() as db:
            row = db.execute("SELECT id FROM bindings WHERE agent_id=? AND thread_id=?",
                             (_agent_id, _thread_id)).fetchone()
            if not row:
                raise BridgeError("Unknown conversation")
            db.execute("UPDATE bindings SET enabled=0 WHERE id=?", (row[0],))
            db.execute("UPDATE commands SET status='rejected',detail='Binding disabled' WHERE binding_id=? AND status='pending'",
                       (row[0],))

    def rebind(_self,
               _agent_id: str,
               _thread_id: str,
               _cursor: dict):
        with _self.transaction() as db:
            row = db.execute("SELECT id FROM bindings WHERE agent_id=? AND thread_id=?",
                             (_agent_id, _thread_id)).fetchone()
            if not row:
                raise BridgeError("Unknown conversation; register it with notify first")
            if db.execute("SELECT 1 FROM commands WHERE binding_id=? AND status IN ('claimed','accepted','uncertain')",
                          (row[0],)).fetchone():
                raise BridgeError("Resolve outstanding commands before resetting observation")
            db.execute("UPDATE bindings SET cursor_json=?,enabled=1,generation=generation+1 WHERE id=?",
                       (json.dumps(_cursor), row[0]))

    def cursor(_self,
               _binding: dict) -> dict:
        with _self.lock_:
            key = f"{_binding['id']}:{_binding['generation']}"
            row = _self.db_.execute("SELECT value FROM cursors WHERE binding_id=?", (key,)).fetchone()
            return json.loads(row[0]) if row else _binding["cursor"]

    def saveCursor(_self,
                   _binding: dict,
                   _cursor: dict):
        with _self.transaction() as db:
            db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(binding_id) DO UPDATE SET value=excluded.value",
                       (f"{_binding['id']}:{_binding['generation']}", json.dumps(_cursor)))

    def beginAttempt(_self,
                     _command_id: str) -> bool:
        with _self.transaction() as db:
            return bool(db.execute("INSERT OR IGNORE INTO attempts(command_id) VALUES(?)",
                                   (_command_id,)).rowcount)

    def finishAttempt(_self,
                      _command_id: str,
                      _result: dict):
        with _self.transaction() as db:
            db.execute("UPDATE attempts SET result_json=? WHERE command_id=?",
                       (json.dumps(_result), _command_id))
            if _result.get("status") == "accepted" and _result.get("turn_id"):
                db.execute("UPDATE input_links SET turn_id=? WHERE command_id=?", (_result["turn_id"], _command_id))

    def trackInput(_self,
                   _command_id: str,
                   _thread_id: str,
                   _text: str,
                   _cursor: dict):
        with _self.transaction() as db:
            db.execute("INSERT INTO input_links(command_id,thread_id,prompt_hash,cursor_json) VALUES(?,?,?,?)",
                       (_command_id, _thread_id, hashlib.sha256(_text.encode()).hexdigest(), json.dumps(_cursor)))

    def matchInput(_self,
                   _event: dict) -> list[str]:
        with _self.transaction() as db:
            rows = db.execute("SELECT i.*,a.result_json FROM input_links i JOIN attempts a ON a.command_id=i.command_id "
                              "WHERE i.thread_id=? AND (i.turn_id IS NULL OR i.turn_id=?) ORDER BY i.rowid",
                              (_event["thread_id"], _event["turn_id"])).fetchall()
            commands = []
            for row in rows:
                result = json.loads(row["result_json"]) if row["result_json"] else {}
                if result.get("status") == "rejected":
                    continue
                cursor = json.loads(row["cursor_json"])
                matched = any(item["hash"] == row["prompt_hash"] and item["mode"] == cursor["mode"]
                              and item["position"] > cursor["position"] for item in _event.get("inputs", []))
                if row["turn_id"] == _event["turn_id"] or matched:
                    db.execute("UPDATE input_links SET turn_id=? WHERE command_id=?",
                               (_event["turn_id"], row["command_id"]))
                    commands.append(row["command_id"])
            return commands

    def pendingResults(_self) -> list[tuple[str, dict]]:
        with _self.lock_:
            rows = _self.db_.execute("SELECT command_id,result_json FROM attempts WHERE reported=0").fetchall()
        return [(r[0],
                 json.loads(r[1])
                 if r[1]
                 else {"status": "uncertain",
                       "detail": "Agent restarted during dispatch; no automatic retry"})
                for r in rows]

    def resultReported(_self,
                       _command_id: str):
        with _self.transaction() as db:
            db.execute("UPDATE attempts SET reported=1 WHERE command_id=?", (_command_id,))
