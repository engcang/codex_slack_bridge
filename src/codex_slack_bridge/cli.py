from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import secrets
import signal
import sqlite3
import sys
import threading
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from . import __version__
from .agent import Agent
from .codex import Codex, threadUuid
from .config import AgentConfig, BridgeError, RouterConfig, loadConfig, secret
from .router import TIMESTAMP, runRouter
from .store import Store
from .transport import RouterClient


@contextmanager
def processLock(_path: Path):
    _path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("Another Bridge process is already using this state directory") from None
        yield


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Connect Slack threads to existing Codex sessions")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("router",
                 "agent",
                 "add",
                 "sessions",
                 "notify",
                 "doctor",
                 "status",
                 "resolve-command",
                 "resolve-output",
                 "unbind",
                 "rebind"):
        command = commands.add_parser(name)
        kind = ("router"
                if name in ("router", "status", "resolve-command", "resolve-output")
                else "agent")
        command.add_argument("--config", default=f"~/.config/codex-slack-bridge/{kind}.toml")
        if name in ("add", "sessions"):
            command.add_argument("--all", action="store_true", help="Show all saved conversations at once")
        if name in ("notify", "doctor", "unbind", "rebind", "add"):
            command.add_argument("--thread",
                                 help="Existing root thread UUID; omit to select a conversation" if name == "add" else "Existing root thread UUID; defaults to CODEX_THREAD_ID")
        if name == "notify":
            command.add_argument("--channel",
                                 help="Allowed Slack channel ID (first registration only)")
            command.add_argument("--best-effort",
                                 action="store_true",
                                 help="Warn and return exit code 0 if notification delivery fails")
            command.add_argument("message", nargs="+", help="Notification text")
        if name == "resolve-command":
            command.add_argument("id")
            command.add_argument("--outcome", choices=["completed", "rejected"], required=True)
        if name == "resolve-output":
            command.add_argument("id")
            group = command.add_mutually_exclusive_group(required=True)
            group.add_argument("--sent-ts",
                               help="Timestamp of the message you confirmed exists in Slack")
            group.add_argument("--retry-confirmed-undelivered", action="store_true")
    commands.add_parser("generate-agent-token", help="Print a random Router/Agent shared token")
    init = commands.add_parser("init",
                               help="Write editable configuration and Slack manifest templates")
    init.add_argument("--directory", default="~/.config/codex-slack-bridge")
    setup = commands.add_parser("setup", help="Configure and install Ubuntu boot services")
    setup.add_argument("--join", help="Private pairing file from the Router PC")
    pair = commands.add_parser("pair", help="Prepare a second PC with its own identity and SSH tunnel")
    pair.add_argument("agent_id")
    pair.add_argument("--ssh", required=True, help="Router PC SSH destination, as reachable from the new PC")
    pair.add_argument("--output", required=True, help="Private pairing file to transfer securely")
    return result


def main(_argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if _argv is None else _argv
    args = parser().parse_args(arguments or ["add"])
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "generate-agent-token":
        print(secrets.token_urlsafe(32))
        return 0
    store = None
    try:
        if args.command in ("setup", "pair"):
            from .setup import pair, setup

            if args.command == "setup":
                setup(args.join)
            else:
                pair(args.agent_id, args.ssh, args.output)
            return 0
        if args.command == "init":
            directory = Path(args.directory).expanduser()
            templates = [item
                         for item in files("codex_slack_bridge").joinpath("templates").iterdir()
                         if item.is_file()]
            if any((directory / item.name).exists() for item in templates):
                raise BridgeError("A template already exists in this directory; nothing was overwritten")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for item in templates:
                with (directory / item.name).open("x") as output:
                    output.write(item.read_text())
            print(f"Templates written to {directory}. Edit the IDs, paths and local credentials before starting services.")
            return 0
        kind = ("router"
                if args.command in ("router", "status", "resolve-command", "resolve-output")
                else "agent")
        config = loadConfig(args.config, kind)
        if isinstance(config, RouterConfig):
            store = Store(config.state_dir_ / "router.sqlite3")
            if args.command == "status":
                print(json.dumps(store.status(), indent=2))
                return 0
            if args.command == "resolve-command":
                store.resolveCommand(threadUuid(args.id), args.outcome)
                print("Command resolved; it was not resent.")
                return 0
            if args.command == "resolve-output":
                if args.sent_ts and not TIMESTAMP.fullmatch(args.sent_ts):
                    raise BridgeError("Use the exact Slack timestamp string")
                store.outputResult(args.id,
                                   "sent" if args.sent_ts else "pending",
                                   _ts=args.sent_ts,
                                   _detail="Manually resolved by operator",
                                   _manual=True)
                print("Output resolution saved.")
                return 0
            stop = stopEvent()
            with processLock(config.state_dir_ / "router.lock"):
                runRouter(config, store, stop)
        else:
            assert isinstance(config, AgentConfig)
            codex = Codex(config.codex_home_, config.codex_bin_, config.queue_timeout_)
            if args.command == "sessions":
                browseSessions(codex, False, args.all)
                return 0
            if args.command == "doctor":
                version = codex.check()
                result = {"version": version,
                          "executable": codex.executable(),
                          "agent_id": config.agent_id_}
                tid = args.thread or os.environ.get("CODEX_THREAD_ID")
                if tid:
                    thread = codex.thread(tid)
                    result.update(thread_id=thread["id"],
                                  title=thread["name"] or thread["title"],
                                  history_mode=thread["history_mode"],
                                  owner_pid=codex.ownerPid(tid),
                                  history_cursor=codex.snapshot(tid))
                print(json.dumps(result, indent=2))
                return 0
            token = (config.token_file_.read_text().strip()
                     if config.token_file_
                     else secret(config.token_env_))
            if len(token) < 32:
                raise BridgeError("Agent token must contain at least 32 characters")
            client = RouterClient(config.router_url_, config.agent_id_, token)
            if args.command == "add":
                tid = args.thread
                if not tid:
                    tid = browseSessions(codex, True, args.all)
                    if tid is None:
                        return 0
                tid = threadUuid(tid)
                if not codex.ownerPid(tid):
                    raise BridgeError("Open this existing Codex conversation in VS Code first")
                for binding in client.call("bindings")["bindings"]:
                    if binding["thread_id"] == tid:
                        print("Already registered. Continue in its existing Slack thread.")
                        return 0
                args.thread, args.channel = tid, None
                args.message = ["Ready. Reply in this Slack thread to continue this Codex conversation."]
                args.command = "notify"
            if args.command in ("notify", "unbind", "rebind"):
                tid = threadUuid(args.thread or os.environ.get("CODEX_THREAD_ID"))
                if args.command == "unbind":
                    client.call("unbind", {"thread_id": tid})
                    print("Binding disabled. Existing Codex work was not interrupted.")
                    return 0
                if args.command == "rebind":
                    client.call("rebind", {"thread_id": tid, "cursor": codex.snapshot(tid)})
                    print("Observation reset to the current history position. The Codex session was not resumed.")
                    return 0
                thread = codex.thread(tid)
                result = client.call("notify",
                                     {"request_id": str(uuid4()),
                                      "thread_id": tid,
                                      "title": (thread["name"] or thread["title"] or tid)[:300],
                                      "text": " ".join(args.message),
                                      "channel_id": args.channel,
                                      "cursor": codex.snapshot(tid)})
                print(f"Notification stored by Router for binding {result['binding']['id']}; Slack delivery is asynchronous.")
                return 0
            codex.check()
            store = Store(config.state_dir_ / "agent.sqlite3")
            with processLock(config.state_dir_ / "agent.lock"):
                Agent(client, codex, store).run(stopEvent(), config.poll_interval_)
        return 0
    except (BridgeError, OSError, sqlite3.Error, EOFError) as exc:
        message = str(exc) if isinstance(exc, BridgeError) else type(exc).__name__
        print(f"codex-slack: {message}", file=sys.stderr)
        return 0 if getattr(args, "best_effort", False) else 1
    except KeyboardInterrupt:
        return 0
    finally:
        if store:
            store.close()


def browseSessions(_codex: Codex,
                   _select: bool,
                   _all: bool = False) -> str | None:
    sessions = _codex.sessions()
    if not sessions:
        raise BridgeError("No saved VS Code conversations found")
    shown = len(sessions) if _all else min(5, len(sessions))
    showSessions(sessions[:shown])
    if not sys.stdin.isatty():
        if shown < len(sessions):
            print(f"Showing {shown} of {len(sessions)}. Use --all to show every conversation.")
        if _select:
            raise BridgeError("Use add --thread UUID when running without an interactive terminal")
        return None
    while True:
        more = ", m for more" if shown < len(sessions) else ""
        if not _select and not more:
            return None
        selection = input(("Conversation number" if _select else "Browse conversations") + more + ", q to cancel: ").strip().lower()
        if selection == "q":
            return None
        if selection == "m" and more:
            end = min(shown + 5, len(sessions))
            showSessions(sessions[shown:end], shown + 1)
            shown = end
            continue
        if _select and selection.isdigit() and 1 <= int(selection) <= shown:
            session = sessions[int(selection) - 1]
            if _codex.ownerPid(session["id"]):
                return session["id"]
            print("Open this conversation in VS Code, then enter its number again.")
            continue
        print("Choose one of the displayed options.")


def showSessions(_sessions: list[dict],
                 _start: int = 1):
    for index, session in enumerate(_sessions, _start):
        title = session["name"] or session["title"] or "Untitled"
        #Codex: Titles are untrusted terminal text, not executable escape sequences.
        label = "".join(char if char.isprintable() else " " for char in f"{title} | {session['cwd']}")
        state = "open" if session.get("is_open") else "closed"
        print(f"{index}. [{state}] {label}\n   {session['id']}")


def stopEvent() -> threading.Event:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: stop.set())
    return stop
