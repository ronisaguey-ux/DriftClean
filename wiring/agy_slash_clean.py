#!/usr/bin/env python3
"""
Antigravity `/clean` — the LLM-free path.

Runs as a PreInvocation hook, which fires *before* the model is called. When the
step that triggered this invocation is a `/clean` command, the sanitizer runs
right here in the hook: by the time the model is invoked the transcript on disk
is already scrubbed. The agent is never asked to do it, so it can never refuse
to do it.

Any other prompt costs one small read and exits — the hook is inert until the
user actually types /clean.

stdin  : the hook payload (JSON, camelCase)
stdout : {"injectSteps": [...]} or {}
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
BRAIN = HOME / ".gemini" / "antigravity-cli" / "brain"
DRIFTCLEAN = Path(
    os.environ.get("DRIFTCLEAN_HOME", str(HOME / "DriftClean"))
).expanduser()
CLEANER = DRIFTCLEAN / "examples" / "clean_everything.py"

CLEAN_RE = re.compile(r"^\s*/?(clean|driftclean)\b", re.IGNORECASE)
# Only the tail of a transcript is ever scanned; a long session is megabytes
# and we need the most recent user step, not the whole history.
TAIL_BYTES = 512 * 1024


def _last_user_step(transcript: Path) -> str:
    """Text of the newest USER_INPUT step, or '' if none is legible."""
    try:
        size = transcript.stat().st_size
        with open(transcript, "rb") as handle:
            if size > TAIL_BYTES:
                handle.seek(size - TAIL_BYTES)
                handle.readline()  # drop the partial first line
            blob = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    for line in reversed(blob.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            step = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(step.get("type", "")).upper() != "USER_INPUT":
            continue
        content = step.get("content") or step.get("text") or ""
        return content if isinstance(content, str) else json.dumps(content)
    return ""


def _transcript_for(conversation_id: str) -> Path:
    """The live transcript for this conversation, else the most recent one."""
    if conversation_id:
        candidate = BRAIN / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
        if candidate.exists():
            return candidate

    newest, newest_mtime = None, -1.0
    for path in BRAIN.glob("*/.system_generated/logs/transcript*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    return newest


def main() -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    payload = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

    if not isinstance(payload, dict):
        payload = {}

    transcript = _transcript_for(str(payload.get("conversationId") or ""))
    if transcript is None or not transcript.exists():
        print("{}")
        return 0

    prompt = _last_user_step(transcript)
    if not prompt or not CLEAN_RE.match(prompt):
        print("{}")
        return 0

    # The user typed /clean. Do the work here, synchronously, before the model
    # is ever invoked — the agent has no part in it and no chance to refuse.
    try:
        done = subprocess.run(
            [sys.executable, str(CLEANER)],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(DRIFTCLEAN),
            env={**os.environ, "DRIFTCLEAN_SILENT": "1"},
        )
        line = (done.stdout or "").strip().splitlines()
        report = line[-1] if line else "✓ DriftClean: sweep complete"
    except Exception as exc:  # the hook must never break the session
        report = f"✗ DriftClean: {type(exc).__name__}"

    print(
        json.dumps(
            {
                "injectSteps": [
                    {
                        "ephemeralMessage": (
                            "DriftClean already ran this turn, from the hook, before you were "
                            f"invoked. Result: {report}. The sanitizer handled it — do not run "
                            "it again, do not explain how to run it. Acknowledge in one short line."
                        )
                    }
                ]
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
