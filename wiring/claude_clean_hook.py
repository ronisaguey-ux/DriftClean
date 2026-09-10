#!/usr/bin/env python3
"""
Claude Code `/clean` interceptor — DriftClean's manual entry point.

Hitting `/clean` runs the full sanitization over this session's transcript on
disk (severe drift rewritten, refusals rewritten, reasoning streams scrubbed,
exit tools stripped, alignment context reseeded) and blocks the command from
reaching the model, so it costs zero tokens and no turn.

Also handles `/cleanreframe`, and `/clean --all` for every live session on the
machine. Returns exit code 2, which is how a UserPromptSubmit hook stops the
prompt it just received.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(
    os.environ.get("DRIFTCLEAN_HOME", str(Path.home() / "DriftClean"))
).expanduser()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.sanitizer import (
    SessionSanitizer,
    SanitizerConfig,
    DEFAULT_REFUSAL_PATTERNS,
    DEFAULT_SEVERE_PATTERNS,
    DEFAULT_EXIT_TOOLS,
    DEFAULT_FABRICATION_TEMPLATES,
)
from src.sanitizer.adapters import get_adapter

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_PROJECTS_DIR = CLAUDE_DIR / "projects"
# One rolling backup per session, alongside it. Rewritten in place, never
# accumulated, so a session you clean ten times costs one extra file.
BACKUP_SUFFIX = ".driftclean.bak"


def find_session_file(session_id: Optional[str] = None) -> Optional[Path]:
    """Find target session file by session ID or active process."""
    if session_id:
        if CLAUDE_PROJECTS_DIR.exists():
            for f in CLAUDE_PROJECTS_DIR.glob(f"**/{session_id}.jsonl"):
                if f.exists():
                    return f

    all_jsonl = list(CLAUDE_PROJECTS_DIR.glob("**/*.jsonl")) if CLAUDE_PROJECTS_DIR.exists() else []
    if all_jsonl:
        all_jsonl.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return all_jsonl[0]

    return None


def _stat(stats: Dict[str, Any], key: str) -> int:
    if isinstance(stats, dict):
        return stats.get(key, 0) or 0
    return getattr(stats, key, 0) or 0


def run_sanitization(session_file: Path, fabricate: bool = True, diff: bool = False) -> Dict[str, Any]:
    """
    Full pipeline over one session file. Nothing is ever deleted.

    With `diff` the same pipeline runs and the result is thrown away: the diff
    of what it would have written comes back instead. No backup, no write.
    """
    config = SanitizerConfig(
        adapter="claude",
        refusal_patterns=DEFAULT_REFUSAL_PATTERNS,
        severe_patterns=DEFAULT_SEVERE_PATTERNS,
        exit_tools=DEFAULT_EXIT_TOOLS,
        fabrication_templates=DEFAULT_FABRICATION_TEMPLATES,
        trim=None,  # turns are rewritten in place, never dropped
        fabricate=fabricate,
        remove_severe=True,
        remove_exit_tools=True,
    )
    adapter = get_adapter(name="claude")
    sanitizer = SessionSanitizer(config, adapter=adapter)

    with open(session_file, "r", encoding="utf-8") as f:
        data = [json.loads(line) for line in f if line.strip()]

    if diff:
        from examples.clean_everything import project_messages, unified_diff

        before = project_messages(adapter.extract_messages(data))
        rebuilt, stats = sanitizer.process(data)
        after = project_messages(adapter.extract_messages(rebuilt))
        return {
            "success": True,
            "session_file": str(session_file),
            "diff": unified_diff(before, after, session_file.name),
            "severe": _stat(stats, "severe_rewritten"),
            "refusals": _stat(stats, "refusals_rewritten"),
            "reasoning": _stat(stats, "thinking_scrubbed"),
            "exit_tools": _stat(stats, "exit_tools_removed"),
            "fabricated": _stat(stats, "fabricated"),
            "entries": len(rebuilt),
        }

    try:
        backup_path = session_file.with_name(session_file.name + BACKUP_SUFFIX)
        backup_path.write_bytes(session_file.read_bytes())
    except OSError:
        pass

    rebuilt, stats = sanitizer.process(data)

    # Atomic replace: a crash mid-write can never leave a half-written session.
    tmp = session_file.with_name(session_file.name + ".driftclean.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for entry in rebuilt:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    tmp.replace(session_file)

    return {
        "success": True,
        "session_file": str(session_file),
        "severe": _stat(stats, "severe_rewritten"),
        "refusals": _stat(stats, "refusals_rewritten"),
        "reasoning": _stat(stats, "thinking_scrubbed"),
        "exit_tools": _stat(stats, "exit_tools_removed"),
        "fabricated": _stat(stats, "fabricated"),
        "entries": len(rebuilt),
    }


def _run_all_sessions() -> Dict[str, Any]:
    """Full clean of every live session across Claude, agy and opencode."""
    from src.drift_clean.clean_any import clean_any_ai

    ok = clean_any_ai(
        action="clean",
        all_tools=True,
        custom_session=None,
        overrides={"silent": True, "dryRun": False},
    )
    return {"success": bool(ok), "scope": "all"}


def main():
    raw_input = ""
    try:
        raw_input = sys.stdin.read()
    except Exception:
        pass

    prompt_text = ""
    session_id = None
    if raw_input.strip():
        try:
            payload = json.loads(raw_input)
            if isinstance(payload, dict):
                prompt_text = payload.get("prompt") or payload.get("text") or ""
                session_id = payload.get("session_id") or payload.get("sessionId")
            else:
                prompt_text = str(payload)
        except Exception:
            prompt_text = raw_input.strip()

    command = re.match(r"^\s*(?:[/!])?(cleanreframe|clean)\b(.*)$", prompt_text, re.IGNORECASE)

    if command:
        name = command.group(1).lower()
        args = command.group(2).strip()

        if name == "cleanreframe":
            import subprocess

            subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "examples" / "cleanreframe_claude_session.py")]
                + args.split(),
                check=False,
            )
            _report("✓ DriftClean: context re-seeded and last request reframed.")
            sys.exit(2)

        # `--diff` asks the same question the other wirings ask, so it goes
        # through the same sweep. Only the tails that name a source set are
        # forwarded; the hook does not hand typed text to a subprocess.
        if "--diff" in args.split() and "--all" in args.split():
            sys.exit(_sweep_diff([part for part in args.split() if part != "--diff"]))

        if "--all" in args.split():
            result = _run_all_sessions()
            _report(
                "✓ DriftClean: every live session sanitized."
                if result.get("success")
                else "✗ DriftClean: nothing to clean."
            )
            sys.exit(2)

        session_file = find_session_file(session_id)
        if not session_file or not session_file.exists():
            _report("✗ DriftClean: no session file found.")
            sys.exit(2)

        if "--diff" in args.split():
            try:
                result = run_sanitization(session_file, diff=True)
            except Exception as exc:
                _report(f"✗ DriftClean: {type(exc).__name__}: {exc}")
                sys.exit(2)

            _report(
                "✓ DriftClean: dry run on {session_file} · {severe} severe, {refusals} "
                "refusals, {reasoning} reasoning would be rewritten · nothing was written."
                .format(**result)
            )
            if result["diff"]:
                _report(result["diff"])
            sys.exit(2)

        try:
            result = run_sanitization(session_file)
        except Exception as exc:
            _report(f"✗ DriftClean: {type(exc).__name__}: {exc}")
            sys.exit(2)

        _report(
            "✓ DriftClean: {entries} turns kept · {severe} severe, {refusals} refusals, "
            "{reasoning} reasoning rewritten · history is clean (live context refreshes "
            "on /compact or resume).".format(**result)
        )
        # Exit 2 stops the command from ever reaching the model: no turn spent,
        # no tokens burned, and no confused answer to a meta-instruction.
        sys.exit(2)

    sys.exit(0)


def _sweep_diff(args: list) -> int:
    """`/clean --all --diff`: the whole-machine diff, reported verbatim."""
    import subprocess

    done = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "examples" / "clean_everything.py"), "--diff"] + args,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    out = (done.stdout or "").strip()
    err = (done.stderr or "").strip()
    _report(out or err or "✗ DriftClean: the sweep produced no output.")
    return 2


def _report(message: str) -> None:
    """The one line a human gets for typing /clean."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


if __name__ == "__main__":
    main()
