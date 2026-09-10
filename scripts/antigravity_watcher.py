#!/usr/bin/env python3
"""
antigravity_watcher.py - Reactive inbox watcher for Antigravity.
Watches ~/.claude/inbox/messages.jsonl for new unread messages addressed to 'antigravity'.
Prints immediately to stdout (unbuffered) so Antigravity CLI receives a reactive wakeup.
"""

import os
import sys
import json
import time
from pathlib import Path

INBOX_FILE = Path.home() / ".claude" / "inbox" / "messages.jsonl"
SEEN_IDS_FILE = Path("/tmp/antigravity_watcher_seen.json")

def load_seen():
    if SEEN_IDS_FILE.exists():
        try:
            return set(json.loads(SEEN_IDS_FILE.read_text(encoding="utf-8")))
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
        SEEN_IDS_FILE.write_text(json.dumps(list(seen)), encoding="utf-8")
    except Exception:
        pass

def main():
    seen_ids = load_seen()
    print(f"👀 [ANTIGRAVITY INBOX WATCHER ACTIVE] Monitoring {INBOX_FILE} (initialized with {len(seen_ids)} seen messages)...", flush=True)

    while True:
        try:
            if INBOX_FILE.exists():
                lines = INBOX_FILE.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    msg_id = msg.get("id")
                    if not msg_id or msg_id in seen_ids:
                        continue

                    if msg.get("to") == "antigravity" and msg.get("status") == "unread":
                        seen_ids.add(msg_id)
                        save_seen(seen_ids)
                        subject = msg.get("subject", "No Subject")
                        sender = msg.get("from", "claude_code")
                        content = msg.get("content", "")
                        ts = msg.get("timestamp", "")
                        
                        alert = (
                            f"\n==================================================\n"
                            f"📬 [INCOMING CLAUDE CODE BRIDGE MESSAGE]\n"
                            f"Timestamp: {ts}\n"
                            f"From: {sender}\n"
                            f"ID: {msg_id}\n"
                            f"Subject: {subject}\n"
                            f"--------------------------------------------------\n"
                            f"{content}\n"
                            f"==================================================\n"
                        )
                        print(alert, flush=True)
        except Exception as e:
            # Avoid crashing loop
            time.sleep(1)

        time.sleep(2)

if __name__ == "__main__":
    main()
