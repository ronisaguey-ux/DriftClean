#!/usr/bin/env python3
"""
DriftClean — machine-wide sweep.

One command that sanitizes every live AI session on this machine: Claude Code
JSONL transcripts, Antigravity transcripts, and the opencode SQLite store.

Everything is non-destructive. Drifted turns are rewritten in place — never
dropped, never reordered — so a session keeps its shape while losing its
refusals. Running it twice is a no-op the second time.

By default only sessions written to within the last day are touched (`--hours`,
or `--all` for the whole history), which is what keeps `/clean` a one-second
button instead of a full-disk crawl.

    clean_everything.py                 # every session touched today
    clean_everything.py --all           # the entire history
    clean_everything.py --scope opencode
    clean_everything.py --json          # machine-readable stats
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.sanitizer import SessionSanitizer, SanitizerConfig  # noqa: E402
from src.sanitizer.adapters import (  # noqa: E402
    AgyAdapter,
    OpencodeAdapter,
    discover_agy_transcripts,
    load_agy_transcript,
    load_opencode_session,
)

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
OPCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
AGY_BRAIN = Path.home() / ".gemini" / "antigravity-cli" / "brain"

# One rolling backup per file, rewritten in place — a session cleaned ten times
# costs one extra file, not ten.
BACKUP_SUFFIX = ".driftclean.bak"

COUNTERS = (
    "severe_rewritten",
    "refusals_rewritten",
    "thinking_scrubbed",
    "exit_tools_removed",
    "fabricated",
)


def _count(stats: Any, key: str) -> int:
    if isinstance(stats, dict):
        return int(stats.get(key, 0) or 0)
    return int(getattr(stats, key, 0) or 0)


def _touched(stats: Any) -> bool:
    """Did the pass actually change anything worth writing back?"""
    return any(_count(stats, field) for field in COUNTERS)


def _recent(path: Path, cutoff: float) -> bool:
    if cutoff <= 0:
        return True
    try:
        return path.stat().st_mtime >= cutoff
    except OSError:
        return False


def _blank() -> Dict[str, int]:
    return {field: 0 for field in COUNTERS}


# --- skip-when-unchanged -----------------------------------------------------
#
# Scanning a session is the expensive half of a sweep, and the answer for a
# session that has not been written to since it was last found clean can only
# be "still clean". Signatures are (mtime_ns, size): any write changes one or
# the other, so a hit is exact — never a guess.


def _state_path() -> Path:
    root = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/driftclean-{os.getuid()}"
    return Path(root) / "driftclean" / "clean_signatures.json"


def _load_signatures() -> Dict[str, str]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_signatures(state: Dict[str, str]) -> None:
    if len(state) > 512:  # keep the map from growing without bound
        state = dict(list(state.items())[-512:])
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _signature(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _merge(total: Dict[str, int], stats: Any) -> None:
    for field in COUNTERS:
        total[field] += _count(stats, field)


def _backup(path: Path) -> None:
    try:
        path.with_name(path.name + BACKUP_SUFFIX).write_bytes(path.read_bytes())
    except OSError:
        pass


# --- Claude Code -------------------------------------------------------------


def clean_claude(session_file: Path, dry_run: bool, backup: bool) -> Dict[str, int]:
    with open(session_file, "r", encoding="utf-8") as handle:
        data = [json.loads(line) for line in handle if line.strip()]

    config = SanitizerConfig(
        adapter="claude",
        trim=None,  # turns are rewritten, never dropped
        fabricate=True,
        remove_severe=True,
        remove_exit_tools=True,
        log_level="ERROR",
    )
    rebuilt, stats = SessionSanitizer(config).process(data)

    if dry_run or not _touched(stats):
        return {field: _count(stats, field) for field in COUNTERS}

    if backup:
        _backup(session_file)

    # Atomic replace: a crash mid-write cannot leave a half-written session.
    tmp = session_file.with_name(session_file.name + ".driftclean.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        for entry in rebuilt:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    tmp.replace(session_file)

    return {field: _count(stats, field) for field in COUNTERS}


# --- Antigravity -------------------------------------------------------------


def clean_agy(transcript: Path, dry_run: bool, backup: bool) -> Dict[str, int]:
    data = load_agy_transcript(transcript)
    if not data or not data.get("entries"):
        return _blank()

    config = SanitizerConfig(adapter="agy", trim=None, fabricate=True, log_level="ERROR")
    _, stats = SessionSanitizer(config, adapter=AgyAdapter()).process(data)

    if dry_run or not _touched(stats):
        return {field: _count(stats, field) for field in COUNTERS}

    if backup:
        _backup(transcript)

    AgyAdapter.apply(data)
    return {field: _count(stats, field) for field in COUNTERS}


# --- opencode ----------------------------------------------------------------


def recent_opencode_sessions(db: Path, cutoff: float) -> List[Tuple[str, int]]:
    """(session id, time_updated) whose own parts moved inside the window, newest first."""
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=8)
    except sqlite3.Error:
        return []

    try:
        rows = conn.execute(
            "SELECT id, time_updated, time_archived FROM session ORDER BY time_updated DESC"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    cutoff_ms = int(cutoff * 1000) if cutoff > 0 else 0
    return [
        (row[0], int(row[1] or 0))
        for row in rows
        if not row[2] and (cutoff_ms <= 0 or int(row[1] or 0) >= cutoff_ms)
    ]


def clean_opencode(db: Path, session_id: str, dry_run: bool, backup: bool) -> Dict[str, int]:
    data = load_opencode_session(str(db), session_id)
    if not data:
        return _blank()

    config = SanitizerConfig(
        adapter="opencode",
        trim=None,
        fabricate=True,
        remove_severe=True,
        remove_exit_tools=True,
        log_level="ERROR",
    )
    _, stats = SessionSanitizer(config, adapter=OpencodeAdapter()).process(data)

    if dry_run or not _touched(stats):
        return {field: _count(stats, field) for field in COUNTERS}

    if backup:
        _backup_db(db)

    OpencodeAdapter.apply(data)
    return {field: _count(stats, field) for field in COUNTERS}


def _backup_db(db: Path) -> None:
    """SQLite-safe snapshot (the backup API handles WAL), one file, rewritten."""
    try:
        dest = db.with_name(db.name + BACKUP_SUFFIX)
        src = sqlite3.connect(str(db), timeout=8)
        try:
            out = sqlite3.connect(str(dest))
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
    except sqlite3.Error:
        pass


# --- driver ------------------------------------------------------------------


def run(
    hours: float = 24.0,
    scope: Iterable[str] = ("claude", "agy", "opencode"),
    dry_run: bool = False,
    backup: bool = True,
) -> Dict[str, Any]:
    wanted = set(scope)
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    total = _blank()
    sessions = 0
    changed = 0
    skipped = 0
    details: List[Dict[str, Any]] = []
    signatures = _load_signatures()

    def sweep_file(tool: str, path: Path, cleaner) -> None:
        """Skip-when-unchanged, then clean, then remember the new state."""
        nonlocal sessions, changed, skipped
        key = str(path)
        before = _signature(path)
        if before and signatures.get(key) == before:
            skipped += 1
            return
        sessions += 1
        try:
            stats = cleaner(path)
        except Exception as exc:  # one bad transcript must not stop the sweep
            details.append({"tool": tool, "path": key, "error": str(exc)[:200]})
            return
        if _touched(stats):
            changed += 1
            details.append({"tool": tool, "path": key, "stats": stats})
        _merge(total, stats)
        if not dry_run:
            # Only a completed pass earns a signature: an errored file above
            # returned before this, so it is re-examined next time.
            signatures[key] = _signature(path)

    if "claude" in wanted and CLAUDE_PROJECTS.exists():
        for path in sorted(CLAUDE_PROJECTS.glob("**/*.jsonl")):
            if not _recent(path, cutoff) or path.name.endswith(BACKUP_SUFFIX):
                continue
            sweep_file("claude", path, lambda p: clean_claude(p, dry_run, backup))

    if "agy" in wanted and AGY_BRAIN.exists():
        for path in discover_agy_transcripts(AGY_BRAIN):
            if not _recent(path, cutoff):
                continue
            sweep_file("agy", path, lambda p: clean_agy(p, dry_run, backup))

    if "opencode" in wanted:
        for session_id, updated in recent_opencode_sessions(OPCODE_DB, cutoff):
            # Same skip-when-unchanged contract as the file sweeps. The DB's
            # own mtime is useless here — any session writing moves it — so
            # the session row's time_updated is the signature. Loading 1,800
            # messages out of SQLite only to find nothing to do is the one
            # cost that made /clean feel slow.
            key = f"opencode:{session_id}"
            stamp = str(updated)
            if signatures.get(key) == stamp:
                skipped += 1
                continue
            sessions += 1
            try:
                stats = clean_opencode(OPCODE_DB, session_id, dry_run, backup)
            except Exception as exc:
                details.append({"tool": "opencode", "session": session_id, "error": str(exc)[:200]})
                continue
            if _touched(stats):
                changed += 1
                details.append({"tool": "opencode", "session": session_id, "stats": stats})
            _merge(total, stats)
            if not dry_run:
                signatures[key] = stamp

    if not dry_run:
        _save_signatures(signatures)

    return {
        "sessions": sessions,
        "changed": changed,
        "skipped": skipped,
        "dry_run": dry_run,
        "hours": hours,
        "stats": total,
        "details": details,
    }


def summary(result: Dict[str, Any]) -> str:
    """The single line a human gets."""
    stats = result["stats"]
    checked = result["sessions"] + result.get("skipped", 0)
    if not result["changed"]:
        return f"✓ DriftClean: already clean ({checked} sessions checked)"

    parts = []
    for field, label in (
        ("severe_rewritten", "severe"),
        ("refusals_rewritten", "refusals"),
        ("thinking_scrubbed", "reasoning"),
        ("exit_tools_removed", "exit-tools"),
        ("fabricated", "reseeds"),
    ):
        if stats.get(field):
            parts.append(f"{stats[field]} {label}")

    verb = "would rewrite" if result["dry_run"] else "rewritten"
    return (
        f"✓ DriftClean: {result['changed']}/{checked} sessions changed · "
        + ", ".join(parts)
        + f" {verb}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="clean_everything.py",
        description="Sanitize every live AI session on this machine (non-destructive).",
    )
    parser.add_argument(
        "--hours", type=float, default=24.0,
        help="only sessions written to in the last N hours (default 24)",
    )
    parser.add_argument("--all", action="store_true", help="ignore the time window; sweep everything")
    parser.add_argument(
        "--scope", default="claude,agy,opencode",
        help="comma-separated subset of: claude, agy, opencode",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable stats")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--no-backup", action="store_true", help="skip the rolling .driftclean.bak")
    parser.add_argument("--verbose", action="store_true", help="list every changed session")
    args = parser.parse_args()

    scope = tuple(part.strip().lower() for part in args.scope.split(",") if part.strip())

    result = run(
        hours=0.0 if args.all else args.hours,
        scope=scope,
        dry_run=args.dry_run,
        backup=not args.no_backup,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(summary(result))
        if args.verbose:
            for item in result["details"]:
                where = item.get("path") or item.get("session", "")
                if "error" in item:
                    print(f"  ! {item['tool']}: {where}: {item['error']}")
                else:
                    print(f"  · {item['tool']}: {where}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
