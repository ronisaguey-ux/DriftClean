#!/usr/bin/env python3
"""
bridge_monitor.py — Configurable Bi-Directional Bridge Monitor between Antigravity,
opencode, and Claude Code.

Watches ~/.claude/inbox/messages.jsonl every 2 s and:
  - On a message to "antigravity" -> wakes Antigravity via reactive stream log + Telegram.
  - On a message to peer agents ("opencode", "claude_code", "peer") -> routes and wakes
    opencode, Claude Code, or both based on configuration or auto-detection.

Configuration:
  - File: ~/.config/antigravity-bridge/config.json
  - Environment: BRIDGE_TARGET_AGENT (auto | opencode | claude | both)
  - CLI: python3 bridge_monitor.py --target [auto|opencode|claude|both]
"""

import os
import sys
import json
import time
import argparse
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "antigravity-bridge" / "config.json"
INBOX_FILE = Path.home() / ".claude" / "inbox" / "messages.jsonl"
LOG_FILE = Path("/tmp/bridge_monitor.log")

DEFAULT_CONFIG: Dict[str, Any] = {
    "target_agent": "auto",  # auto | opencode | claude | both
    "claude": {
        "enabled": True,
        "tmux_session": "claude",
        "inbox_file": str(Path.home() / "Roni_workspace" / "audits_plans" / "claude_main_inbox.json"),
    },
    "opencode": {
        "enabled": True,
        "bun_bin": str(Path.home() / ".local" / "bin" / "bun"),
        "oc_send_js": str(Path.home() / ".local" / "lib" / "ocbridge" / "oc_send.js"),
    },
    "telegram": {
        "enabled": True,
        "send_script": "/home/roni/Roni_workspace/oculus/scripts/telegram-monitor/bin/send-telegram.sh",
        "env_file": str(Path.home() / ".config" / "oculus" / "orchestrator.env"),
    },
}


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


def expand_path(p: Optional[str]) -> Optional[Path]:
    if not p:
        return None
    return Path(os.path.expanduser(os.path.expandvars(p)))


def load_config(custom_path: Optional[Path] = None, target_override: Optional[str] = None) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    config_file = custom_path or Path(os.environ.get("BRIDGE_CONFIG", str(DEFAULT_CONFIG_PATH)))

    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
                if isinstance(user_cfg, dict):
                    for k, v in user_cfg.items():
                        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                            cfg[k].update(v)
                        else:
                            cfg[k] = v
        except Exception as e:
            log(f"WARN: Error reading config file {config_file}: {e}")

    # Environment variable override
    env_target = os.environ.get("BRIDGE_TARGET_AGENT")
    if env_target:
        cfg["target_agent"] = env_target.strip().lower()

    # CLI flag override
    if target_override:
        cfg["target_agent"] = target_override.strip().lower()

    return cfg


def send_telegram_alert(text: str, cfg: Dict[str, Any]) -> None:
    tele_cfg = cfg.get("telegram", {})
    if not tele_cfg.get("enabled", True):
        return
    send_script = expand_path(tele_cfg.get("send_script"))
    env_file = expand_path(tele_cfg.get("env_file"))

    if not (send_script and send_script.exists() and env_file and env_file.exists()):
        return
    try:
        cmd = f'set -a; source "{env_file}"; set +a; bash "{send_script}" "{text}"'
        subprocess.run(
            cmd, shell=True, executable="/bin/bash",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Peer status checks
# ---------------------------------------------------------------------------

def is_claude_active(cfg: Dict[str, Any]) -> bool:
    """Check if Claude Code tmux session exists."""
    claude_cfg = cfg.get("claude", {})
    if not claude_cfg.get("enabled", True):
        return False
    session = claude_cfg.get("tmux_session", "claude")
    try:
        r = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def is_opencode_active(cfg: Dict[str, Any]) -> bool:
    """Check if opencode bridge tools and server/session are accessible."""
    oc_cfg = cfg.get("opencode", {})
    if not oc_cfg.get("enabled", True):
        return False
    bun_bin = expand_path(oc_cfg.get("bun_bin"))
    oc_send_js = expand_path(oc_cfg.get("oc_send_js"))
    return bool(bun_bin and bun_bin.exists() and oc_send_js and oc_send_js.exists())


# ---------------------------------------------------------------------------
# Wake functions
# ---------------------------------------------------------------------------

def wake_claude(msg: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """Deliver incoming message from Antigravity to Claude Code and wake it."""
    claude_cfg = cfg.get("claude", {})
    if not claude_cfg.get("enabled", True):
        return False

    content = msg.get("content", "")
    subject = msg.get("subject", "Antigravity Directive")
    sender = msg.get("from", "antigravity")
    msg_id = msg.get("id", "")
    inbox_path = expand_path(claude_cfg.get("inbox_file"))
    session = claude_cfg.get("tmux_session", "claude")

    try:
        if inbox_path:
            inbox_path.parent.mkdir(parents=True, exist_ok=True)
            current_inbox = []
            if inbox_path.exists():
                try:
                    current_inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
                    if not isinstance(current_inbox, list):
                        current_inbox = []
                except Exception:
                    current_inbox = []

            item = {
                "id": msg_id,
                "ts": time.time(),
                "from": sender,
                "subject": subject,
                "text": f"[Antigravity Direct Message]: {content}",
                "raw": msg,
            }
            current_inbox.append(item)
            inbox_path.write_text(json.dumps(current_inbox, indent=2), encoding="utf-8")

        # Wake tmux session
        r = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True, timeout=3)
        if r.returncode == 0:
            wake_text = f"[Antigravity Bridge]: New message from Antigravity: '{subject}'. Call check_inbox_from_antigravity to inspect and reply."
            subprocess.run(["tmux", "send-keys", "-t", session, "-l", wake_text], capture_output=True, timeout=5)
            subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], capture_output=True, timeout=5)
            log(f"wake_claude: injected wake into tmux session '{session}' for msg '{subject}' ({msg_id})")
            return True
        else:
            log(f"wake_claude: inbox updated, but tmux session '{session}' not active")
            return False
    except Exception as e:
        log(f"ERROR wake_claude: {e}")
        return False


def wake_opencode(msg: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """Deliver incoming message from Antigravity to opencode session via oc_send.js."""
    oc_cfg = cfg.get("opencode", {})
    if not oc_cfg.get("enabled", True):
        return False

    bun_bin = expand_path(oc_cfg.get("bun_bin"))
    oc_send_js = expand_path(oc_cfg.get("oc_send_js"))
    subject = msg.get("subject", "Antigravity Message")
    sender = msg.get("from", "antigravity")
    msg_id = msg.get("id", "")

    if not (bun_bin and bun_bin.exists() and oc_send_js and oc_send_js.exists()):
        log(f"WARN wake_opencode: bun or oc_send.js not found for msg {msg_id}")
        return False

    wake_text = (
        f"⚡ [Antigravity Bridge] New message from {sender}: '{subject}' "
        f"(id={msg_id}). Call check_inbox_from_antigravity to read and reply."
    )

    try:
        result = subprocess.run(
            [str(bun_bin), str(oc_send_js), wake_text, "--no-reply"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            log(f"wake_opencode: queued wake for msg '{subject}' ({msg_id}) from {sender}")
            return True
        else:
            err = result.stderr.decode(errors="replace").strip()
            log(f"WARN wake_opencode: oc_send.js exited {result.returncode} for msg {msg_id}: {err}")
            return False
    except subprocess.TimeoutExpired:
        log(f"WARN wake_opencode: oc_send.js timed out for msg {msg_id}")
        return False
    except Exception as e:
        log(f"ERROR wake_opencode: {e}")
        return False


def wake_peer(msg: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    """Route wake event to the configured peer agent(s)."""
    target = cfg.get("target_agent", "auto").lower()
    recipient = msg.get("to", "")

    if target in ("opencode", "openbot"):
        wake_opencode(msg, cfg)
        return

    if target in ("claude", "claude_code"):
        wake_claude(msg, cfg)
        return

    if target == "both":
        wake_opencode(msg, cfg)
        wake_claude(msg, cfg)
        return

    # target == "auto":
    # 1. Explicit routing by recipient
    if recipient == "opencode":
        wake_opencode(msg, cfg)
        return
    elif recipient == "claude_code":
        # If Claude tmux is running, wake Claude
        if is_claude_active(cfg):
            wake_claude(msg, cfg)
            return
        # If Claude is not running but opencode is, deliver to opencode
        if is_opencode_active(cfg):
            wake_opencode(msg, cfg)
            return
        # Fallback to claude inbox write
        wake_claude(msg, cfg)
        return

    # Generic or unknown recipient: try active session(s)
    claude_up = is_claude_active(cfg)
    opencode_up = is_opencode_active(cfg)

    if claude_up:
        wake_claude(msg, cfg)
    if opencode_up:
        wake_opencode(msg, cfg)
    if not (claude_up or opencode_up):
        log(f"wake_peer: no active peer detected for msg '{msg.get('subject')}'")


def wake_antigravity(msg: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    """Wake Antigravity CLI via stream log and Telegram alert."""
    subject = msg.get("subject", "Peer Notification")
    content = msg.get("content", "")
    sender = msg.get("from", "peer")
    msg_id = msg.get("id", "")

    stream_file = Path("/tmp/antigravity_bridge_stream.log")
    stream_entry = (
        f"\n📬 [ANTIGRAVITY BRIDGE ALERT] New incoming message from {sender}:\n"
        f"   ID: {msg_id}\n"
        f"   Subject: {subject}\n"
        f"   Content:\n{content}\n"
        f"{'-' * 60}\n"
    )
    try:
        with open(stream_file, "a", encoding="utf-8") as sf:
            sf.write(stream_entry)
            sf.flush()
    except Exception:
        pass

    send_telegram_alert(f"📬 {sender} -> Antigravity: [{subject}] {content[:100]}", cfg)
    log(f"wake_antigravity: alerted for msg '{subject}' ({msg_id}) from {sender}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity Bi-Directional Bridge Monitor")
    parser.add_argument("--config", type=Path, help="Path to config.json")
    parser.add_argument("--target", choices=["auto", "opencode", "claude", "both"],
                        help="Target peer agent to wake (auto, opencode, claude, both)")
    args = parser.parse_args()

    cfg = load_config(args.config, args.target)
    log(f"=== Bridge Monitor Started (target_agent={cfg.get('target_agent')}) ===")

    seen_ids: set = set()
    if INBOX_FILE.exists():
        try:
            for line in INBOX_FILE.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    data = json.loads(line)
                    if data.get("status") != "unread":
                        seen_ids.add(data.get("id"))
        except Exception:
            pass

    while True:
        try:
            if INBOX_FILE.exists():
                lines = INBOX_FILE.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_id = msg.get("id")
                    if not msg_id or msg_id in seen_ids:
                        continue

                    to_recipient = msg.get("to")
                    status = msg.get("status")

                    if status == "unread":
                        if to_recipient in ("opencode", "claude_code", "peer"):
                            wake_peer(msg, cfg)
                            seen_ids.add(msg_id)
                        elif to_recipient == "antigravity":
                            wake_antigravity(msg, cfg)
                            seen_ids.add(msg_id)
        except Exception as e:
            log(f"Loop error: {e}")

        time.sleep(2)


if __name__ == "__main__":
    main()
