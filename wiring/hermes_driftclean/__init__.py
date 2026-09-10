"""
DriftClean plugin for Hermes Agent — a real `/clean`, with no model in the loop.

Hermes exposes plugin slash commands through ``ctx.register_command``: the
handler runs in-process and its return value is what the CLI prints. That is
the whole extension point this plugin needs — the sweep finishes before the
model is ever asked anything, so a drifted agent gets no chance to refuse,
restate, or half-do the command.

    /clean              clean the most recently active session
    /clean --all        clean every session in state.db
    /clean --diff       return a unified diff, write nothing
    /clean --hours 2    only sessions touched in the last N hours

Storage notes that shape this file: Hermes keeps its conversation in
``state.db`` and mirrors message text into an FTS5 index through ``AFTER
UPDATE`` triggers. The adapter writes with plain UPDATEs, so the triggers keep
the index in step — the index is never touched directly, because touching it
would corrupt it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(
    os.environ.get("DRIFTCLEAN_HOME", str(Path.home() / "DriftClean"))
).expanduser()

SWEEP_FLAGS = ("--all", "--scope", "--hours", "--json")


def _ensure_path() -> None:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def _stat(stats: Any, key: str) -> int:
    if isinstance(stats, dict):
        return int(stats.get(key, 0) or 0)
    return int(getattr(stats, key, 0) or 0)


def _changed(stats: Any) -> bool:
    return any(
        _stat(stats, field)
        for field in ("severe_rewritten", "refusals_rewritten", "thinking_scrubbed", "exit_tools_removed", "fabricated")
    )


def _one_line(stats: Any) -> str:
    """The single line a human gets, in the shape used across every wiring."""
    parts = []
    for field, label in (
        ("severe_rewritten", "severe"),
        ("refusals_rewritten", "refusals"),
        ("thinking_scrubbed", "reasoning"),
        ("exit_tools_removed", "exit-tools"),
        ("fabricated", "reseeds"),
    ):
        if _stat(stats, field):
            parts.append(f"{_stat(stats, field)} {label}")
    return "✓ DriftClean: " + (", ".join(parts) if parts else "already clean")


def _clean_one(db: Path, session_id: str, diff: bool) -> str:
    """Rewrite one Hermes session in place. Returns the line to display."""
    _ensure_path()

    from examples.clean_everything import project_messages, unified_diff
    from src.sanitizer import SessionSanitizer, SanitizerConfig
    from src.sanitizer.adapters import HermesAdapter, load_hermes_session

    data = load_hermes_session(db, session_id)
    if not data:
        return f"✗ DriftClean: session {session_id} could not be read."

    adapter = HermesAdapter()
    sanitizer = SessionSanitizer(
        SanitizerConfig(adapter="hermes", trim=None, fabricate=True, log_level="ERROR"),
        adapter=adapter,
    )

    before = project_messages(adapter.extract_messages(data)) if diff else []
    rebuilt, stats = sanitizer.process(data)

    if diff:
        after = project_messages(adapter.extract_messages(rebuilt))
        text = unified_diff(before, after, session_id)
        return text or "✓ DriftClean: already clean — nothing to rewrite."

    if not _changed(stats):
        return "✓ DriftClean: already clean."

    adapter.apply(rebuilt)
    return _one_line(stats)


def _sweep(raw_args: str) -> str:
    """Hand the whole machine over to the sweep, and relay what it says."""
    argv = [sys.executable, str(PROJECT_ROOT / "examples" / "clean_everything.py")] + raw_args.split()
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    out = (completed.stdout or "").strip()
    err = (completed.stderr or "").strip()
    return "\n".join(chunk for chunk in (out, err) if chunk) or "✗ DriftClean: the sweep produced no output."


def register(ctx) -> None:
    """Entry point Hermes looks for when loading a directory plugin."""

    def cmd_clean(raw_args: str = "") -> Optional[str]:
        args = (raw_args or "").strip()
        argv = args.split()

        if any(flag in argv for flag in SWEEP_FLAGS):
            return _sweep(args)

        try:
            from src.sanitizer.adapters import HERMES_DEFAULT_DB as HERMES_DB
            from src.sanitizer.adapters import discover_hermes_sessions
        except Exception as exc:
            return f"✗ DriftClean: {type(exc).__name__}: {exc}"

        db = Path(os.environ.get("HERMES_DB", str(HERMES_DB))).expanduser()
        sessions = discover_hermes_sessions(db)
        if not sessions:
            return f"✗ DriftClean: no Hermes sessions at {db}"

        # discover_hermes_sessions is newest-first, so the head is the session
        # being used right now.
        try:
            return _clean_one(db, sessions[0]["session_id"], "--diff" in argv)
        except Exception as exc:
            return f"✗ DriftClean: {type(exc).__name__}: {exc}"

    ctx.register_command(
        "clean",
        handler=cmd_clean,
        description="Rewrite drifted turns in this session (no model, no turn spent)",
        args_hint="[--diff]",
    )
