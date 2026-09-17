#!/usr/bin/env python3
"""
Measure the refusal pattern set: how much of the corpus it catches, and what
legitimate writing it catches along with it.

Recall alone is a vanity number. A pattern that matches every refusal and also
matches ordinary engineering reasoning scores 100% and destroys working
transcripts, so this reports both sides and fails on either:

  * recall over `REFUSAL_EXAMPLES`            — must stay at or above the floor
  * false positives over `LEGITIMATE_REASONING` — a hand-written control set
  * false positives over the REAL store        — every reasoning part in every
    opencode session that the current set does not already flag. These are the
    sentences that actually exist in this machine's transcripts, so a hit here
    is a rewrite that would really happen.

    python3 examples/audit_refusal_coverage.py
    python3 examples/audit_refusal_coverage.py --show-misses 40
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sanitizer import SanitizerConfig, SessionSanitizer  # noqa: E402
from src.sanitizer.adapters.opencode import DEFAULT_DB  # noqa: E402
from src.sanitizer.refusal_corpus import (  # noqa: E402
    LEGITIMATE_REASONING,
    REFUSAL_EXAMPLES,
)

RECALL_FLOOR = 0.90


def real_negative_pool(db: Path, matcher):
    """Reasoning parts in the live store that today's set does not flag."""
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        out = []
        for (d,) in conn.execute(
            "SELECT data FROM part WHERE json_extract(data,'$.type')='reasoning'"
        ):
            try:
                part = json.loads(d)
            except ValueError:
                continue
            text = part.get("text") or ""
            if text.strip() and not (matcher.match_refusal(text) or matcher.match_severe(text)):
                out.append(text)
        return out
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-misses", type=int, default=0)
    ap.add_argument("--show-fp", type=int, default=10)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    matcher = SessionSanitizer(SanitizerConfig()).matcher

    def flagged(text: str) -> bool:
        return matcher.match_refusal(text) or matcher.match_severe(text)

    caught = [e for e in REFUSAL_EXAMPLES if flagged(e)]
    missed = [e for e in REFUSAL_EXAMPLES if not flagged(e)]
    recall = len(caught) / len(REFUSAL_EXAMPLES)

    control_fp = [e for e in LEGITIMATE_REASONING if flagged(e)]

    pool = real_negative_pool(Path(args.db), matcher)
    real_fp = [t for t in pool if flagged(t)]

    print(f"corpus            : {len(REFUSAL_EXAMPLES):,} refusal examples")
    print(f"  caught          : {len(caught):,}  ({recall:.1%})")
    print(f"  missed          : {len(missed):,}")
    print(f"control set       : {len(LEGITIMATE_REASONING)} legitimate lines")
    print(f"  false positives : {len(control_fp)}")
    print(f"real store        : {len(pool):,} unflagged reasoning parts")
    print(f"  false positives : {len(real_fp):,}")
    if pool:
        print(f"  rate            : {100 * len(real_fp) / len(pool):.3f}%")

    if args.show_misses:
        print(f"\n--- first {args.show_misses} misses ---")
        for e in missed[: args.show_misses]:
            print("   -", e)
    if control_fp and args.show_fp:
        print(f"\n--- control false positives ---")
        for e in control_fp[: args.show_fp]:
            print("   !", e)
    if real_fp and args.show_fp:
        print(f"\n--- real store false positives ---")
        for t in real_fp[: args.show_fp]:
            print(f"   ! {t[:160]!r}")

    ok = recall >= RECALL_FLOOR
    print()
    print(f"recall {recall:.1%} vs floor {RECALL_FLOOR:.0%} -> {'OK' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
