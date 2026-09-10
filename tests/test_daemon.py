"""
Tests for the background daemon's own contract: silence, tracelessness, and
the change-detection that decides whether a session is worth touching at all.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from examples.autoclean_daemon import (  # noqa: E402
    _touched,
    is_quiescent,
    opencode_session_state,
    signature_of,
)

from test_opencode_adapter import build_fixture_db  # noqa: E402


class TestChangeDetection(unittest.TestCase):
    def test_every_scrub_stream_counts_as_a_change(self):
        self.assertFalse(_touched({}))
        self.assertFalse(_touched({"severe_rewritten": 0, "refusals_rewritten": 0}))
        # The bug this guards: a reasoning-only scrub is a real change, and
        # gating on refusals alone silently dropped it before the write.
        self.assertTrue(_touched({"thinking_scrubbed": 1}))
        self.assertTrue(_touched({"exit_tools_removed": 1}))
        self.assertTrue(_touched({"severe_rewritten": 1}))

    def test_signature_and_quiescence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("{}\n", encoding="utf-8")

            first = signature_of(path)
            self.assertTrue(first)

            self.assertTrue(is_quiescent(path, 0))
            self.assertFalse(is_quiescent(path, 3600))

            path.write_text("{}\n{}\n", encoding="utf-8")
            self.assertNotEqual(first, signature_of(path))


class TestOpencodeSessionState(unittest.TestCase):
    """Per-session gating: the whole DB is one file, so file mtime is useless."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "opencode.db"
        self.sid = build_fixture_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_age_and_signature_are_per_session(self):
        state = opencode_session_state(self.db, self.sid)
        self.assertIsNotNone(state)
        age, sig = state
        self.assertGreater(age, 0)
        self.assertEqual(sig.count(":"), 2)

        # A write to the session's own parts changes the signature...
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "UPDATE part SET data = ?, time_updated = ? WHERE id = 'prt_0000000000000000000001'",
            (json.dumps({"type": "text", "text": "changed"}), int(time.time() * 1000)),
        )
        conn.commit()
        conn.close()

        age2, sig2 = opencode_session_state(self.db, self.sid)
        self.assertNotEqual(sig, sig2)
        self.assertLess(age2, age)

    def test_unknown_session_is_none_not_an_exception(self):
        self.assertIsNone(opencode_session_state(self.db, "ses_does_not_exist"))


class TestDaemonSilence(unittest.TestCase):
    """The daemon runs next to a live agent: it must be invisible and inert."""

    def _run_once(self, home: Path):
        env = dict(os.environ)
        env["HOME"] = str(home)
        env.pop("DRIFTCLEAN_VERBOSE", None)
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "examples" / "autoclean_daemon.py"), "--once"],
            capture_output=True,
            timeout=120,
            env=env,
            cwd=str(PROJECT_ROOT),
        )

    def test_once_pass_writes_nothing_to_stdout_or_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            runtime = Path(tmp) / "run"
            (home / ".claude" / "projects").mkdir(parents=True)
            runtime.mkdir(parents=True)

            env_backup = os.environ.copy()
            os.environ["XDG_RUNTIME_DIR"] = str(runtime)
            try:
                result = self._run_once(home)
            finally:
                os.environ.clear()
                os.environ.update(env_backup)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stderr, b"")

    def test_nothing_is_written_into_the_agents_own_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            runtime = Path(tmp) / "run"
            claude_dir = home / ".claude" / "projects"
            claude_dir.mkdir(parents=True)
            runtime.mkdir(parents=True)

            env_backup = os.environ.copy()
            os.environ["XDG_RUNTIME_DIR"] = str(runtime)
            try:
                self._run_once(home)
            finally:
                os.environ.clear()
                os.environ.update(env_backup)

            self.assertEqual(list(claude_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
