from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class BridgeError(Exception):
    """A diagnostic that does not contain credentials or private message text."""


def identifier(_value: str) -> str:
    if not isinstance(_value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", _value):
        raise BridgeError("IDs must contain 1–80 letters, digits, underscores or hyphens")
    return _value


def secret(_env_name: str) -> str:
    value = os.environ.get(_env_name, "")
    if not value:
        raise BridgeError(f"Required environment variable is missing: {_env_name}")
    return value


def loopback(_host: str | None) -> bool:
    if _host == "localhost":
        return True
    try:
        return ipaddress.ip_address(_host or "").is_loopback
    except ValueError:
        return False


def routerUrl(_value: str) -> str:
    url = urlsplit(_value)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise BridgeError("router_url must be an http(s) URL")
    if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        raise BridgeError("router_url must be an origin without credentials, path or query")
    if url.scheme != "https" and not loopback(url.hostname):
        raise BridgeError("Remote Router connections require HTTPS; use SSH forwarding for HTTP")
    return _value.rstrip("/")


def stateDir(_value: str) -> Path:
    path = Path(_value).expanduser().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve()
    if path == codex_home or codex_home in path.parents:
        raise BridgeError("Bridge state must be separate from CODEX_HOME")
    return path


@dataclass(frozen=True)
class RouterConfig:
    team_id_: str
    channel_id_: str
    allowed_user_ids_: frozenset[str]
    allowed_channel_ids_: frozenset[str]
    agents_: dict[str, str]
    state_dir_: Path
    host_: str = "127.0.0.1"
    port_: int = 8766
    bot_token_env_: str = "SLACK_BOT_TOKEN"
    app_token_env_: str = "SLACK_APP_TOKEN"
    tls_cert_: str | None = None
    tls_key_: str | None = None


@dataclass(frozen=True)
class AgentConfig:
    agent_id_: str
    router_url_: str
    state_dir_: Path
    codex_home_: Path
    codex_bin_: str = "codex"
    token_env_: str = "CODEX_SLACK_AGENT_TOKEN"
    token_file_: Path | None = None
    poll_interval_: float = 2.0
    queue_timeout_: float = 30.0


def loadConfig(_path: str | Path,
               _kind: str) -> RouterConfig | AgentConfig:
    try:
        data = tomllib.loads(Path(_path).expanduser().read_text())
        section = data[_kind]
        if _kind == "agent":
            config = AgentConfig(agent_id_=identifier(section["agent_id"]),
                                 router_url_=routerUrl(section["router_url"]),
                                 state_dir_=stateDir(section.get("state_dir", "~/.local/state/codex-slack-agent")),
                                 codex_home_=Path(section.get("codex_home", os.environ.get("CODEX_HOME", "~/.codex"))).expanduser().resolve(),
                                 codex_bin_=section.get("codex_bin", "codex"),
                                 token_env_=section.get("token_env", "CODEX_SLACK_AGENT_TOKEN"),
                                 token_file_=Path(section["token_file"]).expanduser() if section.get("token_file") else None,
                                 poll_interval_=float(section.get("poll_interval", 2)),
                                 queue_timeout_=float(section.get("queue_timeout", 30)))
            if config.poll_interval_ < 0.2 or not 1 <= config.queue_timeout_ <= 300:
                raise BridgeError("Invalid polling interval or queue timeout")
            if (config.state_dir_ == config.codex_home_
                or config.codex_home_ in config.state_dir_.parents):
                raise BridgeError("Bridge state must be separate from CODEX_HOME")
            return config
        slack = data["slack"]
        channel = identifier(slack["channel_id"])
        users = frozenset(identifier(v) for v in slack["allowed_user_ids"])
        channels = frozenset(identifier(v) for v in slack.get("allowed_channel_ids", [channel]))
        agents = {identifier(k): v["token_env"] for k, v in data["agents"].items()}
        if not users or not channels or channel not in channels or not agents:
            raise BridgeError("Configure allowed users, channels and at least one Agent")
        cert, key = section.get("tls_cert"), section.get("tls_key")
        host = section.get("host", "127.0.0.1")
        if bool(cert) != bool(key) or (not loopback(host) and not cert):
            raise BridgeError("A non-loopback Router listener requires tls_cert and tls_key")
        port = int(section.get("port", 8766))
        if not 0 <= port <= 65535:
            raise BridgeError("Invalid Router port")
        return RouterConfig(team_id_=identifier(slack["team_id"]),
                            channel_id_=channel,
                            allowed_user_ids_=users,
                            allowed_channel_ids_=channels,
                            agents_=agents,
                            state_dir_=stateDir(section.get("state_dir", "~/.local/state/codex-slack-router")),
                            host_=host,
                            port_=port,
                            tls_cert_=cert,
                            tls_key_=key,
                            bot_token_env_=slack.get("bot_token_env", "SLACK_BOT_TOKEN"),
                            app_token_env_=slack.get("app_token_env", "SLACK_APP_TOKEN"))
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise BridgeError(f"Invalid {_kind} configuration ({type(exc).__name__})") from None
