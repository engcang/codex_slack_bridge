import datetime
import json
import os
import shlex
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_slack_bridge import notify_slack
from codex_slack_bridge.setup import installServices, privateWrite


@pytest.fixture
def webhook():
    messages = []
    reply = {"status": 200, "body": b"ok"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(_self,
                        *_args):
            pass

        def do_POST(_self):
            assert _self.headers["Content-Type"] == "application/json"
            messages.append(json.loads(_self.rfile.read(int(_self.headers["Content-Length"]))))
            _self.send_response(reply["status"])
            _self.end_headers()
            _self.wfile.write(reply["body"])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/private-webhook", messages, reply
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_webhook_posts_session_host_and_timestamp(webhook,
                                                  monkeypatch,
                                                  capsys):
    url, messages, reply = webhook
    monkeypatch.setenv("SLACK_WEBHOOK_URL", url)
    monkeypatch.setattr("codex_slack_bridge.notify_slack.socket.gethostname", lambda: "pc-a")
    assert notify_slack.main(['[session-a] Results: "ready"', "✓\nTests passed"]) == 0
    assert len(messages) == 1
    text = messages[0]["text"]
    assert text.startswith('*[pc-a]* [session-a] Results: "ready" ✓\nTests passed\n`')
    assert datetime.datetime.fromisoformat(text.rsplit("\n", 1)[-1].strip("`")).tzinfo is not None
    output = capsys.readouterr()
    assert output.out == "Slack notification sent.\n"
    assert output.err == ""


@pytest.mark.parametrize("status,body", [(403, b"private-webhook"), (200, b"private-webhook")])
def test_webhook_failure_is_best_effort_and_does_not_leak_url(webhook,
                                                              monkeypatch,
                                                              capsys,
                                                              status,
                                                              body):
    url, messages, reply = webhook
    reply.update(status=status, body=body)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", url)
    assert notify_slack.main(["[session] Completed"]) == 0
    assert len(messages) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "warning:" in output.err
    assert "private-webhook" not in output.err


@pytest.mark.parametrize("error", [TimeoutError("private-webhook"), urllib.error.URLError("private-webhook")])
def test_webhook_network_failure_never_retries_or_leaks_url(monkeypatch,
                                                            capsys,
                                                            error):
    calls = []
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/private-webhook")

    def unavailable(_request,
                    **_kwargs):
        calls.append(_kwargs)
        raise error

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    assert notify_slack.main(["Completed"]) == 0
    assert calls == [{"timeout": 10}]
    assert "private-webhook" not in capsys.readouterr().err


def test_missing_webhook_does_not_send(monkeypatch,
                                       capsys):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    def forbidden(*_args,
                  **_kwargs):
        raise AssertionError("Missing configuration must not make a request")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    assert notify_slack.main(["Completed"]) == 0
    assert "SLACK_WEBHOOK_URL is not set" in capsys.readouterr().err


def test_installed_launcher_loads_bashrc_and_keeps_message_literal(tmp_path,
                                                                   monkeypatch,
                                                                   webhook):
    url, messages, reply = webhook
    directory = tmp_path / ".config/codex-slack-bridge"
    privateWrite(directory / "agent.toml", "placeholder")
    executable = tmp_path / "venv/bin/python"
    target = executable.with_name("codex-slack-notify")
    privateWrite(target,
                 "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(notify_slack.__file__) + ' "$@"\n')
    target.chmod(0o700)
    monkeypatch.setattr("codex_slack_bridge.setup.sys", SimpleNamespace(executable=str(executable)))
    monkeypatch.setattr("codex_slack_bridge.setup.runCommand", lambda _args: "yes")
    installServices(directory, tmp_path)
    privateWrite(tmp_path / ".bashrc", f"case $- in *i*) ;; *) return ;; esac\nexport SLACK_WEBHOOK_URL={shlex.quote(url)}\n")
    environment = dict(os.environ, HOME=str(tmp_path))
    environment.pop("SLACK_WEBHOOK_URL", None)
    message = '[session] Quotes: "value"; $(touch SHOULD_NOT_EXIST) `whoami` $HOME'
    result = subprocess.run(["bash", "-ic", 'exec "$HOME/.local/bin/codex-slack-notify" "$1"', "_", message],
                            env=environment,
                            cwd=tmp_path,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=10)
    assert result.returncode == 0, result.stderr
    assert "Slack notification sent." in result.stdout
    assert len(messages) == 1
    assert message in messages[0]["text"]
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    assert (Path(tmp_path) / ".local/bin/codex-slack-notify").stat().st_mode & 0o777 == 0o700
