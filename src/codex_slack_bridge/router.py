from __future__ import annotations

import hmac
import json
import logging
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .codex import threadUuid
from .config import BridgeError, RouterConfig, secret
from .store import Store

LOG = logging.getLogger(__name__)
TIMESTAMP = re.compile(r"\d+\.\d+")


def textField(_data: dict,
              _name: str,
              _maximum: int = 32000,
              _empty: bool = False) -> str:
    value = _data.get(_name)
    if not isinstance(value, str) or (not _empty and not value.strip()) or len(value) > _maximum:
        raise BridgeError(f"Invalid {_name}")
    return value


def historyCursor(_cursor) -> dict:
    if not isinstance(_cursor, dict) or _cursor.get("mode") not in ("legacy", "paginated"):
        raise BridgeError("A supported history cursor is required")
    fields = ("offset", "inode") if _cursor["mode"] == "legacy" else ("ordinal",)
    if any(type(_cursor.get(k)) is not int or _cursor[k] < (-1 if k == "ordinal" else 0) for k in fields):
        raise BridgeError("Invalid history cursor")
    return {k: _cursor[k] for k in ("mode", *fields)}


class Router:
    def __init__(_self,
                 _config: RouterConfig,
                 _store: Store):
        _self.config_, _self.store_ = _config, _store

    def slackEvent(_self,
                   _payload: dict) -> str | None:
        if _payload.get("type") != "event_callback" or _payload.get("team_id") != _self.config_.team_id_:
            return None
        event = _payload.get("event", {})
        if not isinstance(event, dict) or event.get("type") != "message":
            return None
        if event.get("subtype") or event.get("bot_id") or event.get("app_id"):
            return None
        if (event.get("user") not in _self.config_.allowed_user_ids_
            or event.get("channel") not in _self.config_.allowed_channel_ids_):
            return None
        root = event.get("thread_ts")
        if not isinstance(root, str) or not TIMESTAMP.fullmatch(root) or root == event.get("ts"):
            return None
        if not isinstance(event.get("text"), str) or not 0 < len(event["text"].strip()) <= 32000:
            return None
        event_id = _payload.get("event_id")
        if not isinstance(event_id, str) or not 0 < len(event_id) <= 128:
            return None
        return _self.store_.ingest(event_id, event["channel"], root, event["user"], event["text"])

    def api(_self,
            _agent_id: str,
            _operation: str,
            _payload: dict) -> dict:
        if _operation == "bindings":
            return {"bindings": _self.store_.bindings(_agent_id)}
        if _operation == "claim":
            return {"command": _self.store_.claim(_agent_id)}
        if _operation == "notify":
            channel = _payload.get("channel_id") or _self.config_.channel_id_
            if channel not in _self.config_.allowed_channel_ids_:
                raise BridgeError("Channel is not allowed")
            _payload = dict(_payload)
            _payload["request_id"] = threadUuid(_payload.get("request_id"))
            _payload["thread_id"] = threadUuid(_payload.get("thread_id"))
            _payload["title"] = textField(_payload, "title", 300)
            _payload["text"] = textField(_payload, "text")
            _payload["cursor"] = historyCursor(_payload.get("cursor"))
            return {"binding": _self.store_.notify(_agent_id, _payload, channel)}
        if _operation == "result":
            command_id = threadUuid(_payload.get("command_id"))
            status = _payload.get("status")
            if status not in ("accepted", "rejected", "uncertain"):
                raise BridgeError("Invalid dispatch status")
            result = {"status": status, "detail": textField(_payload, "detail", 1000)}
            if _payload.get("queue_id"):
                result["queue_id"] = threadUuid(_payload["queue_id"])
            _self.store_.result(_agent_id, command_id, result)
            return {"ok": True}
        if _operation == "response":
            thread_id = threadUuid(_payload.get("thread_id"))
            turn_id = threadUuid(_payload.get("turn_id"))
            identity = f"{thread_id}:{turn_id}"
            progress = _payload.get("status") == "in_progress"
            event_id = _payload.get("event_id", "")
            if not isinstance(event_id, str) or not (re.fullmatch(re.escape(identity) + r":progress:\d{1,20}", event_id) if progress else event_id == identity):
                raise BridgeError("Invalid response event identity")
            if _payload.get("status") not in ("completed", "failed", "interrupted", "in_progress"):
                raise BridgeError("Invalid turn status")
            if type(_payload.get("worked", False)) is not bool:
                raise BridgeError("Invalid task activity flag")
            textField(_payload, "text", 500000, _empty=True)
            commands = _payload.get("command_ids", [])
            if not isinstance(commands, list) or len(commands) > 100:
                raise BridgeError("Invalid command references")
            _payload = {**_payload, "command_ids": [threadUuid(x) for x in commands]}
            _self.store_.response(_agent_id, _payload, _self.config_.automatic_notifications_)
            return {"ok": True}
        if _operation == "unbind":
            _self.store_.disable(_agent_id, threadUuid(_payload.get("thread_id")))
            return {"ok": True}
        if _operation == "rebind":
            _self.store_.rebind(_agent_id,
                                threadUuid(_payload.get("thread_id")),
                                historyCursor(_payload.get("cursor")))
            return {"ok": True}
        raise BridgeError("Unknown Router operation")


def makeServer(_router: Router,
               _tokens: dict[str, str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(_self,
                        _format,
                        *_args):
            #Codex: Do not put user messages, Authorization headers or query strings in logs.
            pass

        def reply(_self,
                  _status: int,
                  _body: dict):
            data = json.dumps(_body).encode()
            _self.send_response(_status)
            _self.send_header("Content-Type", "application/json")
            _self.send_header("Content-Length", str(len(data)))
            _self.end_headers()
            _self.wfile.write(data)

        def do_POST(_self):
            parts = urlsplit(_self.path)
            route = parts.path.strip("/").split("/")
            if len(route) != 4 or route[:2] != ["v1", "agents"] or parts.query:
                _self.reply(404, {"error": "Unknown endpoint"})
                return
            agent_id, operation = route[2:]
            token = _tokens.get(agent_id)
            authorization = _self.headers.get("Authorization", "")
            if not token or not hmac.compare_digest(authorization.encode(),
                                                    f"Bearer {token}".encode()):
                _self.reply(401, {"error": "Agent authentication failed"})
                return
            try:
                size = int(_self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024 * 1024 or _self.headers.get("Transfer-Encoding"):
                    _self.reply(413, {"error": "Invalid request size"})
                    return
                raw = _self.rfile.read(size)
                if len(raw) != size:
                    raise BridgeError("Incomplete request body")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise BridgeError("JSON object required")
                _self.reply(200, _router.api(agent_id, operation, payload))
            except (BridgeError, ValueError, TypeError):
                _self.reply(400, {"error": "Invalid request or incompatible binding state"})
            except Exception as exc:
                LOG.error("Router request failed (%s)", type(exc).__name__)
                _self.reply(500, {"error": "Router could not commit the request"})

    class Server(ThreadingHTTPServer):
        def get_request(_self):
            sock, address = super().get_request()
            sock.settimeout(10)
            if isinstance(sock, ssl.SSLSocket):
                sock.do_handshake()
            return sock, address

    server = Server((_router.config_.host_, _router.config_.port_), Handler)
    if _router.config_.tls_cert_:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(_router.config_.tls_cert_, _router.config_.tls_key_)
        server.socket = context.wrap_socket(server.socket,
                                            server_side=True,
                                            do_handshake_on_connect=False)
    return server


class SlackGateway:
    def __init__(_self,
                 _token: str):
        try:
            from slack_sdk import WebClient
        except ImportError:
            raise BridgeError("Install the Router extra: pip install 'codex-slack-bridge[router]'") from None
        #Codex: Retrying a POST after a connection error can duplicate messages.
        _self.client_ = WebClient(token=_token, retry_handlers=[])

    def verify(_self,
               _team_id: str):
        try:
            response = _self.client_.auth_test()
        except Exception as exc:
            raise BridgeError(f"Slack authentication failed ({type(exc).__name__})") from None
        if response.get("team_id") != _team_id:
            raise BridgeError("Slack token belongs to a different workspace")

    def post(_self,
             _output: dict) -> dict:
        from slack_sdk.errors import SlackApiError

        try:
            response = _self.client_.chat_postMessage(channel=_output["channel_id"],
                                                      text=_output["text"],
                                                      thread_ts=_output["root_ts"] if _output["kind"] != "root" else None,
                                                      mrkdwn=False,
                                                      parse="none",
                                                      unfurl_links=False,
                                                      unfurl_media=False)
            ts = response.get("ts")
            if (not isinstance(ts, str)
                or not TIMESTAMP.fullmatch(ts)
                or response.get("channel") != _output["channel_id"]):
                return {"status": "uncertain", "detail": "Unexpected Slack acknowledgement"}
            return {"status": "sent", "ts": ts}
        except SlackApiError as exc:
            error = exc.response.get("error", "unknown_error")
            if error == "ratelimited":
                try:
                    delay = max(1, int(exc.response.headers.get("Retry-After", "30")))
                except (ValueError, TypeError):
                    delay = 30
                return {"status": "pending", "detail": "Slack rate limit", "delay": delay}
            if error in {"invalid_auth",
                         "not_authed",
                         "token_revoked",
                         "account_inactive",
                         "channel_not_found",
                         "not_in_channel",
                         "missing_scope",
                         "is_archived",
                         "msg_too_long"}:
                return {"status": "failed", "detail": f"Slack rejected the message: {error}"}
            return {"status": "uncertain",
                    "detail": "Slack returned an error with uncertain delivery; inspect Slack before retrying"}
        except Exception:
            return {"status": "uncertain",
                    "detail": "Slack acknowledgement unavailable; inspect Slack before retrying"}

    def delete(_self,
               _message: dict) -> dict:
        from slack_sdk.errors import SlackApiError

        ts = _message.get("slack_ts")
        if not isinstance(ts, str) or not TIMESTAMP.fullmatch(ts) or not _message.get("root_ts") or ts == _message["root_ts"]:
            return {"status": "failed", "detail": "Cleanup requires a recorded thread reply"}
        try:
            response = _self.client_.chat_delete(channel=_message["channel_id"], ts=ts)
            if response.get("ok") is True and response.get("channel") == _message["channel_id"] and response.get("ts") == ts:
                return {"status": "deleted"}
            return {"status": "pending", "detail": "Unexpected Slack deletion acknowledgement", "delay": 60}
        except SlackApiError as exc:
            error = exc.response.get("error", "unknown_error")
            if error == "message_not_found":
                return {"status": "deleted"}
            if error == "ratelimited":
                try:
                    delay = max(1, int(exc.response.headers.get("Retry-After", "60")))
                except (ValueError, TypeError):
                    delay = 60
                return {"status": "pending", "detail": "Slack deletion rate limit", "delay": delay}
            if error in {"cant_delete_message", "channel_not_found", "invalid_auth", "not_authed",
                         "token_revoked", "token_expired", "account_inactive", "missing_scope", "no_permission",
                         "not_allowed_token_type", "is_archived", "ekm_access_denied", "access_denied"}:
                return {"status": "failed", "detail": f"Slack rejected deletion: {error}"}
            return {"status": "pending", "detail": "Slack deletion unavailable", "delay": 60}
        except Exception:
            return {"status": "pending", "detail": "Slack deletion acknowledgement unavailable", "delay": 60}


def deliverOnce(_store: Store,
                _gateway) -> bool:
    output = _store.nextOutput()
    if not output:
        return False
    result = _gateway.post(output)
    _store.outputResult(output["id"],
                        result["status"],
                        _ts=result.get("ts"),
                        _detail=result.get("detail", ""),
                        _delay=result.get("delay", 0))
    return True


def cleanupOnce(_store: Store,
                _gateway) -> bool:
    message = _store.nextCleanup()
    if not message:
        return False
    result = _gateway.delete(message)
    _store.cleanupResult(message["channel_id"],
                         message["slack_ts"],
                         result["status"],
                         result.get("detail", ""),
                         result.get("delay", 0))
    if result["status"] == "failed":
        LOG.warning("Slack reply cleanup failed: %s", result["detail"])
    return True


def runRouter(_config: RouterConfig,
              _store: Store,
              _stop: threading.Event):
    tokens = {agent: secret(env) for agent, env in _config.agents_.items()}
    if any(len(token) < 32 for token in tokens.values()) or len(set(tokens.values())) != len(tokens):
        raise BridgeError("Each Agent needs a distinct random token of at least 32 characters")
    gateway = SlackGateway(secret(_config.bot_token_env_))
    gateway.verify(_config.team_id_)
    app_token = secret(_config.app_token_env_)
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    router = Router(_config, _store)
    server = makeServer(router, tokens)
    _store.recoverRouter()
    socket_client = SocketModeClient(app_token=app_token, web_client=gateway.client_)

    def onRequest(_client,
                  _request):
        try:
            if _request.type == "events_api":
                router.slackEvent(_request.payload)
            #Codex: Acknowledge only after a matching command has been durably committed.
            _client.send_socket_mode_response(SocketModeResponse(envelope_id=_request.envelope_id))
        except Exception as exc:
            LOG.error("Slack event could not be acknowledged (%s)", type(exc).__name__)

    socket_client.socket_mode_request_listeners.append(onRequest)
    http_thread = threading.Thread(target=server.serve_forever, name="router-http", daemon=True)
    http_thread.start()
    try:
        socket_client.connect()
        LOG.info("Router running on %s:%s", _config.host_, server.server_port)
        while not _stop.is_set():
            try:
                _store.expireClaims()
                deliverOnce(_store, gateway)
            except Exception as exc:
                LOG.error("Outbox cycle failed (%s); inspect local status", type(exc).__name__)
            try:
                cleanupOnce(_store, gateway)
            except Exception as exc:
                LOG.error("Reply cleanup failed (%s); delivery continues", type(exc).__name__)
            _stop.wait(1.1)
    finally:
        socket_client.close()
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=5)
