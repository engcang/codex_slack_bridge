from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .codex import Codex
from .config import AgentConfig, BridgeError, RouterConfig, identifier, loadConfig
from .transport import RouterClient

MARKER = "#Codex: Managed by codex-slack setup.\n"


def privateWrite(_path: Path,
                 _text: str):
    _path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=_path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(_text)
        os.replace(temporary, _path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def runCommand(_args: list[str]) -> str:
    try:
        result = subprocess.run(_args, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        raise BridgeError(f"{Path(_args[0]).name} timed out; inspect the service before retrying") from None
    if result.returncode:
        raise BridgeError(f"{Path(_args[0]).name} failed; check your user service manager or SSH configuration")
    return result.stdout.strip()


def prompt(_label: str,
           _default: str = "") -> str:
    value = input(f"{_label}" + (f" [{_default}]" if _default else "") + ": ").strip()
    if not value and not _default:
        raise BridgeError(f"{_label} is required")
    return value or _default


def tokenInput(_label: str,
               _prefix: str) -> str:
    value = getpass.getpass(f"{_label} (hidden): ").strip()
    if not re.fullmatch(re.escape(_prefix) + r"[A-Za-z0-9-]+", value):
        raise BridgeError(f"Invalid {_label} format")
    return value


def detectCodex(_home: Path) -> str:
    candidates = []
    for folder in (".vscode/extensions", ".vscode-server/extensions", ".vscode-insiders/extensions"):
        candidates.extend(_home.glob(folder + "/openai.chatgpt-*/bin/*/codex"))
    candidates.sort(key=lambda _path: _path.stat().st_mtime, reverse=True)
    fallback = shutil.which("codex")
    if fallback:
        candidates.append(Path(fallback))
    codex_home = Path(os.environ.get("CODEX_HOME", str(_home / ".codex"))).expanduser()
    for path in candidates:
        try:
            Codex(codex_home, str(path)).check()
            return str(path)
        except BridgeError:
            continue
    return prompt("Path to the VS Code Codex executable")


def systemdQuote(_value: str) -> str:
    if any(ord(char) < 32 for char in _value):
        raise BridgeError("Service paths must not contain control characters")
    return '"' + _value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def serviceText(_command: list[str],
                _environment: Path | None = None) -> str:
    text = MARKER + "[Unit]\nDescription=Codex Slack Bridge\nStartLimitIntervalSec=0\n\n[Service]\nType=simple\nUMask=0077\n"
    if _environment:
        text += "EnvironmentFile=" + systemdQuote(str(_environment)).replace("$$", "$") + "\n"
    text += "ExecStart=" + " ".join(systemdQuote(arg) for arg in _command) + "\n"
    return text + "Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n"


def pairing(_path: Path) -> dict:
    try:
        data = json.loads(_path.read_text())
        identifier(data["agent_id"])
        if data["version"] != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", data["token"]):
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:@[A-Za-z0-9_][A-Za-z0-9_.-]*)?", data["ssh_destination"]):
            raise ValueError
        if type(data["router_port"]) is not int or not 1 <= data["router_port"] <= 65535:
            raise ValueError
        return data
    except (OSError, KeyError, TypeError, ValueError):
        raise BridgeError("Invalid private pairing file") from None


def writeConfiguration(_directory: Path,
                       _home: Path,
                       _join: Path | None):
    codex_home = Path(os.environ.get("CODEX_HOME", str(_home / ".codex"))).expanduser().resolve()
    binary = detectCodex(_home)
    print("Codex: " + Codex(codex_home, binary).check())
    if _join:
        peer = pairing(_join)
        runCommand(["ssh", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oConnectTimeout=10", peer["ssh_destination"], "true"])
        agent_id, token = peer["agent_id"], peer["token"]
        port = 8767
    else:
        agent_id = identifier(prompt("PC name", re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname())[:80]))
        bot = tokenInput("Bot token", "xoxb-")
        app = tokenInput("App token", "xapp-")
        channel = identifier(prompt("Slack channel ID (C... or G...)"))
        user = identifier(prompt("Your Slack member ID (U... or W...)"))
        if not re.fullmatch(r"[CG][A-Z0-9]+", channel) or not re.fullmatch(r"[UW][A-Z0-9]+", user):
            raise BridgeError("Use Slack IDs, not channel or display names")
        from slack_sdk import WebClient

        try:
            team = identifier(WebClient(token=bot).auth_test()["team_id"])
            WebClient(token=app).apps_connections_open()
        except Exception:
            raise BridgeError("Slack token verification failed; check both tokens, connections:write, and the network") from None
        token, port = secrets.token_urlsafe(32), 8766
        router = ("[router]\nhost = \"127.0.0.1\"\nport = 8766\n\n"
                  f"[slack]\nteam_id = {json.dumps(team)}\nchannel_id = {json.dumps(channel)}\n"
                  f"allowed_user_ids = [{json.dumps(user)}]\n\n"
                  f"[agents.{agent_id}]\ntoken_env = \"CODEX_SLACK_AGENT_TOKEN\"\n")
        privateWrite(_directory / "router.env", f"SLACK_BOT_TOKEN={bot}\nSLACK_APP_TOKEN={app}\nCODEX_SLACK_AGENT_TOKEN={token}\n")
        privateWrite(_directory / "router.toml", router)
    agent = (f"[agent]\nagent_id = {json.dumps(agent_id)}\nrouter_url = \"http://127.0.0.1:{port}\"\n"
             f"codex_home = {json.dumps(str(codex_home))}\ncodex_bin = {json.dumps(binary)}\n"
             f"token_file = {json.dumps(str(_directory / 'agent.token'))}\n")
    privateWrite(_directory / "agent.token", token + "\n")
    if _join:
        privateWrite(_directory / "peer.json", json.dumps(peer, indent=4) + "\n")
    #Codex: Write the completion marker last; partial setup is never mistaken for a complete one.
    privateWrite(_directory / "agent.toml", agent)


def installServices(_directory: Path,
                    _home: Path):
    executable = str(Path(sys.executable).parent / "codex-slack")
    units = {"agent": serviceText([executable, "agent", "--config", str(_directory / "agent.toml")])}
    if (_directory / "router.toml").exists():
        units["router"] = serviceText([executable, "router", "--config", str(_directory / "router.toml")], _directory / "router.env")
    if (_directory / "peer.json").exists():
        peer = pairing(_directory / "peer.json")
        units["tunnel"] = serviceText(["/usr/bin/ssh", "-NT", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
                                      "-oExitOnForwardFailure=yes", "-oServerAliveInterval=30", "-oServerAliveCountMax=3",
                                      "-L", f"127.0.0.1:8767:127.0.0.1:{peer['router_port']}", peer["ssh_destination"]])
    unit_dir = _home / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for role in units:
        path = unit_dir / f"codex-slack-{role}.service"
        if path.exists() and not path.read_text().startswith(MARKER):
            raise BridgeError(f"Existing unmanaged service was preserved: {path.name}")
    for role, text in units.items():
        privateWrite(unit_dir / f"codex-slack-{role}.service", text)
    launcher = _home / ".local/bin/codex-slack"
    if launcher.exists() and not launcher.read_text().startswith("#!/bin/sh\n" + MARKER):
        raise BridgeError("Existing ~/.local/bin/codex-slack was preserved; resolve the name conflict first")
    privateWrite(launcher, "#!/bin/sh\n" + MARKER + "exec " + shlex.quote(executable) + ' "$@"\n')
    launcher.chmod(0o700)
    runCommand(["systemctl", "--user", "daemon-reload"])
    names = [f"codex-slack-{role}.service" for role in units]
    runCommand(["systemctl", "--user", "enable", *names])
    runCommand(["systemctl", "--user", "restart", *names])
    user = getpass.getuser()
    if runCommand(["loginctl", "show-user", user, "--property=Linger", "--value"]) != "yes":
        print("Enabling startup before login; Ubuntu may ask for your sudo password.")
        result = subprocess.run(["sudo", "loginctl", "enable-linger", user], check=False)
        if result.returncode:
            raise BridgeError("Services start at login. For startup at boot, run sudo loginctl enable-linger followed by your Ubuntu username, then rerun setup")
    if runCommand(["loginctl", "show-user", user, "--property=Linger", "--value"]) != "yes":
        raise BridgeError("Services are enabled, but boot startup could not be verified; check loginctl enable-linger")


def setup(_join: str | None = None):
    if os.getuid() == 0:
        raise BridgeError("Run setup as the VS Code user, without sudo")
    home = Path.home()
    directory = home / ".config/codex-slack-bridge"
    if not sys.stdin.isatty() and not (directory / "agent.toml").exists() and not _join:
        raise BridgeError("Run sh setup.sh in an interactive terminal to enter your Slack values")
    runCommand(["systemctl", "--user", "show-environment"])
    if (directory / "agent.toml").exists():
        config = loadConfig(directory / "agent.toml", "agent")
        if _join:
            raise BridgeError("This PC is already configured; existing configuration was preserved")
        assert isinstance(config, AgentConfig)
        Codex(config.codex_home_, config.codex_bin_).check()
        print("Reusing existing configuration and session bindings.")
    else:
        if any((directory / name).exists() for name in ("router.toml", "router.env", "agent.token", "peer.json")):
            raise BridgeError("Partial configuration exists; review ~/.config/codex-slack-bridge before setup")
        writeConfiguration(directory, home, Path(_join).expanduser() if _join else None)
    installServices(directory, home)
    config = loadConfig(directory / "agent.toml", "agent")
    assert isinstance(config, AgentConfig)
    if not config.token_file_:
        raise BridgeError("Setup requires an agent token_file")
    client = RouterClient(config.router_url_, config.agent_id_, config.token_file_.read_text().strip())
    for attempt in range(15):
        try:
            client.call("bindings")
            print("Installed. Services start at boot. Open a Codex conversation, then run ~/.local/bin/codex-slack add")
            return
        except BridgeError:
            time.sleep(1)
    raise BridgeError("Services were installed but Router is not ready. Run: journalctl --user -u codex-slack-router -u codex-slack-agent -u codex-slack-tunnel -n 30")


def pair(_agent_id: str,
         _ssh_destination: str,
         _output: str):
    agent_id = identifier(_agent_id)
    directory = Path.home() / ".config/codex-slack-bridge"
    config = loadConfig(directory / "router.toml", "router")
    assert isinstance(config, RouterConfig)
    if agent_id in config.agents_ or Path(_output).expanduser().exists():
        raise BridgeError("Agent ID or output file already exists; nothing was overwritten")
    token = secrets.token_urlsafe(32)
    peer = {"version": 1, "agent_id": agent_id, "token": token, "ssh_destination": _ssh_destination, "router_port": config.port_}
    #Codex: Validate before changing the running Router's configuration.
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "peer.json"
        privateWrite(path, json.dumps(peer))
        pairing(path)
    env_name = "CODEX_SLACK_PEER_" + secrets.token_hex(8).upper()
    env_path, config_path = directory / "router.env", directory / "router.toml"
    privateWrite(env_path, env_path.read_text().rstrip() + f"\n{env_name}={token}\n")
    privateWrite(config_path, config_path.read_text().rstrip() + f"\n\n[agents.{agent_id}]\ntoken_env = \"{env_name}\"\n")
    privateWrite(Path(_output).expanduser(), json.dumps(peer, indent=4) + "\n")
    runCommand(["systemctl", "--user", "restart", "codex-slack-router.service"])
    print(f"Private pairing file saved to {_output}. Transfer it securely to the new PC; run sh setup.sh --join PATH_TO_FILE there.")
