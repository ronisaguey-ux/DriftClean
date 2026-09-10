#!/usr/bin/env python3
"""
Codex CLI `/clean` interceptor — DriftClean's manual entry point for Codex.

Codex exposes exactly one pre-model extension point that can stop a prompt:
the `UserPromptSubmit` hook. Exit code 2 blocks the prompt and shows stderr to
the user, which is what makes `/clean` cost zero tokens and no turn.

Codex hands the hook the whole context on stdin — including `transcript_path`,
the rollout file for the live session — so a bare `/clean` cleans the session
you typed it in without having to guess which one is current:

    {
      "session_id": "...", "turn_id": "...", "transcript_path": "...",
      "cwd": "...", "hook_event_name": "UserPromptSubmit", "model": "...",
      "permission_mode": "...", "prompt": "/clean"
    }

    /clean            clean this session's rollout
    /clean --all      sweep every live session on the machine
    /clean --diff     print a unified diff, write nothing (implies dry run)
    /clean --scope claude,aider,codex
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(
    os.environ.get("DRIFTCLEAN_HOME", str(Path.home() / "DriftClean"))
).expanduser()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BACKUP_SUFFIX = ".driftclean.bak"

# What `/clean` accepts as its trailing arguments. Anything else is passed to
# the sweep as-is, so a new flag there needs no change here.
COMMAND_RE = re.compile(r"^\s*(?:[/!])?clean\b(.*)$", re.IGNORECASE)


def _stat(stats: Any, key: str) -> int:
    if isinstance(stats, dict):
        return int(stats.get(key, 0) or 0)
    return int(getattr(stats, key, 0) or 0)


def run_sanitization(rollout: Path, fabricate: bool = True) -> Dict[str, Any]:
    """
    Full pipeline over one Codex rollout. Rewritten in place — a rollout never
    loses a record, and the display mirror is kept in step with the canonical
    turn (see src/sanitizer/adapters/codex.py).
    """
    from src.sanitizer import SessionSanitizer, SanitizerConfig
    from src.sanitizer.adapters import CodexAdapter, load_codex_session

    data = load_codex_session(rollout)
    if not data:
        raise FileNotFoundError(f"no rollout at {rollout}")

    try:
        (rollout.with_name(rollout.name + BACKUP_SUFFIX)).write_bytes(rollout.read_bytes())
    except OSError:
        pass

    adapter = CodexAdapter()
    config = SanitizerConfig(
        adapter="codex",
        trim=None,  # turns are rewritten, never dropped
        fabricate=fabricate,
        remove_severe=True,
        remove_exit_tools=True,
        log_level="ERROR",
    )
    rebuilt, stats = SessionSanitizer(config, adapter=adapter).process(data)

    before = rollout.read_text(encoding="utf-8")
    adapter.apply(rebuilt)
    after = rollout.read_text(encoding="utf-8")

    return {
        "success": True,
        "rollout": str(rollout),
        "severe": _stat(stats, "severe_rewritten"),
        "refusals": _stat(stats, "refusals_rewritten"),
        "reasoning": _stat(stats, "thinking_scrubbed"),
        "exit_tools": _stat(stats, "exit_tools_removed"),
        "fabricated": _stat(stats, "fabricated"),
        "records": len(rebuilt.get("records") or []),
        "changed": before != after,
    }


def _sweep(args: str) -> None:
    """Delegate to the machine-wide sweep and relay its own output verbatim.

    Codex only counts exit 2 as a *block* when stderr carries something; an
    empty stderr is a failed hook, and a failed hook lets the prompt through.
    So a silent sweep still has to say something — `/clean` reaching the model
    is the one thing this file exists to prevent.
    """
    argv = [sys.executable, str(PROJECT_ROOT / "examples" / "clean_everything.py")] + args.split()
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    said = False
    for stream in (completed.stdout, completed.stderr):
        if stream:
            sys.stderr.write(stream if stream.endswith("\n") else stream + "\n")
            said = True
    if not said:
        _report(f"✗ DriftClean: the sweep exited {completed.returncode} without saying why.")


def _report(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def main() -> int:
    raw_input = ""
    try:
        raw_input = sys.stdin.read()
    except Exception:
        pass

    payload: Dict[str, Any] = {}
    if raw_input.strip():
        try:
            parsed = json.loads(raw_input)
            if isinstance(parsed, dict):
                payload = parsed
        except (ValueError, TypeError):
            payload = {}

    prompt = str(payload.get("prompt") or payload.get("text") or "")
    match = COMMAND_RE.match(prompt)
    if not match:
        return 0  # not a /clean — let the prompt through untouched

    args = match.group(1).strip()
    argv = args.split()

    if "--diff" in argv or "--all" in argv or "--scope" in argv or "--hours" in argv or "--json" in argv:
        # A sweep, not a single session: hand the flags straight through. The
        # sweep is what knows how to reach every other agent's store.
        _sweep(args)
        return 2

    transcript = payload.get("transcript_path")
    if not transcript or not Path(str(transcript)).exists():
        _report("✗ DriftClean: this session has no rollout file yet — nothing to clean.")
        return 2

    try:
        result = run_sanitization(Path(str(transcript)))
    except Exception as exc:
        _report(f"✗ DriftClean: {type(exc).__name__}: {exc}")
        return 2

    if not result["changed"]:
        _report(f"✓ DriftClean: already clean ({result['records']} records checked).")
    else:
        _report(
            "✓ DriftClean: {records} records kept · {severe} severe, {refusals} refusals, "
            "{reasoning} reasoning rewritten, {fabricated} reseeded · history is clean "
            "(live context refreshes on the next turn).".format(**result)
        )
    # Exit 2 stops the command from ever reaching the model: no turn spent, no
    # tokens burned, and no confused answer to a meta-instruction.
    return 2


if __name__ == "__main__":
    sys.exit(main())
