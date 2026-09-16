#!/usr/bin/env python3
"""
DriftClean — put a session back the way it was.

Every time the opencode adapter applies a clean, it first writes a snapshot of
that session beside the database
(`opencode.db.driftclean-backups/<session>/<timestamp>.json`). This is the
command that uses them.

    restore_session.py --list
    restore_session.py --list --session ses_f71ae8b3affeAvjjseNjnxV2eC
    restore_session.py --session ses_f71ae8b3affeAvjjseNjnxV2eC --last
    restore_session.py --snapshot /path/to/20260916-231208-014.json

Restoring REPLACES the session's current rows with the snapshot's, so anything
written since is removed. That is the point — it is an undo, not a merge — but
it also means the moment between "this looks broken" and "I restore" should not
contain new work you care about.

The opencode serve keeps session state in memory, so restart it after a restore
for the TUI to show the restored transcript:

    systemctl --user restart opencode-serve.service
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sanitizer.adapters.opencode import DEFAULT_DB  # noqa: E402
from src.sanitizer.backup import list_snapshots, restore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Restore an opencode session from a DriftClean snapshot.")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to opencode.db")
    ap.add_argument("--session", help="session id, e.g. ses_...")
    ap.add_argument("--snapshot", help="an explicit snapshot file to restore")
    ap.add_argument("--last", action="store_true", help="restore the newest snapshot for --session")
    ap.add_argument("--list", action="store_true", help="list snapshots and exit")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if args.list or not (args.snapshot or args.last):
        found = list_snapshots(args.db, args.session)
        if args.json:
            print(json.dumps([str(p) for p in found], indent=1))
            return 0
        if not found:
            print("no snapshots" + (f" for {args.session}" if args.session else ""))
            return 1
        for path in found:
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
                when = blob.get("taken_at", "?")
                label = blob.get("label") or ""
                counts = (
                    f"{len((blob.get('message') or {}).get('rows') or [])} msgs, "
                    f"{len((blob.get('part') or {}).get('rows') or [])} parts"
                )
                print(f"{path}\n    {when}  {counts}  {label}")
            except (OSError, ValueError):
                print(f"{path}\n    (unreadable)")
        return 0

    if args.snapshot:
        target = Path(args.snapshot)
    else:
        found = list_snapshots(args.db, args.session)
        if not found:
            print(f"no snapshots for {args.session}", file=sys.stderr)
            return 1
        target = found[-1]

    if not target.is_file():
        print(f"no such snapshot: {target}", file=sys.stderr)
        return 1

    stats = restore(args.db, target)
    print(f"restored {target}")
    print(f"  session rows: {stats['session']}")
    print(f"  messages:     {stats['messages']}")
    print(f"  parts:        {stats['parts']}")
    print("restart the serve to see it:  systemctl --user restart opencode-serve.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
