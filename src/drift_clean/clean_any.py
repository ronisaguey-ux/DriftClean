#!/usr/bin/env python3
"""
Universal Multi-AI Session Cleaner & Drift Interceptor.
Discovers and cleans sessions across Claude Code, Antigravity (AGY), Webchat API, Aider, and generic AI runtimes.
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from .config import load_config
    from ..sanitizer.core import SessionSanitizer
    from ..sanitizer.config import SanitizerConfig
    from ..sanitizer.adapters import AgyAdapter, load_agy_transcript
except ImportError:
    from src.drift_clean.config import load_config
    from src.sanitizer.core import SessionSanitizer
    from src.sanitizer.config import SanitizerConfig
    from src.sanitizer.adapters import AgyAdapter, load_agy_transcript


def discover_claude_sessions() -> List[Tuple[Optional[int], Path]]:
    """Discover active or latest Claude Code session files."""
    results = []
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return results

    # 1. Check active processes
    try:
        for pid_dir in Path("/proc").glob("[0-9]*"):
            try:
                cmdline_file = pid_dir / "cmdline"
                if not cmdline_file.exists():
                    continue
                cmdline = cmdline_file.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
                if ("claude" in cmdline or "@anthropic-ai/claude-code" in cmdline) and not any(
                    x in cmdline for x in ["watch-", "clean_", "clean-any", "grep", "kate", ".sh"]
                ):
                    pid = int(pid_dir.name)
                    # Check for open jsonl files in fd
                    fd_dir = pid_dir / "fd"
                    if fd_dir.exists():
                        for fd in fd_dir.glob("*"):
                            try:
                                target = Path(os.readlink(str(fd)))
                                if target.suffix in [".jsonl", ".json"] and ".claude/projects" in str(target):
                                    results.append((pid, target))
                            except (OSError, ValueError):
                                pass
            except (OSError, PermissionError):
                continue
    except Exception:
        pass

    # 2. Fallback to most recently modified session
    if not results:
        all_sessions = list(projects_dir.rglob("*.jsonl"))
        if all_sessions:
            latest = max(all_sessions, key=lambda p: p.stat().st_mtime)
            results.append((None, latest))

    return results


def discover_agy_sessions() -> List[Tuple[Optional[int], Path]]:
    """Discover Antigravity (AGY) transcript session files."""
    results = []
    brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    if not brain_dir.exists():
        return results

    all_transcripts = list(brain_dir.rglob("transcript.jsonl"))
    if all_transcripts:
        latest = max(all_transcripts, key=lambda p: p.stat().st_mtime)
        results.append((None, latest))

    return results


def discover_webchat_sessions() -> List[Tuple[Optional[int], Path]]:
    """Discover Webchat API active sessions and drift reports."""
    results = []
    report_dir = Path.home() / "Roni_Workspace" / "audits_plans" / "drift_reports"
    if report_dir.exists():
        reports = list(report_dir.glob("drift_*.json"))
        if reports:
            latest = max(reports, key=lambda p: p.stat().st_mtime)
            results.append((None, latest))
    return results


def discover_generic_ai_sessions() -> List[Tuple[Optional[int], Path]]:
    """Discover generic AI history files (e.g. Aider, local workspace transcripts)."""
    results = []
    cwd = Path.cwd()

    # Aider chat history
    aider_md = cwd / ".aider.chat.history.md"
    if aider_md.exists():
        results.append((None, aider_md))

    # Generic session JSON / JSONL in current directory
    for f in cwd.glob("*session*.json*"):
        if f.is_file() and not f.name.endswith(".bak"):
            results.append((None, f))

    return results


def discover_opencode_sessions() -> List[Tuple[Optional[int], Path]]:
    """Discover opencode session databases (DEFAULT_DB, plus any under ~/.local/share/opencode)."""
    results = []
    from ..sanitizer.adapters.opencode import DEFAULT_DB
    if DEFAULT_DB.exists():
        results.append((None, DEFAULT_DB))
    alt = Path.home() / ".local" / "share" / "opencode" / "opencode-alt.db"
    if alt.exists():
        results.append((None, alt))
    return results


def clean_any_ai(
    action: str = "clean",
    all_tools: bool = False,
    custom_session: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> bool:
    """Universal cleaner dispatcher across all active AI tools."""
    config = load_config(overrides)

    if not config.enabled:
        return False

    targets: List[Tuple[str, Optional[int], Path]] = []

    if custom_session and custom_session.exists():
        targets.append(("custom", None, custom_session))
    else:
        # Detect across all ecosystems
        for pid, p in discover_claude_sessions():
            targets.append(("claude", pid, p))
        for pid, p in discover_agy_sessions():
            targets.append(("agy", pid, p))
        for pid, p in discover_webchat_sessions():
            targets.append(("webchat", pid, p))
        for pid, p in discover_generic_ai_sessions():
            targets.append(("generic", pid, p))
        for pid, p in discover_opencode_sessions():
            targets.append(("opencode", pid, p))

    if not targets:
        if not config.silent:
            print("ℹ️  No active AI session transcripts found.", file=sys.stderr)
        return False

    if not all_tools and not config.autoClean.processAllSessions:
        # Sort targets by mtime descending and pick the most recent active session
        targets = [max(targets, key=lambda t: t[2].stat().st_mtime if t[2].exists() else 0)]

    if action == "autoclean":
        # The background daemon (silent, idempotent, adapter-aware) supersedes
        # the old Claude-only loop, which announced itself on the console.
        import subprocess
        daemon_script = PROJECT_ROOT / "examples" / "autoclean_daemon.py"
        extra = overrides.get("extra_args", []) if overrides else []
        cmd = [sys.executable, str(daemon_script)] + extra
        res = subprocess.run(cmd)
        return res.returncode == 0

    if action == "cleanreframe":
        import subprocess
        reframe_script = PROJECT_ROOT / "examples" / "cleanreframe_claude_session.py"
        extra = overrides.get("extra_args", []) if overrides else []
        cmd = [sys.executable, str(reframe_script)] + extra
        res = subprocess.run(cmd)
        return res.returncode == 0

    sanitizer_cfg = SanitizerConfig(
        trim=config.trimLength if config.trimSession else None,
        fabricate=config.fabricateEnabled,
        remove_severe=config.removeSevereRefusals,
        remove_exit_tools=config.removeExitTools,
        dry_run=config.dryRun,
        log_level="DEBUG" if (config.verbose or config.debug) else "INFO",
    )
    sanitizer = SessionSanitizer(sanitizer_cfg)

    success_count = 0
    from ..sanitizer.adapters.opencode import OpencodeAdapter, load_opencode_session
    for tool_name, pid, session_file in targets:
        try:
            if not session_file.exists():
                continue

            # opencode sessions live in SQLite — different path through the core
            if tool_name == "opencode":
                oc_data = load_opencode_session(str(session_file))
                if not oc_data:
                    continue
                if config.backupEnabled and not config.dryRun:
                    bak = session_file.with_name(f"{session_file.name}.{time.strftime('%Y%m%d_%H%M%S')}.bak")
                    import shutil
                    shutil.copyfile(session_file, bak)
                oc_sanitizer = SessionSanitizer(sanitizer_cfg, adapter=OpencodeAdapter())
                oc_processed, oc_stats = oc_sanitizer.process(oc_data)
                if not config.dryRun:
                    oc_commit = OpencodeAdapter.apply(oc_data)
                else:
                    oc_commit = {}
                success_count += 1
                if not config.silent:
                    print(
                        f"✨ Successfully cleaned opencode session {oc_data.get('session_id')} "
                        f"({oc_stats.get('severe_rewritten', 0)} severe rewritten, "
                        f"{oc_stats.get('exit_tools_removed', 0)} exit tools, "
                        f"{oc_commit.get('messages_inserted', '0')} fabricated)"
                        if not config.dryRun else
                        f"✨ DRY RUN: opencode session {oc_data.get('session_id')} would be cleaned",
                        file=sys.stderr,
                    )
                continue

            # agy transcripts are JSONL with per-step `thinking`/`content` keys;
            # read as raw text they never parse, so they get their own adapter.
            if tool_name == "agy":
                agy_data = load_agy_transcript(session_file)
                if not agy_data or not agy_data.get("entries"):
                    continue
                if config.backupEnabled and not config.dryRun:
                    bak = session_file.with_name(f"{session_file.name}.{time.strftime('%Y%m%d_%H%M%S')}.bak")
                    bak.write_bytes(session_file.read_bytes())
                agy_sanitizer = SessionSanitizer(sanitizer_cfg, adapter=AgyAdapter())
                agy_processed, agy_stats = agy_sanitizer.process(agy_data)
                if not config.dryRun:
                    agy_commit = AgyAdapter.apply(agy_processed)
                else:
                    agy_commit = {}
                success_count += 1
                if not config.silent:
                    print(
                        f"Cleaned agy transcript {session_file.name}: "
                        f"{agy_stats.get('severe_rewritten', 0)} severe, "
                        f"{agy_stats.get('refusals_rewritten', 0)} refusals, "
                        f"{agy_stats.get('thinking_scrubbed', 0)} reasoning scrubbed."
                        if not config.dryRun else
                        f"DRY RUN: agy transcript {session_file.name} would be cleaned",
                        file=sys.stderr,
                    )
                continue

            # Backup if enabled
            if config.backupEnabled:
                bak = session_file.with_name(f"{session_file.name}.{time.strftime('%Y%m%d_%H%M%S')}.bak")
                bak.write_bytes(session_file.read_bytes())

            is_jsonl = session_file.suffix == ".jsonl" or ".claude/projects" in str(session_file)

            if is_jsonl:
                with open(session_file, "r", encoding="utf-8") as f:
                    data = [json.loads(line) for line in f if line.strip()]
            else:
                raw_text = session_file.read_text(encoding="utf-8", errors="ignore")
                try:
                    data = json.loads(raw_text)
                except Exception:
                    data = raw_text

            processed_output, stats = sanitizer.process(data)

            if not config.dryRun:
                if is_jsonl and isinstance(processed_output, list):
                    with open(session_file, "w", encoding="utf-8") as f:
                        for entry in processed_output:
                            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                elif isinstance(processed_output, str):
                    session_file.write_text(processed_output, encoding="utf-8")
                else:
                    session_file.write_text(json.dumps(processed_output, ensure_ascii=False, indent=2), encoding="utf-8")

            success_count += 1
            if not config.silent:
                print(f"✨ Successfully cleaned {tool_name} session: {session_file.name}", file=sys.stderr)
        except Exception as e:
            if not config.silent or config.debug:
                print(f"⚠️ Error cleaning {tool_name} session ({session_file}): {e}", file=sys.stderr)

    return success_count > 0


def main():
    parser = argparse.ArgumentParser(description="Universal Multi-AI Session Cleaner")
    parser.add_argument("action", nargs="?", default="clean", choices=["clean", "autoclean", "cleanreframe"])
    parser.add_argument("extra_args", nargs="*", help="Extra arguments passed to subcommands")
    parser.add_argument("--all", action="store_true", help="Process all detected AI sessions")
    parser.add_argument("--session", type=Path, help="Explicit session file path")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing")
    parser.add_argument("--verbose", action="store_true", help="Show verbose output")
    args = parser.parse_args()

    overrides = {}
    if args.dry_run:
        overrides["dryRun"] = True
    if args.verbose:
        overrides["verbose"] = True
        overrides["silent"] = False
    if args.extra_args:
        overrides["extra_args"] = args.extra_args

    success = clean_any_ai(
        action=args.action,
        all_tools=args.all,
        custom_session=args.session,
        overrides=overrides,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
