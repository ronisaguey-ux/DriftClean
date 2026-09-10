#!/usr/bin/env python3
"""
AutoClean — compatibility front-end for the DriftClean daemon.

This file used to be a Claude-only watcher: it printed banners, wrote a status
file into ~/.claude, and killed and reloaded sessions when it decided they had
drifted. All of that violates the daemon's actual contract, so the watcher now
lives in `examples/autoclean_daemon.py` — silent, non-interfering, traceless,
idempotent, and adapter-aware across Claude Code, Antigravity and opencode.

What remains here is the old command surface, so existing wiring (the
`/autoclean` slash command, cron entries, scripts) keeps working:

    autoclean_claude_daemon.py status    # report daemon state
    autoclean_claude_daemon.py start     # start the background daemon
    autoclean_claude_daemon.py stop      # stop it
    autoclean_claude_daemon.py toggle    # start if stopped, stop if running
    autoclean_claude_daemon.py watch     # run the daemon in the foreground

`--trim` and `--interval` are accepted for backwards compatibility. `--trim`
is ignored on purpose: nothing is ever deleted from a session, only rewritten
in place, so there is no history to trim.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DAEMON = PROJECT_ROOT / "examples" / "autoclean_daemon.py"


def _run(args, foreground: bool = False) -> int:
    """Invoke the real daemon, detached unless we are asked to wait for it."""
    if not DAEMON.is_file():
        print(f"DriftClean daemon missing: {DAEMON}", file=sys.stderr)
        return 1

    cmd = [sys.executable, str(DAEMON)] + args
    if foreground:
        return subprocess.run(cmd).returncode

    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(PROJECT_ROOT),
    ).wait()
    return 0


def _pid_path() -> Path:
    """Same location the daemon uses for its runtime state."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/driftclean-{os.getuid()}"
    return Path(base) / "driftclean" / "daemon.pid"


def _running() -> bool:
    """True when a daemon is alive, read from its own pid file."""
    import json

    try:
        pid = int(json.loads(_pid_path().read_text(encoding="utf-8")))
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoClean (DriftClean daemon front-end)")
    parser.add_argument(
        "action",
        nargs="?",
        default="toggle",
        choices=["start", "stop", "status", "toggle", "watch"],
        help="Daemon action (default: toggle).",
    )
    parser.add_argument("--interval", "-i", type=float, default=None, help="Poll interval in seconds.")
    parser.add_argument("--min-age", dest="min_age", type=float, default=None, help="Quiescence window before editing a file.")
    # Accepted and ignored: DriftClean rewrites turns in place and never deletes
    # history, so there is nothing for a trim limit to do.
    parser.add_argument("--trim", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--once", action="store_true", help="Run a single pass, then exit.")
    args = parser.parse_args()

    passthrough = []
    if args.interval is not None:
        passthrough += ["--interval", str(args.interval)]
    if args.min_age is not None:
        passthrough += ["--min-age", str(args.min_age)]
    if args.once:
        passthrough.append("--once")

    if args.action == "status":
        return _run(["--status"], foreground=True)
    if args.action == "stop":
        return _run(["--stop"], foreground=True)
    if args.action == "watch":
        return _run(passthrough, foreground=True)
    if args.action == "start":
        _run(passthrough)
        return 0

    # toggle
    if _running():
        return _run(["--stop"], foreground=True)
    _run(passthrough)
    return 0


if __name__ == "__main__":
    sys.exit(main())
