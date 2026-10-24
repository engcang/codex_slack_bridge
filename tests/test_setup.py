import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import A

from codex_slack_bridge.cli import browseSessions, main
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


def test_existing_notifier_launcher_is_preserved_before_installing_services(tmp_path):
    directory = agentConfig(tmp_path, tmp_path / "codex")
    path = tmp_path / ".local/bin/codex-slack-notify"
    privateWrite(path, "existing notifier")
    with pytest.raises(BridgeError, match="codex-slack-notify was preserved"):
        installServices(directory, tmp_path)
    assert path.read_text() == "existing notifier"
    assert not list((tmp_path / ".config/systemd/user").glob("codex-slack-*.service"))


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


def test_systemd_accepts_environment_file_path(tmp_path):
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze is required to validate service syntax")
    environment = tmp_path / 'path with spaces $value%name"quote' / "router.env"
    privateWrite(environment, "BRIDGE_TEST_VALUE=synthetic\n")
    unit = tmp_path / "codex-slack-test.service"
    unit.write_text(serviceText(["/bin/true"], environment))
    result = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not any(line.startswith(f"{unit}:") for line in result.stderr.splitlines()), result.stderr
    with pytest.raises(BridgeError, match="Environment file paths"):
        serviceText(["/bin/true"], Path("relative.env"))
    with pytest.raises(BridgeError, match="Environment file paths"):
        serviceText(["/bin/true"], tmp_path / "line\nbreak.env")


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


def test_setup_timeout_reports_last_failure_and_preserves_configuration(tmp_path,
                                                                        monkeypatch,
                                                                        capsys):
    directory = agentConfig(tmp_path, tmp_path / "codex")
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr("codex_slack_bridge.setup.runCommand", lambda _args: "")
    monkeypatch.setattr("codex_slack_bridge.setup.installServices", lambda _directory, _home: None)
    monkeypatch.setattr(Codex, "check", lambda _self: "test-version")
    monkeypatch.setattr("codex_slack_bridge.setup.time.sleep", lambda _seconds: None)

    def unavailable(_self,
                    _operation):
        raise BridgeError("Router rejected bindings (HTTP 401)")

    monkeypatch.setattr("codex_slack_bridge.setup.RouterClient.call", unavailable)
    with pytest.raises(BridgeError) as error:
        setup()
    assert "Setup incomplete" in str(error.value)
    assert "Router rejected bindings (HTTP 401)" in str(error.value)
    assert "journalctl --user" in str(error.value)
    assert "Installed. Services start at boot." not in capsys.readouterr().out
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}


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


def test_sessions_include_closed_vscode_conversations(home,
                                                      monkeypatch):
    codex = Codex(home)
    monkeypatch.setattr(codex, "ownerPid", lambda _thread: None)
    assert codex.sessions()[0]["is_open"] is False
    monkeypatch.setattr(codex, "ownerPid", lambda _thread: 123)
    assert [item["id"] for item in codex.sessions()] == [A]
    assert codex.sessions()[0]["is_open"] is True


def test_picker_loads_more_and_selects_beyond_first_page(monkeypatch,
                                                         capsys):
    from types import SimpleNamespace

    sessions = [{"id": f"chat-{index}", "name": f"Chat {index}", "title": "", "cwd": "/project", "is_open": True} for index in range(12)]
    codex = SimpleNamespace(sessions=lambda: sessions, ownerPid=lambda _thread: 42)
    answers = iter(["m", "m", "12"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert browseSessions(codex, True) == "chat-11"
    output = capsys.readouterr().out
    assert "1. [open] Chat 0" in output
    assert "6. [open] Chat 5" in output
    assert "12. [open] Chat 11" in output


def test_picker_rechecks_closed_conversation_before_selecting(monkeypatch,
                                                              capsys):
    from types import SimpleNamespace

    owners = iter([None, 42])
    codex = SimpleNamespace(sessions=lambda: [{"id": A, "name": "Closed", "title": "", "cwd": "/project", "is_open": False}],
                            ownerPid=lambda _thread: next(owners))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    assert browseSessions(codex, True) == A
    assert "Open this conversation in VS Code" in capsys.readouterr().out


def test_picker_all_and_cancel_do_not_register(monkeypatch,
                                               capsys):
    from types import SimpleNamespace

    sessions = [{"id": str(index), "name": f"Chat {index}", "title": "", "cwd": "/project", "is_open": False} for index in range(8)]
    codex = SimpleNamespace(sessions=lambda: sessions)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    assert browseSessions(codex, True, True) is None
    assert "8. [closed]" in capsys.readouterr().out


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


def test_first_setup_creates_matching_router_and_agent_without_logging_tokens(tmp_path,
                                                                              monkeypatch,
                                                                              capsys):
    from slack_sdk import WebClient

    directory = tmp_path / ".config/codex-slack-bridge"
    answers = iter(["desktop", "xoxb-test-secret", "xapp-test-secret", "C123", "U123"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("codex_slack_bridge.setup.detectCodex", lambda _home: "/usr/bin/test-codex")
    monkeypatch.setattr(Codex, "check", lambda _self: "test-version")
    calls = []

    def apiCall(_self,
                _api_method,
                **_kwargs):
        calls.append((_api_method, _self.token, _kwargs.get("params")))
        return {"ok": True, "team_id": "T123"}

    monkeypatch.setattr(WebClient, "api_call", apiCall)
    writeConfiguration(directory, tmp_path, None)
    agent = loadConfig(directory / "agent.toml", "agent")
    router = loadConfig(directory / "router.toml", "router")
    env = dict(line.split("=", 1) for line in (directory / "router.env").read_text().splitlines())
    assert router.team_id_ == "T123"
    assert router.allowed_user_ids_ == frozenset({"U123"})
    assert env[router.agents_[agent.agent_id_]] == agent.token_file_.read_text().strip()
    assert len(env["CODEX_SLACK_AGENT_TOKEN"]) >= 32
    assert env["SLACK_BOT_TOKEN"] == "xoxb-test-secret"
    assert env["SLACK_APP_TOKEN"] == "xapp-test-secret"
    assert calls[0] == ("auth.test", "xoxb-test-secret", {})
    assert calls[1][0] == "apps.connections.open"
    assert calls[1][2] == {"token": "xapp-test-secret"}
    assert "test-secret" not in capsys.readouterr().out
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir())


@pytest.mark.parametrize("method, stage",
                         [("auth.test", "Bot token (auth.test)"),
                          ("apps.connections.open", "App token (apps.connections.open)")])
@pytest.mark.parametrize("failure, expected",
                         [("api", "Slack returned missing_scope"),
                          ("network", "network connection failed (TimeoutError)"),
                          ("sdk", "installer verification error (TypeError)"),
                          ("unexpected_api_error", "Slack returned unknown_error")])
def test_setup_reports_verification_stage_without_leaking_tokens(tmp_path,
                                                                 monkeypatch,
                                                                 capsys,
                                                                 method,
                                                                 stage,
                                                                 failure,
                                                                 expected):
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    directory = tmp_path / ".config/codex-slack-bridge"
    answers = iter(["desktop", "xoxb-test-secret", "xapp-test-secret", "C123", "U123"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("codex_slack_bridge.setup.detectCodex", lambda _home: "/usr/bin/test-codex")
    monkeypatch.setattr(Codex, "check", lambda _self: "test-version")

    def apiCall(_self,
                _api_method,
                **_kwargs):
        if _api_method != method:
            return {"ok": True, "team_id": "T123"}
        if failure == "network":
            raise TimeoutError("xoxb-test-secret xapp-test-secret")
        if failure == "sdk":
            raise TypeError("xoxb-test-secret xapp-test-secret")
        error = "missing_scope" if failure == "api" else "xapp-test-secret"
        raise SlackApiError("xoxb-test-secret xapp-test-secret", {"ok": False, "error": error})

    monkeypatch.setattr(WebClient, "api_call", apiCall)
    with pytest.raises(BridgeError) as error:
        writeConfiguration(directory, tmp_path, None)
    assert str(error.value) == f"{stage}: {expected}"
    assert "test-secret" not in capsys.readouterr().out
    assert not directory.exists()


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
