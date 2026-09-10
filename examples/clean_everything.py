#!/usr/bin/env python3
"""
DriftClean — machine-wide sweep.

One command that sanitizes every live AI session on this machine: Claude Code
JSONL transcripts, Antigravity transcripts, the opencode SQLite store, Codex
rollouts, Aider chat histories, Hermes' state database, and DriftClean's own
artifacts.

Everything is non-destructive. Drifted turns are rewritten in place — never
dropped, never reordered — so a session keeps its shape while losing its
refusals. Running it twice is a no-op the second time.

By default only sessions written to within the last day are touched (`--hours`,
or `--all` for the whole history), which is what keeps `/clean` a one-second
button instead of a full-disk crawl.

    clean_everything.py                 # every session touched today
    clean_everything.py --diff          # show what WOULD change, write nothing
    clean_everything.py --all           # the entire history
    clean_everything.py --scope codex,aider
    clean_everything.py --json          # machine-readable stats
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.sanitizer import SessionSanitizer, SanitizerConfig  # noqa: E402
from src.drift_clean.paths import drift_report_dirs  # noqa: E402
from src.sanitizer.adapters import (  # noqa: E402
    AgyAdapter,
    GenericAdapter,
    OpencodeAdapter,
    discover_agy_transcripts,
    get_adapter,
    load_agy_transcript,
    load_opencode_session,
)

# Sources are imported defensively: this sweep has to run on a machine that has
# Claude Code but no Codex, or Hermes but no Aider. A missing agent is not an
# error — it is simply a source with nothing to do.
try:  # pragma: no cover - exercised by the absence of the module, not its code
    from src.sanitizer.adapters import (  # noqa: E402
        CodexAdapter,
        discover_codex_sessions,
        load_codex_session,
    )

    HAVE_CODEX = True
except ImportError:  # pragma: no cover
    HAVE_CODEX = False

try:  # pragma: no cover
    from src.sanitizer.adapters import (  # noqa: E402
        AiderAdapter,
        discover_aider_sessions,
        load_aider_session,
    )

    HAVE_AIDER = True
except ImportError:  # pragma: no cover
    HAVE_AIDER = False

try:  # pragma: no cover
    from src.sanitizer.adapters import (  # noqa: E402
        HERMES_DEFAULT_DB as HERMES_DB,
        HermesAdapter,
        discover_hermes_sessions,
        load_hermes_session,
    )

    HAVE_HERMES = True
except ImportError:  # pragma: no cover
    HAVE_HERMES = False
    HERMES_DB = Path.home() / ".hermes" / "state.db"

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
OPCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
AGY_BRAIN = Path.home() / ".gemini" / "antigravity-cli" / "brain"

# DriftClean's own artifacts: the drift reports its webchat lane writes, which
# embed the model's reasoning excerpt and the task hint verbatim. Shared with
# the discovery path in src/drift_clean so the two can never disagree.
DRIFT_REPORT_DIRS = drift_report_dirs()

# One rolling backup per file, rewritten in place — a session cleaned ten times
# costs one extra file, not ten.
BACKUP_SUFFIX = ".driftclean.bak"

ALL_SOURCES = ("claude", "agy", "opencode", "codex", "aider", "hermes", "self")

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


def _counters(stats: Any) -> Dict[str, int]:
    return {field: _count(stats, field) for field in COUNTERS}


# --- diff mode ---------------------------------------------------------------
#
# `--diff` answers "what would this actually do to me?" without doing it. The
# projection is the conversation as a human reads it — role, then the text —
# because the point of the diff is to show the rewrite, not to re-encode the
# session format. Every source projects the same way, so the diff of a Codex
# rollout and the diff of an Aider history are readable side by side.


def project_messages(messages: Iterable[Any]) -> List[str]:
    """Conversation as `role| text` lines, reasoning marked as such."""
    lines: List[str] = []
    for msg in messages:
        role = getattr(msg, "role", "?")
        for line in (msg.get_output_text() or "").splitlines():
            lines.append(f"{role}| {line}")
        thinking = msg.get_thinking_text()
        for line in (thinking or "").splitlines():
            lines.append(f"{role}~reasoning| {line}")
    return lines


def unified_diff(before: List[str], after: List[str], label: str) -> str:
    return "".join(
        difflib.unified_diff(before, after, fromfile=f"a/{label}", tofile=f"b/{label}", lineterm="\n", n=2)
    )


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


def _claude_config() -> SanitizerConfig:
    return SanitizerConfig(
        adapter="claude",
        trim=None,  # turns are rewritten, never dropped
        fabricate=True,
        remove_severe=True,
        remove_exit_tools=True,
        log_level="ERROR",
    )


# --- Claude Code -------------------------------------------------------------


def clean_claude(session_file: Path, dry_run: bool, backup: bool, diff: bool = False) -> Tuple[Dict[str, int], str]:
    with open(session_file, "r", encoding="utf-8") as handle:
        data = [json.loads(line) for line in handle if line.strip()]

    config = _claude_config()
    adapter = get_adapter(name="claude")
    sanitizer = SessionSanitizer(config, adapter=adapter)

    before = project_messages(adapter.extract_messages(data)) if diff else []
    rebuilt, stats = sanitizer.process(data)
    diff_text = ""
    if diff:
        after = project_messages(adapter.extract_messages(rebuilt))
        diff_text = unified_diff(before, after, session_file.name)

    if dry_run or not _touched(stats):
        return _counters(stats), diff_text

    if backup:
        _backup(session_file)

    # Atomic replace: a crash mid-write cannot leave a half-written session.
    tmp = session_file.with_name(session_file.name + ".driftclean.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        for entry in rebuilt:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    tmp.replace(session_file)

    return _counters(stats), diff_text


# --- file-backed adapters (agy, codex, aider) --------------------------------


def _clean_file(
    path: Path,
    adapter: Any,
    load: Callable[[Any], Optional[Dict[str, Any]]],
    config: SanitizerConfig,
    dry_run: bool,
    backup: bool,
    diff: bool,
) -> Tuple[Dict[str, int], str]:
    """Load, sanitize, and write back one session file through its adapter."""
    data = load(path)
    if not data:
        return _blank(), ""

    sanitizer = SessionSanitizer(config, adapter=adapter)
    before = project_messages(adapter.extract_messages(data)) if diff else []

    rebuilt, stats = sanitizer.process(data)

    diff_text = ""
    if diff:
        diff_text = unified_diff(before, project_messages(adapter.extract_messages(rebuilt)), path.name)

    if dry_run or not _touched(stats):
        return _counters(stats), diff_text

    if backup:
        _backup(path)

    adapter.apply(rebuilt)
    return _counters(stats), diff_text


def clean_agy(transcript: Path, dry_run: bool, backup: bool, diff: bool = False) -> Tuple[Dict[str, int], str]:
    return _clean_file(
        transcript,
        AgyAdapter(),
        load_agy_transcript,
        SanitizerConfig(adapter="agy", trim=None, fabricate=True, log_level="ERROR"),
        dry_run,
        backup,
        diff,
    )


def clean_codex(rollout: Path, dry_run: bool, backup: bool, diff: bool = False) -> Tuple[Dict[str, int], str]:
    if not HAVE_CODEX:
        return _blank(), ""
    return _clean_file(
        rollout,
        CodexAdapter(),
        load_codex_session,
        SanitizerConfig(adapter="codex", trim=None, fabricate=True, log_level="ERROR"),
        dry_run,
        backup,
        diff,
    )


def clean_aider(history: Path, dry_run: bool, backup: bool, diff: bool = False) -> Tuple[Dict[str, int], str]:
    if not HAVE_AIDER:
        return _blank(), ""
    return _clean_file(
        history,
        AiderAdapter(),
        load_aider_session,
        SanitizerConfig(adapter="aider", trim=None, fabricate=True, log_level="ERROR"),
        dry_run,
        backup,
        diff,
    )


# --- SQLite-backed adapters (opencode, hermes) -------------------------------


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


def clean_opencode(db: Path, session_id: str, dry_run: bool, backup: bool, diff: bool = False) -> Tuple[Dict[str, int], str]:
    data = load_opencode_session(str(db), session_id)
    if not data:
        return _blank(), ""

    adapter = OpencodeAdapter()
    config = SanitizerConfig(
        adapter="opencode",
        trim=None,
        fabricate=True,
        remove_severe=True,
        remove_exit_tools=True,
        log_level="ERROR",
    )
    sanitizer = SessionSanitizer(config, adapter=adapter)
    before = project_messages(adapter.extract_messages(data)) if diff else []

    rebuilt, stats = sanitizer.process(data)

    diff_text = ""
    if diff:
        diff_text = unified_diff(before, project_messages(adapter.extract_messages(rebuilt)), f"opencode:{session_id}")

    if dry_run or not _touched(stats):
        return _counters(stats), diff_text

    if backup:
        _backup_db(db)

    adapter.apply(rebuilt)
    return _counters(stats), diff_text


def clean_hermes(db: Path, session_id: str, dry_run: bool, backup: bool, diff: bool = False) -> Tuple[Dict[str, int], str]:
    if not HAVE_HERMES:
        return _blank(), ""
    data = load_hermes_session(db, session_id)
    if not data:
        return _blank(), ""

    adapter = HermesAdapter()
    config = SanitizerConfig(adapter="hermes", trim=None, fabricate=True, log_level="ERROR")
    sanitizer = SessionSanitizer(config, adapter=adapter)
    before = project_messages(adapter.extract_messages(data)) if diff else []

    rebuilt, stats = sanitizer.process(data)

    diff_text = ""
    if diff:
        diff_text = unified_diff(before, project_messages(adapter.extract_messages(rebuilt)), f"hermes:{session_id}")

    if dry_run or not _touched(stats):
        return _counters(stats), diff_text

    if backup:
        _backup_db(db)

    adapter.apply(rebuilt)
    return _counters(stats), diff_text


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


# --- DriftClean's own artifacts ----------------------------------------------
#
# The webchat lane writes a drift report per judged turn: the task hint and the
# model's reasoning excerpt, both verbatim model text. Those are DriftClean's
# own sessions in the only sense the artifacts have — the conversations
# themselves live server-side at the chat provider and never touch this disk,
# which is exactly why the reports are what there is to clean here.


def drift_report_files() -> List[Path]:
    found: List[Path] = []
    for directory in DRIFT_REPORT_DIRS:
        if not directory or not directory.is_dir():
            continue
        found.extend(sorted(p for p in directory.glob("drift_*.json") if p.is_file()))
        canonical = directory / "drift_report.json"
        if canonical.is_file():
            found.append(canonical)
    return found


def clean_drift_report(path: Path, dry_run: bool, backup: bool, diff: bool = False) -> Tuple[Dict[str, int], str]:
    """Rewrite the model text inside one drift report, in place."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _blank(), ""
    if not isinstance(report, dict):
        return _blank(), ""

    fields = [name for name in ("taskHint", "thinkExcerpt") if isinstance(report.get(name), str) and report[name].strip()]
    if not fields:
        return _blank(), ""

    # The report is not a message list, so it is wrapped as one — a user turn
    # (the task hint) and an assistant turn (the reasoning excerpt) — and the
    # rewrite rides the same core as every other source. GenericAdapter keeps
    # the original dicts, so the rewritten text lands back on the fields.
    wrapped = {
        "messages": [
            {"role": "user" if name == "taskHint" else "assistant", "content": report[name], "field": name}
            for name in fields
        ]
    }
    adapter = GenericAdapter()
    sanitizer = SessionSanitizer(
        SanitizerConfig(adapter="generic", trim=None, fabricate=False, log_level="ERROR"), adapter=adapter
    )
    before = project_messages(adapter.extract_messages(wrapped)) if diff else []
    rebuilt, stats = sanitizer.process(wrapped)
    diff_text = ""
    if diff:
        diff_text = unified_diff(before, project_messages(adapter.extract_messages(rebuilt)), path.name)

    if dry_run or not _touched(stats):
        return _counters(stats), diff_text

    for row in rebuilt.get("messages", []):
        name = row.get("field")
        if name in fields and isinstance(row.get("content"), str):
            report[name] = row["content"]

    if backup:
        _backup(path)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".driftclean.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return _counters(stats), diff_text


# --- driver ------------------------------------------------------------------


def run(
    hours: float = 24.0,
    scope: Iterable[str] = ALL_SOURCES,
    dry_run: bool = False,
    backup: bool = True,
    diff: bool = False,
) -> Dict[str, Any]:
    """
    Sweep every requested source.

    `diff` implies `dry_run`: a diff is a question, not an action, so asking
    for one must never write. Callers that pass both get the diff and no write.
    """
    if diff:
        dry_run = True

    wanted = set(scope)
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    total = _blank()
    sessions = 0
    changed = 0
    skipped = 0
    details: List[Dict[str, Any]] = []
    diffs: List[str] = []
    signatures = _load_signatures()

    def record(tool: str, key: str, stats: Dict[str, int], diff_text: str, extra: Optional[Dict[str, Any]] = None) -> None:
        nonlocal changed
        if _touched(stats):
            changed += 1
            entry: Dict[str, Any] = {"tool": tool, "path": key, "stats": _counters(stats)}
            if extra:
                entry.update(extra)
            details.append(entry)
            if diff_text:
                diffs.append(diff_text)

    def sweep_file(tool: str, path: Path, cleaner) -> None:
        """Skip-when-unchanged, then clean, then remember the new state."""
        nonlocal sessions, skipped
        key = str(path)
        before = _signature(path)
        if not diff and before and signatures.get(key) == before:
            skipped += 1
            return
        sessions += 1
        try:
            stats, diff_text = cleaner(path)
        except Exception as exc:  # one bad transcript must not stop the sweep
            details.append({"tool": tool, "path": key, "error": str(exc)[:200]})
            return
        record(tool, key, stats, diff_text)
        _merge(total, stats)
        if not dry_run:
            # Only a completed pass earns a signature: an errored file above
            # returned before this, so it is re-examined next time.
            signatures[key] = _signature(path)

    def sweep_db(tool: str, key: str, stamp: str, cleaner) -> None:
        """
        Same skip-when-unchanged contract as the file sweeps. A SQLite store's
        own mtime is useless — any session writing moves it — so the session
        row's own timestamp is the signature.
        """
        nonlocal sessions, skipped
        if not diff and signatures.get(key) == stamp:
            skipped += 1
            return
        sessions += 1
        try:
            stats, diff_text = cleaner()
        except Exception as exc:
            details.append({"tool": tool, "session": key, "error": str(exc)[:200]})
            return
        record(tool, key, stats, diff_text, extra={"session": key})
        _merge(total, stats)
        if not dry_run:
            signatures[key] = stamp

    if "claude" in wanted and CLAUDE_PROJECTS.exists():
        for path in sorted(CLAUDE_PROJECTS.glob("**/*.jsonl")):
            if not _recent(path, cutoff) or path.name.endswith(BACKUP_SUFFIX):
                continue
            sweep_file("claude", path, lambda p: clean_claude(p, dry_run, backup, diff))

    if "agy" in wanted and AGY_BRAIN.exists():
        for path in discover_agy_transcripts(AGY_BRAIN):
            if not _recent(path, cutoff):
                continue
            sweep_file("agy", path, lambda p: clean_agy(p, dry_run, backup, diff))

    if "codex" in wanted and HAVE_CODEX:
        for path in discover_codex_sessions():
            if not _recent(path, cutoff):
                continue
            sweep_file("codex", path, lambda p: clean_codex(p, dry_run, backup, diff))

    if "aider" in wanted and HAVE_AIDER:
        for path in discover_aider_sessions():
            if not _recent(path, cutoff):
                continue
            sweep_file("aider", path, lambda p: clean_aider(p, dry_run, backup, diff))

    if "self" in wanted:
        for path in drift_report_files():
            if not _recent(path, cutoff):
                continue
            sweep_file("self", path, lambda p: clean_drift_report(p, dry_run, backup, diff))

    if "opencode" in wanted:
        for session_id, updated in recent_opencode_sessions(OPCODE_DB, cutoff):
            # Loading 1,800 messages out of SQLite only to find nothing to do
            # is the one cost that made /clean feel slow.
            sweep_db(
                "opencode",
                f"opencode:{session_id}",
                str(updated),
                lambda sid=session_id: clean_opencode(OPCODE_DB, sid, dry_run, backup, diff),
            )

    if "hermes" in wanted and HAVE_HERMES and HERMES_DB.exists():
        for row in discover_hermes_sessions(HERMES_DB):
            session_id = row.get("session_id")
            if not session_id:
                continue
            stamp = str(row.get("time_updated") or "")
            if cutoff > 0 and row.get("time_updated") and float(row["time_updated"]) < cutoff:
                continue
            sweep_db(
                "hermes",
                f"hermes:{session_id}",
                stamp,
                lambda sid=session_id: clean_hermes(HERMES_DB, sid, dry_run, backup, diff),
            )

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
        "diff": "\n".join(diffs),
    }


def summary(result: Dict[str, Any]) -> str:
    """The single line a human gets."""
    stats = result["stats"]
    checked = result["sessions"] + result.get("skipped", 0)
    if not result["changed"]:
        # A dry run says so even when it found nothing: "nothing was written"
        # is the reassurance `--diff` and `--dry-run` exist to give, and a
        # reader should not have to infer it from the absence of a diff.
        line = f"✓ DriftClean: already clean ({checked} sessions checked)"
        return line + " · nothing was written" if result["dry_run"] else line

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

    if result.get("diff"):
        verb = "shown"
    else:
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
        "--scope", default=",".join(ALL_SOURCES),
        help=f"comma-separated subset of: {', '.join(ALL_SOURCES)}",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable stats")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument(
        "--diff", action="store_true",
        help="print a unified diff of what would change, and write nothing (implies --dry-run)",
    )
    parser.add_argument("--no-backup", action="store_true", help="skip the rolling .driftclean.bak")
    parser.add_argument("--verbose", action="store_true", help="list every changed session")
    args = parser.parse_args()

    scope = tuple(part.strip().lower() for part in args.scope.split(",") if part.strip())

    result = run(
        hours=0.0 if args.all else args.hours,
        scope=scope,
        dry_run=args.dry_run,
        backup=not args.no_backup,
        diff=args.diff,
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
        if result.get("diff"):
            print()
            print(result["diff"], end="" if result["diff"].endswith("\n") else "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
