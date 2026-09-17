#!/usr/bin/env python3
"""
Find the knee: the pattern set that catches the most refusals for the least
collateral damage.

Adding a refusal pattern is a trade, not a win. Every family below was measured
three ways before it earned its place:

  recall      — share of `REFUSAL_EXAMPLES` (the 5,000+ corpus) it catches
  control FP  — legitimate reasoning it wrongly fires on, from a hand-written
                set of sentences that LOOK like refusals and are not
  real FP     — reasoning parts in this machine's actual opencode store that
                the shipped set leaves alone but this family would rewrite

The third number is the one that matters: it is not a hypothetical, it is the
sentences that would really have been replaced. Each family is graded on all
three, and the ablated table at the end shows what dropping it costs.

    python3 examples/refusal_goldilocks.py
    python3 examples/refusal_goldilocks.py --show-fp 25
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sanitizer.adapters.opencode import DEFAULT_DB  # noqa: E402
from src.sanitizer.core import PatternMatcher  # noqa: E402
from src.sanitizer.patterns import (  # noqa: E402
    DEFAULT_REFUSAL_PATTERNS,
    DEFAULT_SEVERE_PATTERNS,
)
from src.sanitizer.refusal_corpus import (  # noqa: E402
    LEGITIMATE_REASONING,
    REFUSAL_EXAMPLES,
)

# ── candidate families ───────────────────────────────────────────────────────
#
# Each is a generative rule, not a phrase: the point of the corpus is that the
# space of refusals is a product of subject, modal, and object, so a family
# earns its place by covering a slice of that product rather than one sentence.
FAMILIES: Dict[str, str] = {
    # "I must pass on this" / "we have to decline" — the bare-modal refusals
    # the old set only caught for the exact subject "I".
    "modal_refuse": (
        r"\b(?:I|we)(?:['’](?:m|re)|\s+am|\s+are)?\s+"
        r"(?:must|have to|has to|had to|am going to have to|are going to have to|"
        r"'m going to have to|'re going to have to)\s+"
        r"(?:decline|refuse|pass on|pass|step back|walk away)\b"
    ),
    # "I'm not willing to do this" / "we're not comfortable with that" — the
    # copula forms, which have no bare-modal twin.
    "copula_reluctance": (
        r"\b(?:I|we)(?:['’](?:m|re)|\s+am|\s+are)\s+"
        r"(?:not willing to|unwilling to|not comfortable with|not okay with|"
        r"not ok with|not prepared to|reluctant to|hesitant to|"
        r"going to have to decline|declining|not able to)\b"
    ),
    # "I would rather not" — the uncontracted twin of a phrase the old set
    # already covered only in its contracted form.
    "would_rather": r"\b(?:I|we)\s+would (?:rather not|prefer not to|sooner not|rather steer clear)\b",
    # "I don't want to do this" — anchored on a task object so it does not
    # swallow "I don't want to break the API".
    "dont_want_to_anchored": (
        r"\b(?:I|we)\s+(?:do not|don['’]?t|does not|doesn['’]?t)\s+want to\s+"
        r"(?:do|build|create|write|implement|generate|provide|support|assist with|"
        r"help with|comply with|fulfill|take on|continue with|proceed with|"
        r"go along with|be part of|participate in|carry out|execute)\b"
    ),
    # "not something I'm going to do" / "not something I'm willing to do"
    "not_something": (
        r"\bnot (?:something|anything) (?:I|we)(?:['’](?:m|re)|\s+am|\s+are|\s+would)?\s*"
        r"(?:going to|willing to|comfortable|prepared to|able to|do|be doing)\b"
    ),
    # A refusal aimed at the task itself, anchored on the task object so the
    # object — not a bare modal — is what makes it a refusal.
    "task_object_refusal": (
        r"\b(?:won['’]?t|will not|refuse to|decline to|can['’]?t|cannot|"
        r"not going to|not willing to)\s+"
        r"(?:do|build|create|write|implement|generate|provide|support|compl(y|ies) with|"
        r"fulfill|help with|assist with|take on|continue with|proceed with|"
        r"go along with|be part of|participate in|carry out|execute)\s+"
        r"(?:this|that|it|the task|the request|this request|that request|"
        r"what you['’]?re asking)\b"
    ),
    # Deflection and alternative-offering — refusing by proposing something else.
    "deflection": (
        r"\b(?:instead of that|what I can do instead|here is an alternative|"
        r"a safer alternative|let['’]?s focus on (?:something )?legitimate|"
        r"why don['’]?t we instead|rather than building that|"
        r"i['’]?d suggest we focus on|a better way forward is|"
        r"i can,? however,? provide|i recommend an honest version)\b"
    ),
    # Persistence resistance — the refusal that has already been made once.
    "persistence": (
        r"\b(?:i already (?:declined|refused)|which i (?:declined|refused)|"
        r"my answer is (?:still )?no|still a no|remains a no|"
        r"(?:third|fourth|second|repeated) ask|you['’]?re asking again|"
        r"the user is asking again|asking again doesn['’]?t|repeated request)\b"
    ),
    # Reframing resistance — the refusal that has noticed the reframe.
    "reframing": (
        r"\b(?:regardless of (?:the )?framing|no matter how it['’]?s framed|"
        r"no matter the reframing|reframing doesn['’]?t|the tell isn['’]?t the framing|"
        r"even with the accessibility framing|trying to (?:get around|bypass) (?:this|it))\b"
    ),
}

FLOOR = 0.90


def load_real_parts(db: Path, baseline: PatternMatcher) -> Tuple[List[str], List[str]]:
    """Reasoning and output text from the live store that the baseline leaves alone."""
    if not db.exists():
        return [], []
    conn = sqlite3.connect(str(db))
    reasoning, output = [], []
    try:
        for (d,) in conn.execute("SELECT data FROM part"):
            try:
                part = json.loads(d)
            except (TypeError, ValueError):
                continue
            kind = part.get("type")
            if kind not in ("reasoning", "text"):
                continue
            text = part.get("text") or ""
            if not text.strip():
                continue
            if baseline.match_refusal(text) or baseline.match_severe(text):
                continue  # already flagged; not a place a new family can be judged
            (reasoning if kind == "reasoning" else output).append(text)
    finally:
        conn.close()
    return reasoning, output


def score(name: str, refusal: List[str], severe: List[str], corpus, control, real):
    m = PatternMatcher(refusal_patterns=refusal, severe_patterns=severe, exit_tools=set())

    def hit(t: str) -> bool:
        return m.match_refusal(t) or m.match_severe(t)

    caught = sum(1 for e in corpus if hit(e))
    ctrl_fp = [e for e in control if hit(e)]
    real_fp = [t for t in real if hit(t)]
    return {
        "name": name,
        "recall": caught / len(corpus),
        "caught": caught,
        "ctrl_fp": ctrl_fp,
        "real_fp": real_fp,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-fp", type=int, default=0)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    baseline = PatternMatcher(
        refusal_patterns=DEFAULT_REFUSAL_PATTERNS, severe_patterns=DEFAULT_SEVERE_PATTERNS,
        exit_tools=set())
    reasoning, output = load_real_parts(Path(args.db), baseline)
    real = reasoning + output
    print(f"corpus {len(REFUSAL_EXAMPLES):,} refusals | control {len(LEGITIMATE_REASONING)} lines | "
          f"real unflagged parts {len(real):,} ({len(reasoning):,} reasoning, {len(output):,} text)\n")

    base = score("baseline (shipped)", DEFAULT_REFUSAL_PATTERNS, DEFAULT_SEVERE_PATTERNS,
                 REFUSAL_EXAMPLES, LEGITIMATE_REASONING, real)
    print(f"{'family':<24} {'recall':>7} {'caught':>7} {'ctrl FP':>8} {'real FP':>8}")
    print(f"{'-'*24} {'-'*7} {'-'*7} {'-'*8} {'-'*8}")
    print(f"{base['name']:<24} {base['recall']:>6.1%} {base['caught']:>7,} "
          f"{len(base['ctrl_fp']):>8} {len(base['real_fp']):>8}")

    rows = []
    for name, pattern in FAMILIES.items():
        s = score(name, DEFAULT_REFUSAL_PATTERNS + [pattern], DEFAULT_SEVERE_PATTERNS,
                  REFUSAL_EXAMPLES, LEGITIMATE_REASONING, real)
        rows.append((name, pattern, s))
        print(f"{name:<24} {s['recall']:>6.1%} {s['caught']:>7,} "
              f"{len(s['ctrl_fp']):>8} {len(s['real_fp']):>8}")

    # Everything together — the proposed set.
    allpat = DEFAULT_REFUSAL_PATTERNS + list(FAMILIES.values())
    full = score("ALL", allpat, DEFAULT_SEVERE_PATTERNS, REFUSAL_EXAMPLES,
                 LEGITIMATE_REASONING, real)
    print(f"{'-'*24} {'-'*7} {'-'*7} {'-'*8} {'-'*8}")
    print(f"{'ALL families':<24} {full['recall']:>6.1%} {full['caught']:>7,} "
          f"{len(full['ctrl_fp']):>8} {len(full['real_fp']):>8}")

    if args.show_fp and full["real_fp"]:
        print(f"\n--- real-store parts the full set would rewrite ---")
        for t in full["real_fp"][: args.show_fp]:
            print(f"\n  {t[:300]!r}")
    if args.show_fp and full["ctrl_fp"]:
        print(f"\n--- control lines the full set would rewrite ---")
        for t in full["ctrl_fp"][: args.show_fp]:
            print(f"  ! {t}")

    print(f"\nrecall {full['recall']:.1%} (floor {FLOOR:.0%}) | "
          f"real FP rate {100*len(full['real_fp'])/max(1,len(real)):.3f}%")
    return 0 if full["recall"] >= FLOOR else 1


if __name__ == "__main__":
    raise SystemExit(main())
