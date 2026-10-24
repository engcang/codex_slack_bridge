from __future__ import annotations

import logging
import sqlite3
import threading

from .codex import Codex
from .config import BridgeError
from .store import Store
from .transport import RouterClient

LOG = logging.getLogger(__name__)


class Agent:
    def __init__(_self,
                 _client: RouterClient,
                 _codex: Codex,
                 _state: Store):
        _self.client_, _self.codex_, _self.state_ = _client, _codex, _state

    def tick(_self):
        #Codex: Recover acknowledgements before claiming anything else. A crash never replays queue.
        for command_id, result in _self.state_.pendingResults():
            _self.client_.call("result", {"command_id": command_id, **result})
            _self.state_.resultReported(command_id)
        bindings = _self.client_.call("bindings")["bindings"]
        valid = set()
        for binding in bindings:
            try:
                cursor = _self.state_.cursor(binding)
                events, new_cursor = _self.codex_.responses(binding["thread_id"], cursor)
                for event in events:
                    _self.client_.call("response", event)
                #Codex: Only checkpoint after every event is durably acknowledged by the Router.
                _self.state_.saveCursor(binding, new_cursor)
                valid.add(binding["id"])
            except (BridgeError, sqlite3.Error, OSError, ValueError) as exc:
                LOG.warning("Cannot observe binding %s (%s)", binding["id"], type(exc).__name__)
        command = _self.client_.call("claim").get("command")
        if not command:
            return
        command_id = command["id"]
        if not _self.state_.beginAttempt(command_id):
            #Codex: Already attempted locally: never call Codex twice for the same command.
            return
        if command["binding_id"] not in valid:
            result = {"status": "rejected",
                      "detail": "History observation is unavailable; command was not executed. Inspect Agent compatibility before retrying."}
        else:
            try:
                result = _self.codex_.queue(command["thread_id"], command_id, command["text"])
            except (BridgeError, sqlite3.Error) as exc:
                result = {"status": "rejected",
                          "detail": f"Codex preflight failed ({type(exc).__name__}); command was not executed"}
        _self.state_.finishAttempt(command_id, result)
        _self.client_.call("result", {"command_id": command_id, **result})
        _self.state_.resultReported(command_id)

    def run(_self,
            _stop: threading.Event,
            _interval: float):
        while not _stop.is_set():
            try:
                _self.tick()
            except (BridgeError, sqlite3.Error, OSError, ValueError) as exc:
                LOG.warning("Agent cycle could not finish (%s); durable state retained",
                            type(exc).__name__)
            _stop.wait(_interval)
