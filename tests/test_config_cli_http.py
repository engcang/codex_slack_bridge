import json
import threading
import urllib.error
import urllib.request

import pytest

from codex_slack_bridge.cli import main
from codex_slack_bridge.config import BridgeError, loadConfig, routerUrl
from codex_slack_bridge.router import makeServer
from codex_slack_bridge.transport import RouterClient


@pytest.mark.parametrize("url",
                         ["http://remote:8766",
                          "http://user:password@localhost:8766",
                          "https://host/path",
                          "https://host/?token=secret"])
def test_unsafe_origin_rejected(url):
    with pytest.raises(BridgeError):
        routerUrl(url)


def test_installed_templates_load_and_init_never_overwrites(tmp_path):
    assert main(["init", "--directory", str(tmp_path)]) == 0
    assert loadConfig(tmp_path / "router.toml", "router").team_id_ == "T_REPLACE_ME"
    assert loadConfig(tmp_path / "agent.toml", "agent").agent_id_ == "workstation"
    original = (tmp_path / "agent.toml").read_bytes()
    assert main(["init", "--directory", str(tmp_path)]) == 1
    assert (tmp_path / "agent.toml").read_bytes() == original
    manifest = json.loads((tmp_path / "slack-app-manifest.json").read_text())
    assert manifest["settings"]["socket_mode_enabled"]


def test_notify_best_effort_only_changes_exit_code(tmp_path,
                                                   capsys):
    config = str(tmp_path / "missing.toml")
    assert main(["notify", "--config", config, "hello"]) == 1
    assert main(["notify", "--config", config, "--best-effort", "hello"]) == 0
    assert "Invalid agent configuration" in capsys.readouterr().err


def test_http_authentication_and_cross_agent_isolation(router):
    server = makeServer(router, {"pc-a": "a" * 40, "pc-b": "b" * 40})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert RouterClient(url, "pc-a", "a" * 40).call("bindings") == {"bindings": []}
        with pytest.raises(BridgeError, match="401"):
            RouterClient(url, "pc-b", "a" * 40).call("bindings")
        request = urllib.request.Request(f"{url}/v1/agents/pc-a/bindings", data=b"{}")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert exc.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
