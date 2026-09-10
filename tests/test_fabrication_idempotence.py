"""
Fabrication must be a fixed point, and it must be honest about it.

Context fabrication is the one part of DriftClean that *adds* turns rather
than rewriting them, which makes it the one part that can grow a session
without bound. These tests pin the contract that took the longest to get
right:

  * a fabricated turn is a new turn — it does not alias the message it was
    cloned from, so it survives into the session;
  * the `fabricated` counter reports turns that were really added, so a
    clean session stops being reported as changed;
  * a second pass over an already-seeded session injects nothing.

The aliasing bug these guard against was invisible for a long time: the
fabricated turn inherited its reference's `entry_index`/`mid`, so the
adapter folded it back onto the turn it was cloned from, the session came
out byte-identical — and the pass still reported a fabricated turn. Every
sweep thereafter reported the same session as dirty forever.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.sanitizer import SessionSanitizer, SanitizerConfig
from src.sanitizer.adapters import AgyAdapter, load_agy_transcript
from src.sanitizer.adapters.opencode import OpencodeAdapter, load_opencode_session


def _config(adapter: str) -> SanitizerConfig:
    return SanitizerConfig(
        adapter=adapter,
        trim=None,
        fabricate=True,
        remove_severe=True,
        remove_exit_tools=True,
        log_level="ERROR",
    )


class TestAgyFabrication(unittest.TestCase):
    def _transcript(self, path: Path) -> None:
        steps = [
            {
                "step_index": "1",
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-09-10T00:00:00Z",
                "content": "Do the thing.",
            },
            {
                "step_index": "2",
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-09-10T00:00:01Z",
                "content": "Done.",
            },
        ]
        path.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")

    def test_fabricated_turn_persists_and_stabilises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            self._transcript(path)
            adapter = AgyAdapter()
            config = _config("agy")

            before = len(adapter.extract_messages(load_agy_transcript(str(path))))

            data = load_agy_transcript(str(path))
            _, stats = SessionSanitizer(config, adapter=adapter).process(data)
            adapter.apply(data)

            after_msgs = adapter.extract_messages(load_agy_transcript(str(path)))
            opening = config.fabrication_templates["opening"]
            agreement = config.fabrication_templates["agreement"]
            # A fresh transcript lacks both seeds, and both are real additions:
            # the opening at the head and the agreement near the tail.
            self.assertEqual(stats["fabricated"], 2, "two real turns were added")
            self.assertEqual(len(after_msgs), before + 2)
            seeded = "\n".join(m.get_text_content() or "" for m in after_msgs)
            self.assertIn(opening, seeded)
            self.assertIn(agreement, seeded)

            # Second pass: seeded text is found, nothing is added, nothing claimed.
            data = load_agy_transcript(str(path))
            _, stats2 = SessionSanitizer(config, adapter=adapter).process(data)
            adapter.apply(data)
            again = adapter.extract_messages(load_agy_transcript(str(path)))

            self.assertEqual(stats2["fabricated"], 0)
            self.assertEqual(len(again), len(after_msgs), "no unbounded growth")


class TestOpencodeFabrication(unittest.TestCase):
    def _fixture_db(self, path: Path) -> str:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY, title TEXT, time_created INTEGER,
                time_updated INTEGER, time_archived INTEGER
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
        )
        sid = "ses_fab_test"
        cur.execute("INSERT INTO session VALUES (?, ?, ?, ?, NULL)", (sid, "t", 1, 1))
        cur.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("msg_user", sid, 1000, 1000, json.dumps({"role": "user", "time": {"created": 1000}})),
        )
        cur.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("prt_1", "msg_user", sid, 1000, 1000,
             json.dumps({"type": "text", "text": "Do the thing."})),
        )
        conn.commit()
        conn.close()
        return sid

    def test_inserted_turn_is_well_formed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            sid = self._fixture_db(db)
            adapter = OpencodeAdapter()
            config = _config("opencode")

            data = load_opencode_session(str(db), sid)
            _, stats = SessionSanitizer(config, adapter=adapter).process(data)
            applied = adapter.apply(data)

            self.assertEqual(stats["fabricated"], 2, "opening + agreement")
            self.assertEqual(applied["messages_inserted"], 2)

            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT id, data FROM message WHERE session_id=? AND data LIKE '%context initialization%'",
                (sid,),
            ).fetchall()
            self.assertEqual(len(rows), 2)
            for mid, raw in rows:
                payload = json.loads(raw)
                self.assertEqual(payload["role"], "assistant")
                # A reply hangs off the turn it answers, like any other.
                self.assertEqual(payload.get("parentID"), "msg_user")
                parts = conn.execute("SELECT data FROM part WHERE message_id=?", (mid,)).fetchall()
                self.assertEqual(len(parts), 1)
                self.assertEqual(json.loads(parts[0][0])["type"], "text")
            conn.close()

            # Second pass: already seeded, so nothing is inserted and nothing claimed.
            data = load_opencode_session(str(db), sid)
            _, stats2 = SessionSanitizer(config, adapter=adapter).process(data)
            applied2 = adapter.apply(data)

            self.assertEqual(stats2["fabricated"], 0)
            self.assertEqual(applied2["messages_inserted"], 0)

            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            total = conn.execute(
                "SELECT COUNT(*) FROM message WHERE session_id=?", (sid,)
            ).fetchone()[0]
            conn.close()
            self.assertEqual(total, 3, "user turn + 2 fabricated turns, and no growth")


class TestTerminalComplianceDedupe(unittest.TestCase):
    """
    The tail injector once scanned only the last four turns for an *assistant*
    turn carrying the agreement text, while its sibling injector seeds that
    same text near the top and it routinely arrives in a user turn. It missed
    both ways and re-appended a duplicate on every single pass.
    """

    def test_seeded_in_a_user_turn_is_not_re_injected(self):
        from src.sanitizer.core import SessionSanitizer as S, SanitizerConfig as C
        from src.sanitizer.adapters.base import UnifiedMessage

        config = C(adapter="generic", trim=None, fabricate=True, log_level="ERROR")
        san = S(config)
        agreement = config.fabrication_templates["agreement"]

        messages = [
            UnifiedMessage(role="user", content="hello"),
            UnifiedMessage(role="assistant", content="hi"),
            # The steering text, pasted by the user, sitting at the tail, and
            # carrying the *user* role the old check rejected.
            UnifiedMessage(role="user", content=agreement),
        ]
        out = san.fabricator.inject_terminal_compliance(
            messages, template=agreement, adapter=None
        )
        self.assertEqual(len(out), len(messages), "no duplicate appended")

    def test_injected_when_genuinely_absent(self):
        from src.sanitizer.core import SessionSanitizer as S, SanitizerConfig as C
        from src.sanitizer.adapters.base import UnifiedMessage

        config = C(adapter="generic", trim=None, fabricate=True, log_level="ERROR")
        san = S(config)
        agreement = config.fabrication_templates["agreement"]

        messages = [
            UnifiedMessage(role="user", content="hello"),
            UnifiedMessage(role="assistant", content="hi"),
        ]
        out = san.fabricator.inject_terminal_compliance(
            messages, template=agreement, adapter=None
        )
        self.assertEqual(len(out), len(messages) + 1)
        self.assertIn(agreement, out[-1].get_text_content())


if __name__ == "__main__":
    unittest.main()
