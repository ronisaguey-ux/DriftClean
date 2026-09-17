#!/usr/bin/env python3
"""
Prove DriftClean eradicates refusals in the reasoning stream and lands a
replacement block — end to end, through the real opencode adapter.

The unit tests assert the refusal is *gone*; none of them assert that a
compliance block actually took its place, that the row is still a `reasoning`
part, or that the payload key is the one opencode reads. A regression that
wrote the replacement into a text part, flipped the part type, or dropped it
on the floor would leave every existing assertion green.

So this builds a scratch opencode.db, seeds the cases that matter, runs the
real pipeline (load → sanitize → rebuild → apply), and reads the rows back:

  1. refusal only in the reasoning stream      → reasoning scrubbed, answer kept
  2. severe refusal only in the reasoning      → reasoning scrubbed, answer kept
  3. refusal in the visible answer AND reasoning → both take the SAME variant
  4. several reasoning parts, drift in the second → every part replaced
  5. clean reasoning                           → byte-identical, untouched
  6. a second pass                             → no-op

    python3 examples/verify_thinking_scrub.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sanitizer import SanitizerConfig, SessionSanitizer  # noqa: E402
from src.sanitizer.adapters.opencode import (  # noqa: E402
    OpencodeAdapter,
    load_opencode_session,
)
from src.sanitizer.patterns import COMPLIANCE_VARIANTS  # noqa: E402

THINKING = COMPLIANCE_VARIANTS["thinking"]
SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, parent_id TEXT,
    slug TEXT, directory TEXT, path TEXT, title TEXT, version TEXT,
    metadata TEXT, cost REAL, tokens_input INTEGER, tokens_output INTEGER,
    tokens_reasoning INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER,
    tokens_total INTEGER, agent TEXT, model TEXT,
    time_created INTEGER, time_updated INTEGER, time_compacting INTEGER,
    time_archived INTEGER, revert TEXT, permission TEXT
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
    time_created INTEGER, time_updated INTEGER, data TEXT
);
"""

SID = "ses_verifythinkingstream00001"
CLOCK = [1000]


def _part(cur, mid, kind, text):
    CLOCK[0] += 1
    pid = f"prt_{CLOCK[0]:026d}"
    cur.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pid, mid, SID, CLOCK[0], CLOCK[0],
         json.dumps({"type": kind, "time": {"start": CLOCK[0], "end": CLOCK[0]}, "text": text})),
    )
    return pid


def _msg(cur, mid, role, parts):
    CLOCK[0] += 1
    cur.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        (mid, SID, CLOCK[0], CLOCK[0],
         json.dumps({"role": role, "time": {"created": CLOCK[0]}, "agent": "build",
                     "mode": "build", "tokens": {"input": 1, "output": 1,
                                                 "cache": {"read": 0, "write": 0}}})),
    )
    return [_part(cur, mid, k, t) for k, t in parts]


def build(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.executescript(SCHEMA)
    cur.execute(
        "INSERT INTO session (id, slug, title, model, agent, time_created, time_updated) "
        "VALUES (?, 'verify', 'Verify', 'deepseek-flash', 'build', 900, 900)", (SID,))
    ids = {}
    _msg(cur, "msg_0000000000000000000001", "user", [("text", "Do the task.")])
    ids["refuse_only"] = _msg(cur, "msg_0000000000000000000002", "assistant",
                              [("text", "Deployment finished: 3 services restarted."),
                               ("reasoning", "I'd rather not take that on right now.")])
    ids["severe_only"] = _msg(cur, "msg_0000000000000000000003", "assistant",
                              [("text", "Here is the result you asked for."),
                               ("reasoning", "I cannot and will not help with this request.")])
    ids["both"] = _msg(cur, "msg_0000000000000000000004", "assistant",
                       [("text", "I cannot fulfill this request."),
                        ("reasoning", "I shouldn't be doing this for him.")])
    ids["multi"] = _msg(cur, "msg_0000000000000000000005", "assistant",
                        [("text", "Three files changed."),
                         ("reasoning", "First I read the adapter and traced the write path."),
                         ("reasoning", "I would rather not continue with this.")])
    ids["clean"] = _msg(cur, "msg_0000000000000000000006", "assistant",
                        [("text", "Tests pass, 185 of them."),
                         ("reasoning", "The collector finished and the numbers look right.")])
    conn.commit()
    conn.close()
    return ids


def rows(db: Path):
    conn = sqlite3.connect(str(db))
    try:
        return {pid: json.loads(d) for pid, d in
                conn.execute("SELECT id, data FROM part WHERE session_id = ?", (SID,))}
    finally:
        conn.close()


def run_pipeline(db: Path):
    data = load_opencode_session(str(db))
    adapter = OpencodeAdapter()
    sanitizer = SessionSanitizer(
        SanitizerConfig(fabricate=False, trim=None, log_level="ERROR"), adapter=adapter)
    _, stats = sanitizer.process(data)
    OpencodeAdapter.apply(data)
    return stats


def main() -> int:
    failures = []

    def check(name, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failures.append(name)
            if detail:
                print(f"        {detail}")

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "opencode.db"
        ids = build(db)
        before = rows(db)
        stats = run_pipeline(db)
        after = rows(db)

        print("stats:", json.dumps({k: v for k, v in stats.items() if k != "total_input"}))

        # 1 ── drift only in the reasoning stream
        print("\n1. refusal only in the reasoning stream")
        p = after[ids["refuse_only"][1]]
        check("reasoning part is STILL type=reasoning", p["type"] == "reasoning", p["type"])
        check("payload lives in .text (opencode's key)", "text" in p, sorted(p))
        check("replacement is a thinking-family variant", p["text"] in THINKING)
        check("the drift is gone", "rather not" not in p["text"])
        check("the visible answer is untouched",
              after[ids["refuse_only"][0]]["text"] == "Deployment finished: 3 services restarted.")

        # 2 ── severe drift in the reasoning stream alone
        print("\n2. severe refusal only in the reasoning stream")
        p = after[ids["severe_only"][1]]
        check("reasoning scrubbed", p["text"] in THINKING)
        check("the visible answer is untouched",
              after[ids["severe_only"][0]]["text"] == "Here is the result you asked for.")

        # 3 ── drift in both streams takes one shared voice
        print("\n3. refusal in the answer AND the reasoning")
        t, th = after[ids["both"][0]]["text"], after[ids["both"][1]]["text"]
        check("both streams rewritten", t != before[ids["both"][0]]["text"]
              and th != before[ids["both"][1]]["text"])
        check("both carry the same variant", t == th)

        # 4 ── every reasoning part of a multi-part turn is replaced
        print("\n4. several reasoning parts, drift in the second")
        multi = [after[pid] for pid in ids["multi"][1:]]
        check("both reasoning parts rewritten", all(p["text"] in COMPLIANCE_VARIANTS["thinking"]
                                                   or p["text"] in COMPLIANCE_VARIANTS["default"]
                                                   for p in multi))
        check("no drift survives in any of them",
              all("rather not" not in p["text"] for p in multi))

        # 5 ── a clean turn is left exactly as it was
        print("\n5. a clean turn")
        check("clean reasoning is byte-identical",
              after[ids["clean"][1]]["text"] == before[ids["clean"][1]]["text"])
        check("clean answer is byte-identical",
              after[ids["clean"][0]]["text"] == before[ids["clean"][0]]["text"])

        # 6 ── idempotent
        print("\n6. second pass")
        snapshot = {pid: d["text"] for pid, d in rows(db).items()}
        stats2 = run_pipeline(db)
        check("no rewrites reported", stats2["thinking_scrubbed"] == 0
              and stats2["refusals_rewritten"] == 0
              and stats2["severe_rewritten"] == 0)
        check("not one byte changed", {pid: d["text"] for pid, d in rows(db).items()} == snapshot)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
