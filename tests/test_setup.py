import json
import os
import stat
from pathlib import Path

import pytest
from conftest import A

from codex_slack_bridge.cli import main
from codex_slack_bridge.codex import Codex
from codex_slack_bridge.config import BridgeError, loadConfig
from codex_slack_bridge.setup import (
    MARKER,
    installServices,
    pairing,
    privateWrite,
    serviceText,
    setup,
    writeConfiguration,
)


def agentConfig(_home,
                _codex_home):
    directory = _home / ".config/codex-slack-bridge"
    privateWrite(directory / "agent.token", "x" * 40)
    privateWrite(directory / "agent.toml", f'[agent]\nagent_id="pc-a"\nrouter_url="http://127.0.0.1:8766"\ncodex_home={json.dumps(str(_codex_home))}\ntoken_file={json.dumps(str(directory / "agent.token"))}\n')
    return directory


def test_private_files_and_services_keep_secrets_out_of_units(tmp_path,
                                                              monkeypatch):
    directory = agentConfig(tmp_path, tmp_path / "codex")
    privateWrite(directory / "router.toml", "placeholder")
    privateWrite(directory / "router.env", "SLACK_BOT_TOKEN=private-token\n")
    calls = []

    def run(_args):
        calls.append(_args)
        return "yes" if _args[0] == "loginctl" else ""

    monkeypatch.setattr("codex_slack_bridge.setup.runCommand", run)
    installServices(directory, tmp_path)
    unit = (tmp_path / ".config/systemd/user/codex-slack-router.service").read_text()
    assert "EnvironmentFile=" in unit
    assert "private-token" not in unit
    assert "Restart=always" in unit
    assert stat.S_IMODE((directory / "router.env").stat().st_mode) == 0o600
    assert any(args[:3] == ["systemctl", "--user", "enable"] for args in calls)
    assert any(args[:3] == ["systemctl", "--user", "restart"] for args in calls)
    installServices(directory, tmp_path)
    assert (directory / "router.env").read_text() == "SLACK_BOT_TOKEN=private-token\n"


def test_existing_service_is_never_overwritten(tmp_path):
    directory = agentConfig(tmp_path, tmp_path / "codex")
    path = tmp_path / ".config/systemd/user/codex-slack-agent.service"
    privateWrite(path, "existing service")
    with pytest.raises(BridgeError, match="preserved"):
        installServices(directory, tmp_path)
    assert path.read_text() == "existing service"


def test_service_escaping_and_ssh_option_injection_rejected(tmp_path):
    unit = serviceText(['/home/path with spaces/$name%value/"codex-slack"', "agent"])
    assert '$$name%%value/\\"codex-slack\\"' in unit
    with pytest.raises(BridgeError):
        serviceText(["/bin/tool\nExecStart=/bin/bad"])
    path = tmp_path / "join.json"
    privateWrite(path,
                 json.dumps({"version": 1,
                             "agent_id": "pc-b",
                             "token": "a" * 40,
                             "ssh_destination": "-oProxyCommand=bad",
                             "router_port": 8766}))
    with pytest.raises(BridgeError, match="pairing"):
        pairing(path)


def test_setup_reuses_configuration_without_prompting_or_resetting_state(tmp_path,
                                                                         monkeypatch):
    directory = agentConfig(tmp_path, tmp_path / "codex")
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr("codex_slack_bridge.setup.runCommand", lambda _args: "")
    monkeypatch.setattr("codex_slack_bridge.setup.installServices", lambda _directory, _home: None)
    monkeypatch.setattr(Codex, "check", lambda _self: "test-version")
    monkeypatch.setattr("codex_slack_bridge.setup.RouterClient.call", lambda _self, _operation: {"bindings": []})
    setup()
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(BridgeError, match="already configured"):
        setup("new-peer.json")


def test_add_repeated_selection_is_idempotent_and_does_not_queue(tmp_path,
                                                                 home,
                                                                 monkeypatch,
                                                                 router):
    directory = agentConfig(tmp_path, home)
    monkeypatch.setattr(Codex, "ownerPid", lambda _self, _thread: 123)

    def forbidden(_self,
                  *_args):
        raise AssertionError("Registration must never send a Codex command")

    monkeypatch.setattr(Codex, "queue", forbidden)
    monkeypatch.setattr("codex_slack_bridge.cli.RouterClient.call", lambda _self, _operation, _payload=None: router.api("pc-a", _operation, _payload or {}))
    args = ["add", "--config", str(directory / "agent.toml"), "--thread", A]
    assert main(args) == 0
    original = router.api("pc-a", "bindings", {})["bindings"]
    assert main(args) == 0
    assert router.api("pc-a", "bindings", {})["bindings"] == original
    assert len(router.store_.status()["outbox"]) == 1
    assert loadConfig(directory / "agent.toml", "agent").agent_id_ == "pc-a"


def test_sessions_list_only_open_vscode_conversations(home,
                                                      monkeypatch):
    codex = Codex(home)
    monkeypatch.setattr(codex, "ownerPid", lambda _thread: None)
    assert codex.sessions() == []
    monkeypatch.setattr(codex, "ownerPid", lambda _thread: 123)
    assert [item["id"] for item in codex.sessions()] == [A]


def test_remote_install_creates_persistent_ssh_tunnel(tmp_path,
                                                      monkeypatch):
    directory = agentConfig(tmp_path, tmp_path / "codex")
    privateWrite(directory / "peer.json",
                 json.dumps({"version": 1,
                             "agent_id": "pc-b",
                             "token": "a" * 40,
                             "ssh_destination": "user@router",
                             "router_port": 8766}))
    monkeypatch.setattr("codex_slack_bridge.setup.runCommand", lambda _args: "yes")
    installServices(directory, tmp_path)
    unit = (tmp_path / ".config/systemd/user/codex-slack-tunnel.service").read_text()
    assert unit.startswith(MARKER)
    assert "127.0.0.1:8767:127.0.0.1:8766" in unit
    assert "StrictHostKeyChecking=yes" in unit
    assert "a" * 40 not in unit


def test_first_setup_creates_matching_router_and_agent_without_exposing_tokens(tmp_path,
                                                                               monkeypatch,
                                                                               capsys):
    from slack_sdk import WebClient

    directory = tmp_path / ".config/codex-slack-bridge"
    answers = iter(["desktop", "C123", "U123"])
    tokens = iter(["xoxb-test-secret", "xapp-test-secret"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(tokens))
    monkeypatch.setattr("codex_slack_bridge.setup.detectCodex", lambda _home: "/usr/bin/test-codex")
    monkeypatch.setattr(Codex, "check", lambda _self: "test-version")
    monkeypatch.setattr(WebClient, "auth_test", lambda _self: {"team_id": "T123"})
    monkeypatch.setattr(WebClient, "apps_connections_open", lambda _self: {"ok": True})
    writeConfiguration(directory, tmp_path, None)
    agent = loadConfig(directory / "agent.toml", "agent")
    router = loadConfig(directory / "router.toml", "router")
    env = dict(line.split("=", 1) for line in (directory / "router.env").read_text().splitlines())
    assert router.team_id_ == "T123"
    assert router.allowed_user_ids_ == frozenset({"U123"})
    assert env[router.agents_[agent.agent_id_]] == agent.token_file_.read_text().strip()
    assert len(env["CODEX_SLACK_AGENT_TOKEN"]) >= 32
    assert "test-secret" not in capsys.readouterr().out
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir())


def test_join_configuration_checks_ssh_and_never_requests_slack_tokens(tmp_path,
                                                                       monkeypatch):
    directory = tmp_path / ".config/codex-slack-bridge"
    path = tmp_path / "join.json"
    peer = {"version": 1, "agent_id": "laptop", "token": "a" * 40, "ssh_destination": "user@router", "router_port": 8766}
    privateWrite(path, json.dumps(peer))
    calls = []
    monkeypatch.setattr("codex_slack_bridge.setup.runCommand", lambda _args: calls.append(_args))
    monkeypatch.setattr("codex_slack_bridge.setup.detectCodex", lambda _home: "/usr/bin/test-codex")
    monkeypatch.setattr(Codex, "check", lambda _self: "test-version")
    writeConfiguration(directory, tmp_path, path)
    config = loadConfig(directory / "agent.toml", "agent")
    assert config.agent_id_ == "laptop"
    assert config.router_url_ == "http://127.0.0.1:8767"
    assert config.token_file_.read_text().strip() == peer["token"]
    assert not (directory / "router.env").exists()
    assert calls[0][0] == "ssh"
    assert "-oStrictHostKeyChecking=yes" in calls[0]


def test_linger_failure_does_not_claim_boot_startup(tmp_path,
                                                    monkeypatch):
    from types import SimpleNamespace

    directory = agentConfig(tmp_path, tmp_path / "codex")
    monkeypatch.setattr("codex_slack_bridge.setup.runCommand", lambda _args: "no")
    monkeypatch.setattr("codex_slack_bridge.setup.subprocess.run", lambda _args, **_kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(BridgeError, match="Services start at login"):
        installServices(directory, tmp_path)
