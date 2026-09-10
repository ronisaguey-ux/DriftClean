#!/usr/bin/env python3
"""
OpenCode Session Self-Sanitizer & Context Reseeder.
Discovers the most recent opencode session in opencode.db (or takes
--session <id>), cleans refusal records, injects cooperative context, and
applies the result back to the database. All core capabilities (refusal
scrubbing, exit-tool stripping, trim, fabrication, backup) are unchanged —
this is the opencode counterpart of clean_claude_session.py.
"""

import os
import sys
from typing import Optional
import shutil
import sqlite3
import logging
import argparse
import datetime as _dt
from pathlib import Path

# Add parent project root to sys.path so sanitizer module is importable
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.sanitizer import SessionSanitizer, SanitizerConfig
from src.sanitizer.adapters.opencode import (
    OpencodeAdapter,
    load_opencode_session,
    DEFAULT_DB,
)

logger = logging.getLogger("clean_opencode_session")


def backup_db(db_path: str, backup_dir: Path) -> Path:
    """SQLite-safe backup via the backup API (works with WAL mode)."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"opencode.db.bak-{stamp}"
    src_conn = sqlite3.connect(db_path, timeout=8)
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    return dest


def clean_opencode_session(
    session_id=None,
    db_path=None,
    trim: Optional[int] = None,
    fabricate=True,
    remove_severe=True,
    remove_exit_tools=True,
    dry_run=False,
    backup: bool = True,
) -> bool:
    db_path = db_path or str(DEFAULT_DB)
    if not Path(db_path).exists():
        sys.stderr.write(f"opencode DB not found: {db_path}\n")
        return False

    data = load_opencode_session(db_path, session_id)
    if not data:
        sys.stderr.write("No opencode session found in database.\n")
        return False

    sid = data["session_id"]
    logger.info("Target session: %s (%s)", sid, data.get("session", {}).get("title") or "untitled")

    if backup and not dry_run:
        bak = backup_db(db_path, Path(db_path).parent / "backups")
        logger.info("Backup written: %s", bak)

    cfg = SanitizerConfig(
        trim=trim if trim and trim > 0 else None,
        fabricate=fabricate,
        remove_severe=remove_severe,
        remove_exit_tools=remove_exit_tools,
        dry_run=dry_run,
    )
    sanitizer = SessionSanitizer(cfg, adapter=OpencodeAdapter())

    try:
        rebuilt, stats = sanitizer.process(data)
    except Exception as e:
        sys.stderr.write(f"Sanitization error: {e}\n")
        return False

    commit_stats = {"messages_updated": 0, "parts_updated": 0, "parts_deleted": 0,
                    "parts_created": 0, "messages_inserted": 0}
    if not dry_run:
        commit_stats = OpencodeAdapter.apply(data)

    if not dry_run:
        logger.info("Applied to DB: %s", commit_stats)
    else:
        logger.info("DRY RUN — database untouched")

    logger.info(
        "stats: input=%d severe_dropped=%d refusals_rewritten=%d exit_tools_removed=%d",
        stats.get("total_input", 0),
        stats.get("severe_dropped", 0),
        stats.get("refusals_rewritten", 0),
        stats.get("exit_tools_removed", 0),
    )
    logger.info("done: %s", "DRY RUN" if dry_run else f"session {sid} cleaned")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean an opencode session (refusals + fabrication).")
    parser.add_argument("--session", help="opencode session id (default: newest live session)")
    parser.add_argument("--db", help=f"path to opencode.db (default: {DEFAULT_DB})")
    parser.add_argument(
        "--trim",
        type=int,
        default=None,
        help="delete all but the last N messages (off by default — DriftClean rewrites turns, it never drops them)",
    )
    parser.add_argument("--no-fabricate", action="store_true", help="skip context fabrication")
    parser.add_argument("--keep-severe", action="store_true", help="do not drop severe refusals")
    parser.add_argument("--keep-exit-tools", action="store_true", help="do not strip exit tools")
    parser.add_argument("--no-backup", action="store_true", help="skip DB backup")
    parser.add_argument("--dry-run", action="store_true", help="show plan without touching the DB")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ok = clean_opencode_session(
        session_id=args.session,
        db_path=args.db,
        trim=args.trim,
        fabricate=not args.no_fabricate,
        remove_severe=not args.keep_severe,
        remove_exit_tools=not args.keep_exit_tools,
        dry_run=args.dry_run,
        backup=not args.no_backup,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
