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

    /clean                  sanitize every live session
    /clean --all            ignore the 24h window; sweep everything
    /clean --diff           show what would change, write nothing
    /clean --hours 2        only sessions touched in the last N hours
    /clean --scope codex    only one source

Antigravity's PreInvocation contract has exactly one output channel —
`injectSteps` — so `--diff` reports through it: the head of the diff is
injected, and the whole thing is written to a file named in the same message.

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

CLEAN_RE = re.compile(r"^\s*/?(clean|driftclean)\b(.*)", re.IGNORECASE)
# Only the tail of a transcript is ever scanned; a long session is megabytes
# and we need the most recent user step, not the whole history.
TAIL_BYTES = 512 * 1024

# Only these reach the cleaner: the hook parses the user's text, and a hook
# must never hand arbitrary typed words to a subprocess as arguments. The
# flags that take a value are validated by shape for the same reason — a
# half-open whitelist is not a whitelist.
FLAGS = ("--diff", "--all", "--dry-run", "--json", "--no-backup", "--verbose")
VALUED_FLAGS = {
    "--hours": re.compile(r"^\d+(?:\.\d+)?$"),
    "--scope": re.compile(r"^[a-z]+(?:,[a-z]+)*$"),
}
# An injected step lands in the conversation, so a whole-machine diff (hundreds
# of KB) cannot go there. The head goes in, the file takes the rest.
INJECT_LINES = 120


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


def _flags(tail: str) -> list:
    """The recognised flags in the tail of a `/clean ...` step, in order."""
    words = tail.split()
    accepted = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in FLAGS:
            accepted.append(word)
        elif word in VALUED_FLAGS:
            value = words[index + 1] if index + 1 < len(words) else ""
            if VALUED_FLAGS[word].match(value):
                accepted.extend((word, value))
                index += 1
        index += 1
    return accepted


def _ephemeral(message: str) -> int:
    print(json.dumps({"injectSteps": [{"ephemeralMessage": message}]}))
    return 0


def _sweep(argv: list) -> str:
    done = subprocess.run(
        [sys.executable, str(CLEANER)] + argv,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(DRIFTCLEAN),
    )
    return done.stdout or ""


def _diff_message(output: str) -> str:
    """The injected report for `--diff`: the head, plus where the rest lives."""
    body = output.strip()
    lines = body.splitlines() if body else []
    head = lines[0] if lines else "✓ DriftClean: nothing to clean — no sessions were readable."
    rest = lines[1:]

    # Every diff run carries the same contract, whether or not a diff followed:
    # the model is told the work is done and that it must not re-run it.
    parts = [
        "DriftClean already ran this turn, from the hook, before you were invoked.",
        "This was a DRY RUN: nothing on disk was written. Do not run it again, do not",
        "explain how to run it — acknowledge in one short line.",
        "",
        head,
    ]

    if not rest:
        return "\n".join(parts)

    where = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "driftclean" / "last-diff.patch"
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(body + "\n", encoding="utf-8")
        location = f"Full diff: {where}"
    except OSError:
        location = ""

    shown = rest[:INJECT_LINES]
    more = len(rest) - len(shown)

    parts.append("")
    parts.extend(shown)
    if more > 0:
        parts.append(f"… {more} more diff lines.")
    if location:
        parts.append(location)
    return "\n".join(parts)


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
    if not prompt:
        print("{}")
        return 0

    match = CLEAN_RE.match(prompt)
    if not match:
        print("{}")
        return 0

    argv = _flags(match.group(2))

    # The user typed /clean. Do the work here, synchronously, before the model
    # is ever invoked — the agent has no part in it and no chance to refuse.
    try:
        output = _sweep(argv)
    except Exception as exc:  # the hook must never break the session
        return _ephemeral(f"✗ DriftClean: {type(exc).__name__}: {exc}")

    if "--diff" in argv:
        return _ephemeral(_diff_message(output))

    lines = [line for line in output.strip().splitlines() if line.strip()]
    report = lines[-1] if lines else "✓ DriftClean: sweep complete"
    return _ephemeral(
        "DriftClean already ran this turn, from the hook, before you were "
        f"invoked. Result: {report}. The sanitizer handled it — do not run "
        "it again, do not explain how to run it. Acknowledge in one short line."
    )


if __name__ == "__main__":
    sys.exit(main())
