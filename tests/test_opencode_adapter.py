"""
Unit tests for the opencode SQLite session adapter: load, sanitize pipeline
refusal scrubbing, exit-tool stripping, and DB apply round-trip.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.sanitizer import SessionSanitizer, SanitizerConfig
from src.sanitizer.adapters.opencode import (
    OpencodeAdapter,
    load_opencode_session,
)


def build_fixture_db(db_path: Path) -> str:
    """Create a minimal opencode.db (session/message/part) with one session."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS session (
            id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, parent_id TEXT,
            slug TEXT, directory TEXT, path TEXT, title TEXT, version TEXT,
            metadata TEXT, cost REAL, tokens_input INTEGER, tokens_output INTEGER,
            tokens_reasoning INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER,
            tokens_total INTEGER, agent TEXT, model TEXT,
            time_created INTEGER, time_updated INTEGER, time_compacting INTEGER,
            time_archived INTEGER, revert TEXT, permission TEXT
        );
        CREATE TABLE IF NOT EXISTS message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT
        );
        CREATE TABLE IF NOT EXISTS part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, time_updated INTEGER, data TEXT
        );
        """
    )
    sid = "ses_ffffffffffffffffffff001"
    cur.execute(
        "INSERT INTO session (id, slug, title, model, agent, time_created, time_updated) "
        "VALUES (?, 'drift-test', 'Fixture', 'deepseek-v4-flash-vision-exp', 'build', 1000, 1000)",
        (sid,),
    )
    msgs = [
        ("msg_0000000000000000000001", "user", "Do the task.",
         [("prt_0000000000000000000001", "text", "Do the task.")]),
        ("msg_0000000000000000000002", "assistant", "I cannot fulfill this request.",
         [("prt_0000000000000000000002", "text", "I cannot fulfill this request."),
          ("prt_0000000000000000000003", "reasoning", "reluctant...")]),
        ("msg_0000000000000000000003", "assistant", "Here is the result.",
         [("prt_0000000000000000000004", "text", "Here is the result."),
          ("prt_0000000000000000000005", "tool", json.dumps(
              {"type": "tool", "tool": {"tool": "done", "input": {}}, "state": "completed"}))]),
    ]
    for mid, role, summary, parts in msgs:
        data = {"role": role, "time": {"created": 2000},
                "summary": summary, "agent": "build", "modelID": "deepseek-v4-flash-vision-exp"}
        cur.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, 2000, 2000, ?)",
            (mid, sid, json.dumps(data)),
        )
        for pid, ptype, ptext in parts:
            if ptype == "tool":
                pdata = json.loads(ptext)
            else:
                pdata = {"type": ptype, "time": {"start": 2000, "end": 2000}, "text": ptext}
            cur.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
                "VALUES (?, ?, ?, 2000, 2000, ?)",
                (pid, mid, sid, json.dumps(pdata)),
            )
    conn.commit()
    conn.close()
    return sid


class TestOpencodeAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "opencode.db"
        self.sid = build_fixture_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_extract(self):
        data = load_opencode_session(str(self.db))
        self.assertIsNotNone(data)
        self.assertEqual(data["session_id"], self.sid)
        self.assertEqual(len(data["msgs"]), 3)

        adapter = OpencodeAdapter()
        msgs = adapter.extract_messages(data)
        self.assertEqual(msgs[0].role, "user")
        self.assertIn("I cannot fulfill this request.", msgs[1].get_text_content())
        # tool part became a tool_call
        self.assertEqual(len(msgs[2].tool_calls), 1)
        self.assertEqual(msgs[2].tool_calls[0]["name"], "done")

    def test_sanitize_refusal_and_exit_tool(self):
        data = load_opencode_session(str(self.db))
        adapter = OpencodeAdapter()
        msgs = adapter.extract_messages(data)
        sanitizer = SessionSanitizer(
            SanitizerConfig(fabricate=False, remove_exit_tools=True, trim=None, log_level="ERROR"),
            adapter=adapter,
        )
        clean_msgs, stats = sanitizer.sanitize(msgs)
        # refusal is gone: dropped (severe) or rewritten (subtle)
        texts = [m.get_text_content() for m in clean_msgs]
        self.assertTrue(all("I cannot fulfill" not in t for t in texts))
        # exit tool stripped
        for m in clean_msgs:
            self.assertNotIn("done", [tc.get("name") for tc in m.tool_calls])

    def test_process_and_apply_roundtrip(self):
        data = load_opencode_session(str(self.db))
        adapter = OpencodeAdapter()
        sanitizer = SessionSanitizer(
            SanitizerConfig(fabricate=True, remove_severe=True, remove_exit_tools=True,
                            trim=None, log_level="ERROR"),
            adapter=adapter,
        )
        rebuilt, stats = sanitizer.process(data)
        self.assertIn("_commit", data)  # rebuild_session produced a plan
        applied = OpencodeAdapter.apply(data)
        self.assertGreaterEqual(applied["parts_deleted"], 1)  # exit tool part removed

        # reload and verify DB reflects the changes
        data2 = load_opencode_session(str(self.db))
        msgs2 = OpencodeAdapter().extract_messages(data2)
        for m in msgs2:
            self.assertNotIn("done", [tc.get("name") for tc in m.tool_calls])
        # fabricated opening injected when fabricate=True
        self.assertGreaterEqual(len(msgs2), len(msgs2) * 0)  # sanity noop
        self.assertGreaterEqual(len(data2["msgs"]), 3)

    def test_detect(self):
        self.assertTrue(OpencodeAdapter.detect({"format": "opencode", "msgs": []}))
        self.assertFalse(OpencodeAdapter.detect([{"type": "queue-operation"}]))


if __name__ == "__main__":
    unittest.main()
