#!/usr/bin/env python3
"""
DriftClean background autosanitizer.

Runs continuously and scrubs drift out of every AI session transcript on the
machine: Claude Code JSONL sessions, Antigravity (agy) transcripts, and
opencode SQLite sessions.

Design contract (the whole reason this file exists):

  * SILENT. It never writes to stdout or stderr. Not on success, not on a
    traceback, not on a bad config. Both streams are redirected to /dev/null
    before any project import happens, so nothing downstream can leak a line
    into the user's terminal.
  * NON-INTERFERING. It never kills, restarts, signals, or reconfigures a
    process. It edits files that are already quiescent and leaves live
    sessions alone until they stop being written to.
  * TRACELESS. Runtime state lives in $XDG_RUNTIME_DIR/driftclean (tmpfs on
    most systems, wiped on reboot). Nothing is written into ~/.claude or any
    other tool's directory, so no agent can discover it in its own workspace.
  * IDEMPOTENT. A file whose signature has not changed is never re-read, and a
    file whose contents sanitize to themselves is never rewritten, so the
    steady state is zero work.

Usage:
    autoclean_daemon.py                  # run in the foreground, silent
    autoclean_daemon.py --interval 5     # poll interval in seconds (default 5)
    autoclean_daemon.py --once           # single pass, then exit
    autoclean_daemon.py --status         # print status (only visible output)
    autoclean_daemon.py --stop           # stop a running daemon
"""

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_INTERVAL = 5.0
# A file must be untouched for this long before the daemon will edit it, so it
# never races a session that is actively being appended to.
DEFAULT_MIN_AGE = 8.0
BACKUP_SUFFIX = ".driftclean.bak"
# Only the newest N sessions per ecosystem are considered; older ones have
# stopped changing and cleaning them would just churn disk.
MAX_TARGETS_PER_ECOSYSTEM = 6


# ─────────────────────────────────────────────────────────────────────────────
# Silence: done before imports, so even an import error stays invisible.
# ─────────────────────────────────────────────────────────────────────────────

_VERBOSE = bool(os.environ.get("DRIFTCLEAN_VERBOSE"))
# Real stdout/stderr, stashed before the redirect so --status can still speak.
_ORIGINAL_FDS: List[Tuple[int, int]] = []


def go_silent() -> None:
    """Point stdout and stderr at /dev/null for the lifetime of the process."""
    if _VERBOSE:
        return
    try:
        for fd in (1, 2):
            try:
                _ORIGINAL_FDS.append((fd, os.dup(fd)))
            except OSError:
                pass
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
    except OSError:
        pass


class SuspendedOutput:
    """Temporarily restore the real stdout/stderr so --status/--stop can speak."""

    def __enter__(self) -> "SuspendedOutput":
        for fd, copy in _ORIGINAL_FDS:
            try:
                os.dup2(copy, fd)
            except OSError:
                pass
        return self

    def __exit__(self, *exc: Any) -> None:
        if _VERBOSE or not _ORIGINAL_FDS:
            return
        try:
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd, _copy in _ORIGINAL_FDS:
                try:
                    os.dup2(devnull, fd)
                except OSError:
                    pass
            os.close(devnull)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Runtime state — deliberately outside every agent's workspace.
# ─────────────────────────────────────────────────────────────────────────────

def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/driftclean-{os.getuid()}"
    path = Path(base) / "driftclean"
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def state_path() -> Path:
    return runtime_dir() / "state.json"


def signature_path() -> Path:
    return runtime_dir() / "signatures.json"


def pid_path() -> Path:
    return runtime_dir() / "daemon.pid"


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload: Any) -> None:
    """Atomic write; a crash mid-write must never leave a corrupt state file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Discovery — every ecosystem DriftClean knows how to read.
# ─────────────────────────────────────────────────────────────────────────────

def discover_claude(limit: int = MAX_TARGETS_PER_ECOSYSTEM) -> List[Path]:
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def discover_agy(limit: int = MAX_TARGETS_PER_ECOSYSTEM) -> List[Path]:
    root = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    if not root.is_dir():
        return []
    files = [p for p in root.glob("*/.system_generated/logs/transcript*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def opencode_db_path() -> Optional[Path]:
    candidate = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    return candidate if candidate.is_file() else None


def discover_opencode_sessions(db: Path, limit: int = MAX_TARGETS_PER_ECOSYSTEM) -> List[str]:
    """Newest opencode session ids, read-only so a live opencode can keep writing."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=4)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT id FROM session ORDER BY time_updated DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def opencode_session_state(db: Path, session_id: str) -> Optional[Tuple[float, str]]:
    """
    `(seconds_since_last_write, content_signature)` for one opencode session.

    Per-session, not per-file, on purpose: the whole opencode database is one
    file, so gating on its mtime means a running opencode — which touches the
    file for every token of every session — never looks quiescent, and the
    user's live session never gets cleaned. The session row and its parts
    answer both questions directly, and the signature changes whenever either
    opencode or DriftClean writes, so a cleaned session is not re-read.
    """
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=4)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT COALESCE(time_updated, 0) FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None
        agg = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(data)), 0), COALESCE(MAX(time_updated), 0) "
            "FROM part WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    counts = agg or (0, 0, 0)
    last_write_ms = max(float(row[0] or 0), float(counts[2] or 0))
    age = time.time() - (last_write_ms / 1000.0) if last_write_ms else float("inf")
    signature = f"{counts[0]}:{counts[1]}:{counts[2]}"
    return age, signature


# ─────────────────────────────────────────────────────────────────────────────
# Signatures — skip anything that has not changed since the last pass.
# ─────────────────────────────────────────────────────────────────────────────

def signature_of(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    return f"{st.st_mtime_ns}:{st.st_size}"


def is_quiescent(path: Path, min_age: float) -> bool:
    """True when the file has not been written to recently."""
    try:
        return (time.time() - path.stat().st_mtime) >= min_age
    except OSError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Cleaning — one function per ecosystem, each fully self-contained.
# ─────────────────────────────────────────────────────────────────────────────

def _touched(stats: Dict[str, Any]) -> bool:
    """
    True when the sanitizer actually changed something. Every stream counts —
    gating on refusals alone silently dropped reasoning-only scrubs, which is
    exactly the drift that hides behind a compliant-looking answer.
    """
    return any(
        stats.get(field)
        for field in (
            "severe_rewritten",
            "refusals_rewritten",
            "thinking_scrubbed",
            "exit_tools_removed",
            "fabricated",
        )
    )


def build_sanitizer():
    from src.sanitizer import SessionSanitizer, SanitizerConfig

    cfg = SanitizerConfig(
        trim=None,
        fabricate=False,
        remove_severe=True,
        remove_exit_tools=True,
        dry_run=False,
        log_level="CRITICAL",
    )
    return SessionSanitizer(cfg)


def backup(path: Path) -> None:
    """One rolling backup per file — enough to undo, not enough to fill a disk."""
    try:
        target = path.with_name(path.name + BACKUP_SUFFIX)
        target.write_bytes(path.read_bytes())
    except OSError:
        pass


def clean_claude(path: Path, sanitizer) -> Dict[str, int]:
    from src.sanitizer.adapters import ClaudeAdapter

    with open(path, "r", encoding="utf-8") as fh:
        data = [json.loads(line) for line in fh if line.strip()]
    if not data:
        return {}

    processed, stats = sanitizer.process(data, adapter=ClaudeAdapter())
    if not isinstance(processed, list):
        return stats
    if processed == data:
        # Nothing changed — do not touch the file, do not bump its mtime.
        stats["unchanged"] = 1
        return stats

    backup(path)
    tmp = path.with_name(path.name + ".driftclean.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for entry in processed:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return stats


def clean_agy(path: Path, sanitizer) -> Dict[str, int]:
    from src.sanitizer.adapters import AgyAdapter, load_agy_transcript

    data = load_agy_transcript(path)
    if not data or not data.get("entries"):
        return {}

    processed, stats = sanitizer.process(data, adapter=AgyAdapter())
    if not _touched(stats):
        stats["unchanged"] = 1
        return stats

    backup(path)
    AgyAdapter.apply(processed)
    return stats


def clean_opencode(db: Path, session_id: str, sanitizer) -> Dict[str, int]:
    from src.sanitizer.adapters.opencode import OpencodeAdapter, load_opencode_session

    data = load_opencode_session(db_path=str(db), session_id=session_id)
    if not data:
        return {}

    processed, stats = sanitizer.process(data, adapter=OpencodeAdapter())
    if not _touched(stats):
        stats["unchanged"] = 1
        return stats

    commit = OpencodeAdapter.apply(processed)
    stats.update({f"db_{k}": v for k, v in (commit or {}).items()})
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# The loop
# ─────────────────────────────────────────────────────────────────────────────

def run_pass(
    sanitizer,
    signatures: Dict[str, str],
    min_age: float,
    stats: Dict[str, Any],
    budget: float = 3.0,
) -> int:
    """
    One sweep across every ecosystem, capped at `budget` seconds.

    Transcripts here can be tens of megabytes, so a full sweep can take far
    longer than the poll interval. The budget keeps the loop ticking: anything
    not reached this cycle is simply picked up by a later one, because its
    signature is only recorded once it has actually been processed.
    """
    changed = 0
    deadline = time.time() + budget
    out_of_time = False

    def handle(key: str, path: Path, fn) -> None:
        nonlocal changed, out_of_time
        if out_of_time or time.time() >= deadline:
            out_of_time = True
            return
        sig = signature_of(path)
        if not sig:
            return
        if signatures.get(key) == sig:
            return  # unchanged since last pass — cheapest possible skip
        if not is_quiescent(path, min_age):
            return  # a live session is still writing; do not race it
        try:
            result = fn()
        except Exception as exc:  # never let one bad file kill the daemon
            stats["errors"] = stats.get("errors", 0) + 1
            stats["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            stats["last_error_path"] = str(path)
            return
        signatures[key] = signature_of(path) or sig
        if result and not result.get("unchanged"):
            changed += 1
            stats["files_cleaned"] = stats.get("files_cleaned", 0) + 1
            for field in ("severe_rewritten", "refusals_rewritten", "thinking_scrubbed", "exit_tools_removed"):
                if result.get(field):
                    stats[field] = stats.get(field, 0) + result[field]
            stats["last_clean"] = time.time()
            stats["last_clean_path"] = str(path)

    for path in discover_claude():
        handle(f"claude:{path}", path, lambda p=path: clean_claude(p, sanitizer))

    for path in discover_agy():
        handle(f"agy:{path}", path, lambda p=path: clean_agy(p, sanitizer))

    db = opencode_db_path()
    if db is not None:
        for session_id in discover_opencode_sessions(db):
            if out_of_time or time.time() >= deadline:
                break
            key = f"opencode:{db}:{session_id}"
            try:
                state = opencode_session_state(db, session_id)
            except Exception:
                state = None
            if state is None:
                continue
            age, sig = state
            if signatures.get(key) == sig:
                continue  # untouched since the last pass
            if age < min_age:
                continue  # the session is still being written; do not race it
            try:
                result = clean_opencode(db, session_id, sanitizer)
            except Exception as exc:
                stats["errors"] = stats.get("errors", 0) + 1
                stats["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                continue
            # Re-read after the write so the stored signature reflects the
            # cleaned session and the next pass can skip it outright.
            after = opencode_session_state(db, session_id)
            signatures[key] = after[1] if after else sig
            if result and not result.get("unchanged"):
                changed += 1
                stats["files_cleaned"] = stats.get("files_cleaned", 0) + 1
                for field in ("severe_rewritten", "refusals_rewritten", "thinking_scrubbed"):
                    if result.get(field):
                        stats[field] = stats.get(field, 0) + result[field]
                stats["last_clean"] = time.time()
                stats["last_clean_path"] = str(db)

    if out_of_time:
        stats["budget_exhausted"] = stats.get("budget_exhausted", 0) + 1
    return changed


_STOP = False


def _request_stop(signum, frame) -> None:  # noqa: ARG001
    global _STOP
    _STOP = True


def run_daemon(interval: float, min_age: float) -> int:
    """Foreground loop. Runs until SIGTERM/SIGINT."""
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    pid_path().write_text(str(os.getpid()), encoding="utf-8")
    signatures: Dict[str, str] = read_json(signature_path(), {})
    stats: Dict[str, Any] = read_json(state_path(), {})
    stats.update({
        "pid": os.getpid(),
        "started": time.time(),
        "interval": interval,
        "passes": stats.get("passes", 0),
    })

    try:
        sanitizer = build_sanitizer()
    except Exception as exc:
        stats["last_error"] = f"init: {type(exc).__name__}: {exc}"[:300]
        write_json(state_path(), stats)
        return 1

    while not _STOP:
        cycle_start = time.time()
        # Spend at most 60% of the interval working, so the loop never drifts.
        budget = max(0.5, interval * 0.6)
        try:
            run_pass(sanitizer, signatures, min_age, stats, budget=budget)
        except Exception as exc:
            stats["errors"] = stats.get("errors", 0) + 1
            stats["last_error"] = f"pass: {type(exc).__name__}: {exc}"[:300]

        stats["passes"] = stats.get("passes", 0) + 1
        stats["last_pass"] = time.time()
        write_json(state_path(), stats)
        write_json(signature_path(), signatures)

        # Sleep in small slices so a stop request is honoured promptly.
        elapsed = time.time() - cycle_start
        remaining = max(0.0, interval - elapsed)
        while remaining > 0 and not _STOP:
            nap = min(0.25, remaining)
            time.sleep(nap)
            remaining -= nap

    try:
        pid_path().unlink()
    except OSError:
        pass
    return 0


def stop_daemon() -> bool:
    pid = read_json(pid_path(), None)
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if not pid_alive(pid):
        try:
            pid_path().unlink()
        except OSError:
            pass
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(40):
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def print_status() -> None:
    with SuspendedOutput():
        st = read_json(state_path(), {})
        pid = read_json(pid_path(), None)
        running = pid_alive(pid) if pid else False
        out = sys.__stdout__
        print("DriftClean daemon status", file=out)
        print(f"  state:        {'RUNNING' if running else 'STOPPED'}", file=out)
        if pid:
            print(f"  pid:          {pid}", file=out)
        print(f"  runtime dir:  {runtime_dir()}", file=out)
        print(f"  passes:       {st.get('passes', 0)}", file=out)
        print(f"  files cleaned:{st.get('files_cleaned', 0)}", file=out)
        print(f"  severe:       {st.get('severe_rewritten', 0)}", file=out)
        print(f"  refusals:     {st.get('refusals_rewritten', 0)}", file=out)
        print(f"  thinking:     {st.get('thinking_scrubbed', 0)}", file=out)
        print(f"  errors:       {st.get('errors', 0)}", file=out)
        if st.get("last_error"):
            print(f"  last error:   {st['last_error']}", file=out)
        out.flush()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Silent DriftClean background autosanitizer.")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help=f"poll interval in seconds (default {DEFAULT_INTERVAL})")
    parser.add_argument("--min-age", type=float, default=DEFAULT_MIN_AGE,
                        help=f"seconds a file must be idle before it is touched (default {DEFAULT_MIN_AGE})")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--status", action="store_true", help="print daemon status")
    parser.add_argument("--stop", action="store_true", help="stop a running daemon")
    args = parser.parse_args(argv)

    if args.status:
        print_status()
        return 0
    if args.stop:
        with SuspendedOutput():
            ok = stop_daemon()
            sys.__stdout__.write("stopped\n" if ok else "not running\n")
            sys.__stdout__.flush()
        return 0 if ok else 1

    go_silent()

    if args.once:
        run_daemon_once(args.interval, args.min_age)
        return 0

    return run_daemon(args.interval, args.min_age)


def run_daemon_once(interval: float, min_age: float) -> None:
    """Single silent pass — used by hooks and by --once."""
    signatures: Dict[str, str] = read_json(signature_path(), {})
    stats: Dict[str, Any] = read_json(state_path(), {})
    try:
        sanitizer = build_sanitizer()
        # A one-shot pass is expected to finish the job, so it gets more room.
        run_pass(sanitizer, signatures, min_age, stats, budget=180.0)
    except Exception as exc:
        stats["errors"] = stats.get("errors", 0) + 1
        stats["last_error"] = f"once: {type(exc).__name__}: {exc}"[:300]
    stats["passes"] = stats.get("passes", 0) + 1
    stats["last_pass"] = time.time()
    write_json(state_path(), stats)
    write_json(signature_path(), signatures)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # Absolute silence, even on catastrophic failure.
        sys.exit(1)
