from __future__ import annotations

import json
import os
import socket
import stat
import struct
import time
from pathlib import Path
from uuid import uuid4

from .config import BridgeError


class IpcError(BridgeError):
    pass


class CodexIpc:
    """Connect to the existing extension router; never start a router or session."""

    def __init__(_self,
                 _home: Path,
                 _timeout: float):
        _self.path_ = _home / "ipc" / "ipc.sock"
        _self.timeout_ = _timeout
        _self.socket_ = None
        _self.client_ = "initializing-client"

    def __enter__(_self):
        for path in (_self.path_.parent, _self.path_):
            info = path.lstat()
            expected = stat.S_ISSOCK(info.st_mode) if path == _self.path_ else stat.S_ISDIR(info.st_mode)
            if not expected or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BridgeError("Codex IPC requires a private socket owned by the current user")
        _self.socket_ = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            _self.socket_.settimeout(_self.timeout_)
            _self.socket_.connect(str(_self.path_))
            peer = _self.socket_.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack("3i", peer)[1] != os.getuid():
                raise BridgeError("Codex IPC peer belongs to another user")
            response = _self.request("initialize", {"clientType": "codex-slack-bridge"}, 0)
            _self.client_ = response["result"]["clientId"]
            return _self
        except BaseException:
            _self.socket_.close()
            raise

    def __exit__(_self,
                 _exc_type,
                 _exc_value,
                 _traceback):
        _self.socket_.close()

    def send(_self,
             _message: dict):
        data = json.dumps(_message).encode()
        _self.socket_.sendall(struct.pack("<I", len(data)) + data)

    def read(_self,
             _size: int,
             _deadline: float) -> bytes:
        chunks = []
        while _size:
            remaining = _deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex IPC timed out")
            _self.socket_.settimeout(remaining)
            chunk = _self.socket_.recv(_size)
            if not chunk:
                raise OSError("Codex IPC closed")
            chunks.append(chunk)
            _size -= len(chunk)
        return b"".join(chunks)

    def request(_self,
                _method: str,
                _params: dict,
                _version: int = 1,
                _target: str | None = None) -> dict:
        identity = str(uuid4())
        deadline = time.monotonic() + _self.timeout_
        _self.socket_.settimeout(_self.timeout_)
        _self.send({"type": "request",
                    "requestId": identity,
                    "sourceClientId": _self.client_,
                    "method": _method,
                    "version": _version,
                    "params": _params,
                    "targetClientId": _target,
                    "timeoutMs": int(_self.timeout_ * 1000)})
        while True:
            size = struct.unpack("<I", _self.read(4, deadline))[0]
            if not 0 < size <= 16 * 1024 * 1024:
                raise BridgeError("Invalid Codex IPC frame size")
            message = json.loads(_self.read(size, deadline))
            if not isinstance(message, dict):
                raise BridgeError("Invalid Codex IPC response")
            if message.get("type") == "client-discovery-request":
                _self.send({"type": "client-discovery-response",
                            "requestId": message["requestId"],
                            "response": {"canHandle": False}})
            if message.get("type") != "response" or message.get("requestId") != identity:
                continue
            if message.get("resultType") != "success":
                raise IpcError(str(message.get("error", "Unknown IPC error")))
            if message.get("method") != _method or (_target and message.get("handledByClientId") != _target):
                raise BridgeError("Codex IPC response came from an unexpected owner")
            return message

    def owner(_self,
              _thread_id: str) -> str:
        response = _self.request("thread-owner-discovery", {"hostId": "local", "conversationId": _thread_id})
        owner = response.get("handledByClientId")
        if not isinstance(owner, str) or not owner:
            raise BridgeError("The existing Codex window could not be identified")
        return owner

    def steer(_self,
              _owner: str,
              _thread_id: str,
              _command_id: str,
              _text: str,
              _cwd: str) -> str:
        #Codex: This versioned extension IPC handler asserts existing ownership and calls turn/steer.
        restore = {"id": _command_id,
                   "text": _text,
                   "cwd": _cwd,
                   "createdAt": int(time.time() * 1000),
                   "context": {"prompt": _text,
                               "addedFiles": [],
                               "fileAttachments": [],
                               "ideContext": None,
                               "imageAttachments": [],
                               "workspaceRoots": [_cwd]}}
        response = _self.request("thread-follower-steer-turn",
                                 {"conversationId": _thread_id,
                                  "input": [{"type": "text", "text": _text, "text_elements": []}],
                                  "restoreMessage": restore,
                                  "clientUserMessageId": _command_id,
                                  "attachments": []},
                                 _target=_owner)
        return response["result"]["result"]["turnId"]
