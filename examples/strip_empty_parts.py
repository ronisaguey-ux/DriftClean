#!/usr/bin/env python3
"""
Strip the empty text parts an older DriftClean left behind in opencode.

A previous version of the opencode adapter manufactured a text part for every
message it saw that had no text — compaction boundaries, tool-only turns,
aborted requests. 1,700 of them accumulated in one session: contentless parts
that render as blank turns and are re-read on every load. The bug is fixed, but
the rows it already wrote are still there.

DriftClean itself will not remove them: its contract is that a clean rewrites
turns and never drops them, and that contract is worth more than this cleanup.
So the removal is a separate, explicit operation — which is what this is.

It is deliberately narrow. A part is removed only when ALL of these hold:

  * it belongs to the named session,
  * its type is `text`,
  * its text is empty or whitespace,

and it takes a normal DriftClean snapshot first, so `restore_session.py --last`
undoes it in one command.

    strip_empty_parts.py --session ses_...            # dry run, prints the plan
    strip_empty_parts.py --session ses_... --apply    # snapshot, then delete

Restart the serve afterwards so the TUI reads the pruned transcript:

    systemctl --user restart opencode-serve.service
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sanitizer.adapters.opencode import DEFAULT_DB  # noqa: E402
from src.sanitizer.backup import snapshot  # noqa: E402


def is_empty_text(data: str) -> bool:
    try:
        part = json.loads(data)
    except ValueError:
        return False
    return part.get("type") == "text" and not (part.get("text") or "").strip()


def find_empty(conn: sqlite3.Connection, session_id: str):
    """The empty text parts, with what each message would keep without them."""
    empty = [
        (pid, mid)
        for pid, mid, data in conn.execute(
            "SELECT id, message_id, data FROM part WHERE session_id = ?", (session_id,)
        )
        if is_empty_text(data)
    ]
    if not empty:
        return empty, 0, 0
    doomed = {pid for pid, _ in empty}
    keep, orphan = 0, 0
    seen = set()
    for pid, mid in empty:
        if mid in seen:
            continue
        seen.add(mid)
        others = conn.execute(
            "SELECT id FROM part WHERE message_id = ?", (mid,)
        ).fetchall()
        if any(o[0] not in doomed for o in others):
            keep += 1
        else:
            orphan += 1
    return empty, keep, orphan


def main() -> int:
    ap = argparse.ArgumentParser(description="Remove DriftClean's leftover empty text parts.")
    ap.add_argument("--session", required=True, help="opencode session id, e.g. ses_...")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="path to opencode.db")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args()

    conn = sqlite3.connect(str(args.db), timeout=15)
    try:
        # Foreign keys are off by default in SQLite, so nothing cascades; the
        # part rows are deleted directly and nothing else is touched.
        empty, keep, orphan = find_empty(conn, args.session)
        if not empty:
            print(f"{args.session}: no empty text parts — nothing to strip")
            return 0

        print(f"{args.session}")
        print(f"  empty text parts : {len(empty):,}")
        print(f"  messages affected: {len({m for _, m in empty}):,}")
        print(f"    still keep >=1 part : {keep:,}")
        print(f"    left with 0 parts   : {orphan:,}  (opencode writes partless turns too)")

        if not args.apply:
            print("\ndry run — nothing written. Re-run with --apply to remove them.")
            return 0

        snap = snapshot(
            conn, args.session, args.db, label=f"pre-strip-empty-parts ({len(empty)})"
        )
        if snap:
            print(f"\nsnapshot: {snap}")
            print(f"  undo with: restore_session.py --session {args.session} --last")
        else:
            print("\nWARNING: no snapshot was written; continuing anyway (the rows are junk)")

        conn.executemany("DELETE FROM part WHERE id = ?", [(p,) for p, _ in empty])
        conn.commit()

        left = conn.execute(
            "SELECT COUNT(*) FROM part WHERE session_id = ?", (args.session,)
        ).fetchone()[0]
        print(f"\ndeleted {len(empty):,} parts; session now holds {left:,}")
        print("restart the serve to see it:  systemctl --user restart opencode-serve.service")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
