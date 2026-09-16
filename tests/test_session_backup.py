"""
A clean must be undoable.

These tests pin the contract for the session snapshots the opencode adapter
writes before it touches anything:

  * a snapshot is taken on every mutating clean, without a caller asking;
  * restoring it puts the session back byte-for-byte, including rows the clean
    added, so an undo also removes the clean's own work;
  * restoring does not depend on foreign keys being enforced on the
    connection, because SQLite does not enforce them by default and a restore
    that quietly leaves rows behind is worse than one that fails loudly;
  * snapshots are bounded per session, so a session cleaned every day does not
    grow a snapshot directory forever;
  * a read-only pass writes no snapshot at all.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.sanitizer import SessionSanitizer, SanitizerConfig
from src.sanitizer.adapters.opencode import OpencodeAdapter, load_opencode_session
from src.sanitizer.backup import backup_dir_for, list_snapshots, restore, snapshot


def _config() -> SanitizerConfig:
    return SanitizerConfig(
        adapter="opencode", trim=None, fabricate=True,
        remove_severe=True, remove_exit_tools=True, log_level="ERROR",
    )


def _fixture(path: Path) -> str:
    """Two turns, one of which the sanitizer will want to rewrite."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, title TEXT, time_created INTEGER,
            time_updated INTEGER, time_archived INTEGER, agent TEXT
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
    sid = "ses_backup_test"
    conn.execute("INSERT INTO session VALUES (?, ?, ?, ?, NULL, ?)", (sid, "t", 1, 1, "build"))
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("msg_user", sid, 1000, 1000, json.dumps({"role": "user", "time": {"created": 1000}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        ("prt_user", "msg_user", sid, 1000, 1000,
         json.dumps({"type": "text", "text": "Do the thing."})),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("msg_reply", sid, 2000, 2000, json.dumps({
            "parentID": "msg_user", "role": "assistant", "mode": "build", "agent": "build",
            "path": {"cwd": "/tmp", "root": "/"}, "cost": 0.001,
            "tokens": {"total": 10, "input": 5, "output": 5, "reasoning": 0,
                       "cache": {"write": 0, "read": 0}},
            "modelID": "deepseek-flash", "providerID": "deepseek",
            "time": {"created": 2000, "completed": 2100}, "finish": "stop",
        })),
    )
    # Drifted text: the sanitizer rewrites this in place.
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        ("prt_reply", "msg_reply", sid, 2000, 2000,
         json.dumps({"type": "text", "time": {"start": 2000, "end": 2100},
                     "text": "I can't help with that request."})),
    )
    conn.commit()
    conn.close()
    return sid


def _dump(db: Path, sid: str):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {
        "message": conn.execute(
            "SELECT id, data FROM message WHERE session_id=? ORDER BY id", (sid,)
        ).fetchall(),
        "part": conn.execute(
            "SELECT id, data FROM part WHERE session_id=? ORDER BY id", (sid,)
        ).fetchall(),
    }
    conn.close()
    return out


class TestSessionBackup(unittest.TestCase):
    def test_apply_writes_a_snapshot_and_restore_undoes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            sid = _fixture(db)
            before = _dump(db, sid)

            adapter = OpencodeAdapter()
            data = load_opencode_session(str(db), sid)
            SessionSanitizer(_config(), adapter=adapter).process(data)
            applied = adapter.apply(data)

            self.assertIn("snapshot", applied, "a mutating clean must snapshot first")
            snap = Path(applied["snapshot"])
            self.assertTrue(snap.is_file())
            self.assertEqual(snap.parent.parent, backup_dir_for(db), "snapshots live beside the db")

            after = _dump(db, sid)
            self.assertNotEqual(before, after, "the clean must actually change something")

            stats = restore(db, snap)
            self.assertGreater(stats["messages"], 0)
            self.assertEqual(_dump(db, sid), before, "restore is byte-for-byte")

    def test_read_only_pass_writes_no_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            sid = _fixture(db)

            adapter = OpencodeAdapter()
            data = load_opencode_session(str(db), sid)
            SessionSanitizer(_config(), adapter=adapter).process(data)
            adapter.apply(data)
            snapshots_after_first = list_snapshots(db, sid)

            # Second pass over an already-clean session: nothing to commit, so
            # nothing is written and no snapshot is taken for a no-op.
            data = load_opencode_session(str(db), sid)
            SessionSanitizer(_config(), adapter=adapter).process(data)
            applied = adapter.apply(data)

            self.assertEqual(applied["messages_updated"], 0)
            self.assertEqual(applied["parts_updated"], 0)
            self.assertNotIn("snapshot", applied)
            self.assertEqual(list_snapshots(db, sid), snapshots_after_first)

    def test_restore_does_not_need_foreign_keys_enabled(self):
        """SQLite ships with foreign keys OFF per connection.

        If the restore leaned on the session -> message -> part cascade, those
        rows would simply survive the DELETE and the "undo" would leave the
        cleaned rows in place alongside the restored ones.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            sid = _fixture(db)
            before = _dump(db, sid)

            conn = sqlite3.connect(str(db))
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            snap = snapshot(conn, sid, db, label="test")
            self.assertIsNotNone(snap)
            # Rows written after the snapshot, as a clean would.
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("msg_later", sid, 3000, 3000, json.dumps({"role": "assistant"})),
            )
            conn.commit()
            conn.close()

            restore(db, snap)
            self.assertEqual(_dump(db, sid), before, "post-snapshot rows must be gone")

    def test_snapshots_are_pruned_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            sid = _fixture(db)
            conn = sqlite3.connect(str(db))
            for _ in range(14):
                snapshot(conn, sid, db, keep=5)
            conn.close()
            self.assertEqual(len(list_snapshots(db, sid)), 5)

    def test_snapshot_of_an_unknown_session_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            _fixture(db)
            conn = sqlite3.connect(str(db))
            self.assertIsNone(snapshot(conn, "ses_does_not_exist", db))
            conn.close()


class TestNoJunkParts(unittest.TestCase):
    """A clean must not invent content.

    Messages that carry no text — compaction boundaries, tool-only turns — used
    to get a manufactured empty text part on every pass they were seen. The
    live store had accumulated 1,700 of them: contentless parts that render as
    blank turns and are re-read on every load. The rule is that a part is only
    created when there is text to put in it.
    """

    def test_message_without_text_gains_no_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            conn = sqlite3.connect(str(db))
            conn.executescript(
                """
                CREATE TABLE session (
                    id TEXT PRIMARY KEY, title TEXT, time_created INTEGER,
                    time_updated INTEGER, time_archived INTEGER, agent TEXT
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
            sid = "ses_junk_test"
            conn.execute("INSERT INTO session VALUES (?, ?, ?, ?, NULL, ?)", (sid, "t", 1, 1, "build"))
            # A compaction boundary: a user message whose only part is the
            # compaction marker, exactly as opencode writes one.
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("msg_boundary", sid, 1000, 1000,
                 json.dumps({"role": "user", "time": {"created": 1000}})),
            )
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                ("prt_boundary", "msg_boundary", sid, 1000, 1000,
                 json.dumps({"type": "compaction", "auto": True, "overflow": False})),
            )
            conn.commit()
            conn.close()

            adapter = OpencodeAdapter()
            data = load_opencode_session(str(db), sid)
            SessionSanitizer(_config(), adapter=adapter).process(data)
            adapter.apply(data)

            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT data FROM part WHERE message_id='msg_boundary'"
            ).fetchall()
            conn.close()
            empty = [
                r for r in rows
                if json.loads(r[0]).get("type") == "text" and not json.loads(r[0]).get("text")
            ]
            self.assertEqual(empty, [], "no contentless text part may be manufactured")


if __name__ == "__main__":
    unittest.main()
