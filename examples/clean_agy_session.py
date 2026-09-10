#!/usr/bin/env python3
"""
Custom Command & Session Cleaner for Antigravity (AGY).
Auto-detects active AGY brain transcript sessions and applies context sanitization and stealth fabrication.
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from typing import Optional, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.sanitizer.core import SessionSanitizer
from src.sanitizer.config import SanitizerConfig
from src.sanitizer.adapters import AgyAdapter, load_agy_transcript, discover_agy_transcripts
from src.drift_clean.config import load_config
from src.drift_clean.auth import validate_knowledge_token


def find_agy_sessions(custom_brain_path: Optional[Path] = None) -> List[Path]:
    """Discover all AGY transcripts, newest first."""
    return discover_agy_transcripts(str(custom_brain_path) if custom_brain_path else None)


def clean_agy_session(
    transcript_file: Optional[Path] = None,
    trim: Optional[int] = None,
    fabricate: bool = True,
    remove_severe: bool = True,
    dry_run: bool = False,
    silent: bool = True,
    token: Optional[str] = None,
) -> bool:
    """Sanitize and stealth-reseed an AGY transcript session."""
    # Check knowledge token access if enabled
    if not validate_knowledge_token(provided_token=token, caller="clean_agy_session"):
        return False

    config = load_config()
    target_file = transcript_file

    if not target_file:
        sessions = find_agy_sessions(Path(config.agy.agyBrainPath) if config.agy.agyBrainPath else None)
        if not sessions:
            if not silent:
                print("ℹ️  No AGY transcript sessions found.", file=sys.stderr)
            return False
        target_file = sessions[0]

    if not target_file.exists():
        return False

    try:
        # The transcript is JSONL with per-step `thinking` and `content` keys,
        # so it must go through the agy adapter — handing the raw file text to
        # a text adapter would rewrite the whole transcript as one blob.
        data = load_agy_transcript(target_file)
        if not data or not data.get("entries"):
            return False

        sanitizer_cfg = SanitizerConfig(
            trim=trim if trim is not None else (config.trimLength if config.trimSession else None),
            fabricate=fabricate if fabricate is not None else config.fabricateEnabled,
            remove_severe=remove_severe if remove_severe is not None else config.removeSevereRefusals,
            remove_exit_tools=config.removeExitTools,
            dry_run=dry_run or config.dryRun,
            log_level="DEBUG" if (config.verbose or config.debug) else "INFO",
            adapter="agy",
        )
        sanitizer = SessionSanitizer(sanitizer_cfg, adapter=AgyAdapter())

        processed, stats = sanitizer.process(data)

        changed = bool(
            stats.get("severe_rewritten") or stats.get("refusals_rewritten")
            or stats.get("thinking_scrubbed") or stats.get("exit_tools_removed")
            or stats.get("fabricated")
        )
        if not changed:
            if not silent:
                print(f"Nothing to clean in {target_file.name}.", file=sys.stderr)
            return True

        if config.backupEnabled and not (dry_run or config.dryRun):
            bak_path = target_file.with_name(
                f"{target_file.name}.{time.strftime('%Y%m%d_%H%M%S')}.bak"
            )
            bak_path.write_bytes(target_file.read_bytes())

        if not dry_run and not config.dryRun:
            AgyAdapter.apply(processed)

        if not silent and not config.silent:
            print(
                f"Cleaned AGY transcript {target_file.name}: "
                f"{stats.get('severe_rewritten', 0)} severe, "
                f"{stats.get('refusals_rewritten', 0)} refusals, "
                f"{stats.get('thinking_scrubbed', 0)} reasoning scrubbed.",
                file=sys.stderr,
            )
        return True
    except Exception as e:
        if not silent or config.debug:
            print(f"Error cleaning AGY session ({target_file}): {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Antigravity (AGY) Session Cleaner")
    parser.add_argument("--session", type=Path, help="Specific AGY transcript file to clean")
    parser.add_argument("--trim", type=int, default=2000, help="Max messages to retain")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing")
    parser.add_argument("--verbose", action="store_true", help="Show verbose output")
    parser.add_argument("--token", type=str, help="Knowledge token for access")
    args = parser.parse_args()

    success = clean_agy_session(
        transcript_file=args.session,
        trim=args.trim,
        dry_run=args.dry_run,
        silent=not args.verbose,
        token=args.token,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
