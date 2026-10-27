from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from uuid import UUID

from .config import BridgeError
from .ipc import CodexIpc, IpcError

REQUEST_PREFIX = "Slack bridge request: "
REQUEST_RE = re.compile(r"^Slack bridge request: ([0-9a-f-]{36})$", re.MULTILINE)


def threadUuid(_value: str) -> str:
    try:
        return str(UUID(_value))
    except (ValueError, TypeError, AttributeError):
        raise BridgeError("An explicit Codex thread UUID is required") from None


def readonly(_path: Path) -> sqlite3.Connection:
    if not _path.is_file():
        raise BridgeError(f"Codex history file is unavailable: {_path.name}")
    db = sqlite3.connect(_path.as_uri() + "?mode=ro", uri=True, timeout=2)
    db.execute("PRAGMA query_only=ON")
    db.row_factory = sqlite3.Row
    return db


class Codex:
    def __init__(_self,
                 _home: Path,
                 _binary: str = "codex",
                 _timeout: float = 30):
        _self.home_, _self.binary_, _self.timeout_ = _home.resolve(), _binary, _timeout

    def executable(_self) -> str:
        executable = shutil.which(_self.binary_)
        if not executable:
            raise BridgeError("Codex executable not found; configure agent.codex_bin")
        return executable

    def check(_self) -> str:
        env = {**os.environ, "CODEX_HOME": str(_self.home_)}
        try:
            result = subprocess.run([_self.executable(), "queue", "--help"],
                                    env=env,
                                    capture_output=True,
                                    text=True,
                                    timeout=10,
                                    check=False)
            if (result.returncode
                or "--thread" not in result.stdout
                or "--message" not in result.stdout):
                raise BridgeError("Installed Codex does not support codex queue --thread --message")
            result = subprocess.run([_self.executable(), "--version"],
                                    env=env,
                                    capture_output=True,
                                    text=True,
                                    timeout=10,
                                    check=False)
            return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            raise BridgeError("Could not inspect the Codex executable") from None

    def thread(_self,
               _thread_id: str) -> dict:
        _thread_id = threadUuid(_thread_id)
        with closing(readonly(_self.home_ / "state_5.sqlite")) as db:
            row = db.execute("SELECT id,name,title,cwd,source,rollout_path,history_mode,archived "
                             "FROM threads WHERE id=?",
                             (_thread_id,)).fetchone()
        if not row or row["archived"]:
            raise BridgeError("The requested thread is missing or archived")
        if row["source"] not in ("vscode", "cli"):
            raise BridgeError("Register the visible root conversation, not a child agent thread")
        return dict(row)

    def sessions(_self) -> list[dict]:
        with closing(readonly(_self.home_ / "state_5.sqlite")) as db:
            rows = db.execute("SELECT id,name,title,cwd FROM threads "
                              "WHERE archived=0 AND source='vscode' ORDER BY updated_at DESC,id").fetchall()
        return [{**dict(row), "is_open": bool(_self.ownerPid(row["id"]))} for row in rows]

    def inputCursor(_self,
                    _thread_id: str) -> dict:
        thread = _self.thread(_thread_id)
        if thread["history_mode"] == "paginated":
            with closing(readonly(_self.home_ / "thread_history_1.sqlite")) as db:
                position = db.execute("SELECT COALESCE(MAX(rollout_ordinal),-1) FROM thread_items WHERE thread_id=?",
                                      (_thread_id,)).fetchone()[0]
            return {"mode": "paginated", "position": position}
        if thread["history_mode"] != "legacy":
            raise BridgeError("Unsupported Codex history mode")
        return {"mode": "legacy", "position": _self.completeOffset(_self.rollout(thread))}

    @staticmethod
    def inputReference(_text: str,
                       _mode: str,
                       _position: int) -> dict:
        return {"hash": hashlib.sha256(_text.encode()).hexdigest(), "mode": _mode, "position": _position}

    def ownerPid(_self,
                 _thread_id: str) -> int | None:
        """Inspect the kernel's existing lock. Never acquire or change Codex locks."""
        _thread_id = threadUuid(_thread_id)
        try:
            stat = (_self.home_ / "thread-writer-locks" / f"{_thread_id}.lock").stat()
            key = (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)
            lines = Path("/proc/locks").read_text().splitlines()
            for line in lines:
                parts = line.split()
                if len(parts) < 6 or parts[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
                    continue
                major, minor, inode = parts[5].split(":")
                if (int(major, 16), int(minor, 16), int(inode)) != key:
                    continue
                pid = int(parts[4])
                args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if b"app-server" in args and Path(f"/proc/{pid}").stat().st_uid == os.getuid():
                    return pid
        except (OSError, ValueError):
            return None
        return None

    def activeTurn(_self,
                   _thread_id: str) -> str | None:
        thread = _self.thread(_thread_id)
        if thread["history_mode"] == "paginated":
            with closing(readonly(_self.home_ / "thread_history_1.sqlite")) as db:
                row = db.execute("SELECT turn_id,status FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1",
                                 (_thread_id,)).fetchone()
            return row["turn_id"] if row and row["status"] == "inProgress" else None
        if thread["history_mode"] != "legacy":
            raise BridgeError("Unsupported Codex history mode")
        active = None
        with _self.rollout(thread).open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                item = json.loads(line)
                if item.get("type") != "event_msg":
                    continue
                payload = item.get("payload", {})
                if payload.get("type") == "task_started":
                    active = payload.get("turn_id")
                elif payload.get("type") in ("task_complete", "turn_aborted"):
                    active = None
        return active

    def steer(_self,
              _thread_id: str,
              _command_id: str,
              _text: str) -> dict:
        sent = False
        try:
            thread = _self.thread(_thread_id)
            threadUuid(_command_id)
            owner_pid = _self.ownerPid(_thread_id)
            if not owner_pid:
                raise BridgeError("Open the existing Codex conversation in VS Code before using !steer")
            turn = _self.activeTurn(_thread_id)
            if not turn:
                raise BridgeError("No active Codex turn to steer. Send a normal message to start the next task.")
            with CodexIpc(_self.home_, _self.timeout_) as client:
                owner = client.owner(_thread_id)
                if _self.ownerPid(_thread_id) != owner_pid or _self.activeTurn(_thread_id) != turn:
                    raise BridgeError("The active Codex turn changed before steering; no message was sent")
                sent = True
                turn_id = threadUuid(client.steer(owner, _thread_id, _command_id, _text, thread["cwd"]))
            return {"status": "accepted", "turn_id": turn_id, "detail": "Steer accepted by the active Codex turn; awaiting its result."}
        except IpcError as exc:
            error = str(exc)
            if error in ("no-client-found", "request-version-mismatch", "no-handler-for-request") or "no active turn to steer" in error or "is not being streamed" in error:
                return {"status": "rejected", "detail": "Steer unavailable: keep the existing VS Code chat open with a task running. No message was queued."}
            return {"status": "uncertain" if sent else "rejected",
                    "detail": "Codex IPC could not confirm steering. Inspect the VS Code conversation; no automatic retry or queue fallback."}
        except (OSError, BridgeError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            if sent:
                return {"status": "uncertain", "detail": "Steer acknowledgement unavailable; delivery may have occurred. Inspect VS Code before retrying."}
            detail = str(exc) if isinstance(exc, BridgeError) else "Codex steering connection/history is unavailable. Keep the existing VS Code chat open."
            return {"status": "rejected", "detail": detail}

    def queue(_self,
              _thread_id: str,
              _command_id: str,
              _text: str) -> dict:
        _self.thread(_thread_id)
        if not _self.ownerPid(_thread_id):
            return {"status": "rejected",
                    "detail": "Original session is not loaded or its owner cannot be verified. Open that conversation before sending a new reply."}
        threadUuid(_command_id)
        env = {**os.environ, "CODEX_HOME": str(_self.home_)}
        try:
            result = subprocess.run([_self.executable(),
                                     "queue",
                                     "--thread",
                                     threadUuid(_thread_id),
                                     "--message",
                                     _text],
                                    env=env,
                                    capture_output=True,
                                    text=True,
                                    timeout=_self.timeout_,
                                    check=False)
        except subprocess.TimeoutExpired:
            return {"status": "uncertain",
                    "detail": "Codex queue timed out; delivery is unknown. Inspect the original conversation before resolving this command."}
        except OSError:
            return {"status": "rejected", "detail": "Could not launch the Codex executable"}
        if result.returncode:
            #Codex: A failing process may have accepted a message before failing. Never replay it.
            return {"status": "uncertain",
                    "detail": f"Codex queue exited {result.returncode}; inspect the original conversation. No automatic retry."}
        match = re.search(r"Queued message ([0-9a-f-]{36}) for thread ([0-9a-f-]{36})",
                          result.stdout)
        if not match or match[2] != _thread_id:
            return {"status": "uncertain",
                    "detail": "Codex returned an unrecognized acknowledgement; no automatic retry"}
        return {"status": "accepted",
                "queue_id": match[1],
                "detail": "Accepted by Codex; awaiting the actual turn result"}

    def snapshot(_self,
                 _thread_id: str) -> dict:
        thread = _self.thread(_thread_id)
        if thread["history_mode"] == "paginated":
            with closing(readonly(_self.home_ / "thread_history_1.sqlite")) as db:
                db.execute("BEGIN")
                ordinal = db.execute("SELECT COALESCE(MAX(rollout_ordinal),-1) FROM thread_turns "
                                     "WHERE thread_id=? AND status IN ('completed','failed','interrupted') "
                                     "AND rollout_ordinal < COALESCE((SELECT MIN(rollout_ordinal) FROM thread_turns "
                                     "WHERE thread_id=? AND status NOT IN ('completed','failed','interrupted')),9223372036854775807)",
                                     (_thread_id, _thread_id)).fetchone()[0]
            return {"mode": "paginated", "ordinal": ordinal}
        if thread["history_mode"] != "legacy":
            raise BridgeError("Unsupported Codex history mode")
        path = _self.rollout(thread)
        return {"mode": "legacy",
                "offset": _self.completeOffset(path),
                "inode": path.stat().st_ino}

    @staticmethod
    def completeOffset(_path: Path) -> int:
        #Codex: A live writer may be halfway through a JSON line when notify registers.
        with _path.open("rb") as stream:
            position = stream.seek(0, 2)
            while position:
                start = max(0, position - 65536)
                stream.seek(start)
                chunk = stream.read(position - start)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    return start + newline + 1
                position = start
        return 0

    def rollout(_self,
                _thread: dict) -> Path:
        path = Path(_thread["rollout_path"]).resolve()
        if _self.home_ not in path.parents:
            raise BridgeError("Codex rollout path is outside CODEX_HOME")
        return path

    def responses(_self,
                  _thread_id: str,
                  _cursor: dict) -> tuple[list[dict], dict]:
        thread = _self.thread(_thread_id)
        if _cursor.get("mode") != thread["history_mode"]:
            raise BridgeError("Codex history mode changed; re-register this conversation explicitly")
        if _cursor["mode"] == "paginated":
            return _self.paginated(_thread_id, _cursor)
        return _self.legacy(thread, _cursor)

    def paginated(_self,
                  _thread_id: str,
                  _cursor: dict) -> tuple[list[dict], dict]:
        events = []
        _cursor = dict(_cursor)
        with closing(readonly(_self.home_ / "thread_history_1.sqlite")) as db:
            db.execute("BEGIN")
            #Codex: Do not skip an earlier unfinished turn: it may complete after this poll.
            turns = db.execute("SELECT turn_id,status,rollout_ordinal FROM thread_turns WHERE thread_id=? "
                               "AND rollout_ordinal>? ORDER BY rollout_ordinal LIMIT 100",
                               (_thread_id, _cursor["ordinal"])).fetchall()
            for turn in turns:
                finished = turn["status"] in ("completed", "failed", "interrupted")
                texts, requests, progress, inputs = [], set(), [], []
                worked = False
                for row in db.execute("SELECT item_json,rollout_ordinal FROM thread_items WHERE thread_id=? AND turn_id=? ORDER BY rollout_ordinal",
                                      (_thread_id, turn["turn_id"])):
                    item = json.loads(row[0])
                    kind = item.get("type")
                    if kind in ("commandExecution", "fileChange", "mcpToolCall", "webSearch", "imageView", "subAgentActivity", "dynamicToolCall"):
                        if not worked:
                            progress.append((row[1], "Codex is working on this task."))
                        worked = True
                    if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                        texts.append(item.get("text", ""))
                    elif kind == "agentMessage" and item.get("phase") == "commentary" and worked:
                        progress.append((row[1], item.get("text", "")))
                    elif item.get("type") == "userMessage":
                        parts = []
                        for part in item.get("content", []):
                            requests.update(REQUEST_RE.findall(part.get("text", "")))
                            if part.get("type") == "text":
                                parts.append(part.get("text", ""))
                        for text in dict.fromkeys([*parts, "\n".join(parts)]):
                            inputs.append(_self.inputReference(text, "paginated", row[1]))
                if not finished:
                    seen = _cursor.get("progress_ordinal", -1)
                    if progress and progress[-1][0] > seen:
                        ordinal, text = progress[-1]
                        event = _self.progress(_thread_id, turn["turn_id"], ordinal, text, requests)
                        event["inputs"] = inputs
                        events.append(event)
                        _cursor["progress_ordinal"] = ordinal
                    break
                events.append(_self.event(_thread_id,
                                          turn["turn_id"],
                                          turn["status"],
                                          "\n\n".join(texts),
                                          requests,
                                          worked))
                events[-1]["inputs"] = inputs
                _cursor["ordinal"] = turn["rollout_ordinal"]
                _cursor.pop("progress_ordinal", None)
        return events, _cursor

    def legacy(_self,
               _thread: dict,
               _cursor: dict) -> tuple[list[dict], dict]:
        _cursor = dict(_cursor)
        path = _self.rollout(_thread)
        stat = path.stat()
        if stat.st_ino != _cursor.get("inode") or stat.st_size < _cursor["offset"]:
            raise BridgeError("Codex rollout was replaced/truncated; re-register explicitly")
        events = []
        requests = set(_cursor.get("requests", []))
        with path.open("rb") as stream:
            stream.seek(_cursor["offset"])
            for line_index in range(5000):
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break
                item = json.loads(line)
                p = item.get("payload", {})
                if item.get("type") == "event_msg":
                    kind = p.get("type")
                    if kind == "task_started":
                        _cursor["turn_id"] = p.get("turn_id")
                        _cursor["worked"] = False
                        requests = set()
                        _cursor["inputs"] = []
                    elif kind == "user_message":
                        requests.update(REQUEST_RE.findall(p.get("message", "")))
                        _cursor.setdefault("inputs", []).append(_self.inputReference(p.get("message", ""), "legacy", stream.tell()))
                    elif kind == "agent_message" and p.get("phase") == "commentary" and _cursor.get("worked"):
                        if _cursor.get("turn_id"):
                            events.append(_self.progress(_thread["id"], _cursor["turn_id"], stream.tell(), p.get("message", ""), requests))
                            events[-1]["inputs"] = list(_cursor.get("inputs", []))
                    elif kind in ("task_complete", "turn_aborted"):
                        turn_id = p.get("turn_id") or _cursor.get("turn_id")
                        if not turn_id:
                            raise BridgeError("Cannot identify a completed Codex turn")
                        status = "completed" if kind == "task_complete" else "interrupted"
                        events.append(_self.event(_thread["id"],
                                                  turn_id,
                                                  status,
                                                  p.get("last_agent_message") or "",
                                                  requests,
                                                  _cursor.get("worked", False)))
                        events[-1]["inputs"] = _cursor.pop("inputs", [])
                        requests = set()
                        _cursor.pop("turn_id", None)
                        _cursor.pop("worked", None)
                elif item.get("type") == "response_item" and p.get("type") in ("function_call", "custom_tool_call", "web_search_call"):
                    if not _cursor.get("worked") and _cursor.get("turn_id"):
                        events.append(_self.progress(_thread["id"], _cursor["turn_id"], stream.tell(), "Codex is working on this task.", requests))
                        events[-1]["inputs"] = list(_cursor.get("inputs", []))
                    _cursor["worked"] = True
                _cursor["offset"] = stream.tell()
        _cursor["requests"] = sorted(requests)
        completed = {event["turn_id"] for event in events if event["status"] != "in_progress"}
        latest_progress = {event["turn_id"]: event for event in events if event["status"] == "in_progress"}
        #Codex: When catching up, send completion rather than stale progress for finished work.
        events = [event for event in events if event["status"] != "in_progress"
                  or (event["turn_id"] not in completed and event is latest_progress[event["turn_id"]])]
        return events, _cursor

    @staticmethod
    def progress(_thread_id: str,
                 _turn_id: str,
                 _ordinal: int,
                 _text: str,
                 _requests: set) -> dict:
        event = Codex.event(_thread_id, _turn_id, "in_progress", _text[:600], _requests, True)
        event["event_id"] += f":progress:{_ordinal}"
        return event

    @staticmethod
    def event(_thread_id: str,
              _turn_id: str,
              _status: str,
              _text: str,
              _requests: set,
              _worked: bool = False) -> dict:
        return {"event_id": f"{_thread_id}:{_turn_id}",
                "thread_id": _thread_id,
                "turn_id": _turn_id,
                "status": _status,
                "text": _text,
                "worked": _worked,
                "command_ids": sorted(_requests)}
