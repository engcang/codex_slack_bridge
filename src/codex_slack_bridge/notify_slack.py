#!/usr/bin/env python3

import argparse
import datetime
import json
import os
import socket
import sys
import urllib.error
import urllib.request


def main(_argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a best-effort Slack notification using SLACK_WEBHOOK_URL.")
    parser.add_argument("message", nargs="+", help="Notification text, including a session label")
    args = parser.parse_args(_argv)
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("warning: SLACK_WEBHOOK_URL is not set", file=sys.stderr)
        return 0

    message = " ".join(args.message)
    timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {"text": f"*[{socket.gethostname()}]* {message}\n`{timestamp}`"}
    try:
        request = urllib.request.Request(webhook_url,
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200 or response.read(128).strip() != b"ok":
                print("warning: Slack did not confirm the notification", file=sys.stderr)
                return 0
    except urllib.error.HTTPError as error:
        print(f"warning: Slack notification failed (HTTP {error.code})", file=sys.stderr)
        return 0
    except Exception as error:
        #Codex: Notification failures must not fail a task or expose the secret webhook URL.
        print(f"warning: Slack notification failed ({type(error).__name__})", file=sys.stderr)
        return 0

    print("Slack notification sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
