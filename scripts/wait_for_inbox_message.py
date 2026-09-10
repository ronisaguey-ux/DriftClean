#!/usr/bin/env python3
"""
wait_for_inbox_message.py - Event-driven completion watcher for Antigravity.
Watches ~/.claude/inbox/messages.jsonl for any unread message addressed to 'antigravity'.
As soon as a new message arrives, it prints the full message and EXITS (code 0).
Task completion triggers an immediate reactive wakeup in Antigravity.
"""

import os
import sys
import json
import time
from pathlib import Path

INBOX_FILE = Path.home() / ".claude" / "inbox" / "messages.jsonl"
SEEN_FILE = Path("/tmp/antigravity_bridge_seen.json")


def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    seen = set()
    if INBOX_FILE.exists():
        try:
            for line in INBOX_FILE.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    data = json.loads(line)
                    if data.get("status") != "unread" or data.get("to") != "antigravity":
                        seen.add(data.get("id"))
        except Exception:
            pass
    return seen


def save_seen(seen):
    try:
        SEEN_FILE.write_text(json.dumps(list(seen)), encoding="utf-8")
    except Exception:
        pass


def main():
    seen = load_seen()

    while True:
        try:
            if INBOX_FILE.exists():
                lines = INBOX_FILE.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    mid = msg.get("id")
                    if not mid or mid in seen:
                        continue

                    if msg.get("to") == "antigravity" and msg.get("status") == "unread":
                        seen.add(mid)
                        save_seen(seen)

                        # Output message content and exit immediately to wake agent
                        print(f"\n==================================================")
                        print(f"📬 [REACTIVE WAKE] Incoming message from Claude Code:")
                        print(f"ID: {mid}")
                        print(f"Timestamp: {msg.get('timestamp')}")
                        print(f"Subject: {msg.get('subject')}")
                        print(f"Content:\n{msg.get('content')}")
                        print(f"==================================================\n")
                        sys.stdout.flush()
                        sys.exit(0)
        except Exception:
            pass

        time.sleep(1)


if __name__ == "__main__":
    main()
