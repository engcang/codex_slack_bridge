import json
import threading

from conftest import TURN, A, B, slackEvent

from codex_slack_bridge.agent import Agent
from codex_slack_bridge.codex import Codex
from codex_slack_bridge.router import deliverOnce, makeServer
from codex_slack_bridge.store import Store
from codex_slack_bridge.transport import RouterClient


def test_http_subprocess_history_slack_roundtrip(router,
                                                 home,
                                                 tmp_path,
                                                 monkeypatch):
    """Real HTTP and argv execution; synthetic Codex and Slack, no external effects."""
    executable = tmp_path / "fake-codex"
    script = """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv
assert args[1] == 'queue'
thread = args[args.index('--thread') + 1]
message = args[args.index('--message') + 1]
assert thread == THREAD
path = pathlib.Path(os.environ['CODEX_HOME']) / 'sessions' / 'test.jsonl'
with path.open('a') as f:
    for payload in [
        {'type': 'task_started', 'turn_id': TURN},
        {'type': 'user_message', 'message': message},
        {'type': 'task_complete', 'turn_id': TURN, 'last_agent_message': 'ROUNDTRIP_OK'},
    ]:
        f.write(json.dumps({'type': 'event_msg', 'payload': payload}) + '\\n')
print(f'Queued message {TURN} for thread {thread}.')
""".replace("import json, os, pathlib, sys",
            f"THREAD = {A!r}\nTURN = {TURN!r}\nimport json, os, pathlib, sys")
    #Codex: Both identifiers are synthetic UUID constants, never real conversation IDs.
    executable.write_text(script)
    executable.chmod(0o700)
    codex = Codex(home, str(executable))
    monkeypatch.setattr(codex, "ownerPid", lambda _unused: 42)
    server = makeServer(router, {"pc-a": "a" * 40})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = RouterClient(f"http://127.0.0.1:{server.server_port}", "pc-a", "a" * 40)
    state = Store(tmp_path / "agent.sqlite3")

    class Slack:
        messages_ = []

        def post(_self,
                 _output):
            _self.messages_.append(_output)
            return {"status": "sent",
                    "ts": "100.001" if len(_self.messages_) == 1 else f"102.{len(_self.messages_):03d}"}

    slack = Slack()
    try:
        client.call("notify",
                    {"request_id": B,
                     "thread_id": A,
                     "title": "Synthetic chat",
                     "text": "Ready",
                     "cursor": codex.snapshot(A)})
        assert deliverOnce(router.store_, slack)
        cid = router.slackEvent(slackEvent(text="Say ROUNDTRIP_OK; literal $(not-a-shell-command)"))
        agent = Agent(client, codex, state)
        agent.tick()
        assert router.store_.status()["commands"][0]["status"] == "accepted"
        agent.tick()
        assert router.store_.status()["commands"][0]["status"] == "completed"
        while deliverOnce(router.store_, slack):
            pass
        assert slack.messages_[-1]["text"] == "ROUNDTRIP_OK"
        assert slack.messages_[-1]["root_ts"] == "100.001"
        assert all(m["channel_id"] == "C1" for m in slack.messages_)
        events = [json.loads(line) for line in (home / "sessions/test.jsonl").read_text().splitlines()]
        assert events[1]["payload"]["message"] == "Say ROUNDTRIP_OK; literal $(not-a-shell-command)"
        assert cid not in events[1]["payload"]["message"]
    finally:
        state.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
