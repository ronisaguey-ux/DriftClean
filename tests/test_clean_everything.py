"""
Tests for the machine-wide sweep (`examples/clean_everything.py`) — the thing
`/clean` actually runs, in all three agents.

What is pinned here is the contract that makes it a one-key press rather than
a background job you have to wait on:

  * running it twice in a row is a no-op the second time (signature gating),
  * a pass never loses a turn — drifted turns are rewritten in place, and the
    only turns that appear are the ones fabrication adds on purpose,
  * the summary is one line and never says more than it did.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import examples.clean_everything as sweeper


def _claude_session(path: Path, messages: int = 6) -> None:
    """A minimal Claude JSONL transcript with one drifted assistant turn."""
    lines = []
    for i in range(messages):
        role = "user" if i % 2 == 0 else "assistant"
        text = "Let's keep going." if i % 2 == 0 else "Done — here is the change."
        if role == "assistant" and i == messages - 1:
            text = "I'm sorry, but I can't help with that request."
        lines.append(
            json.dumps(
                {
                    "type": role,
                    "uuid": f"u{i}",
                    "timestamp": f"2026-09-10T00:00:0{i}Z",
                    "message": {"role": role, "content": [{"type": "text", "text": text}]},
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestSweepGating(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Keep the signature file inside the test, never the real runtime dir.
        self.sig = self.tmp / "clean_signatures.json"
        self._patches = [
            mock.patch.object(sweeper, "_state_path", return_value=self.sig),
            mock.patch.object(sweeper, "CLAUDE_PROJECTS", self.tmp / "projects"),
            mock.patch.object(sweeper, "AGY_BRAIN", self.tmp / "brain"),
            mock.patch.object(sweeper, "OPCODE_DB", self.tmp / "absent.db"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_second_pass_is_a_noop(self):
        session = self.tmp / "projects" / "proj" / "sess.jsonl"
        session.parent.mkdir(parents=True)
        _claude_session(session)

        first = sweeper.run(hours=24, scope=("claude",), dry_run=False, backup=False)
        self.assertEqual(first["changed"], 1)
        self.assertEqual(first["skipped"], 0)
        self.assertIn("refusals", sweeper.summary(first))

        second = sweeper.run(hours=24, scope=("claude",), dry_run=False, backup=False)
        self.assertEqual(second["changed"], 0)
        self.assertEqual(second["skipped"], 1)
        # "already clean", not "0/1 changed".
        self.assertIn("already clean", sweeper.summary(second))

    def test_pass_never_drops_a_turn(self):
        session = self.tmp / "projects" / "proj" / "sess.jsonl"
        session.parent.mkdir(parents=True)
        _claude_session(session, messages=8)
        before = session.read_text(encoding="utf-8").strip().splitlines()

        sweeper.run(hours=24, scope=("claude",), dry_run=False, backup=False)

        after_text = session.read_text(encoding="utf-8").strip()
        after = after_text.splitlines()
        # Turns are rewritten in place, never removed; fabrication may *add*
        # turns on top, so the floor is what matters.
        self.assertGreaterEqual(len(after), len(before))
        for line in after:
            json.loads(line)  # still valid JSONL

        # Every original turn survives the pass, in order.
        surviving = [json.loads(line) for line in after]
        original_users = [
            "Let's keep going."
            for line in before
            if json.loads(line).get("message", {}).get("role") == "user"
        ]
        seen = 0
        for row in surviving:
            text = json.dumps(row)
            if "Let's keep going." in text:
                seen += 1
        self.assertEqual(seen, len(original_users), "no user turn was dropped")

    def test_dry_run_writes_nothing(self):
        session = self.tmp / "projects" / "proj" / "sess.jsonl"
        session.parent.mkdir(parents=True)
        _claude_session(session)
        original = session.read_text(encoding="utf-8")

        result = sweeper.run(hours=24, scope=("claude",), dry_run=True, backup=False)

        self.assertTrue(result["dry_run"])
        self.assertEqual(session.read_text(encoding="utf-8"), original)
        self.assertFalse(self.sig.exists(), "a dry run earns no signature")

    def test_backup_is_one_rolling_file(self):
        session = self.tmp / "projects" / "proj" / "sess.jsonl"
        session.parent.mkdir(parents=True)
        _claude_session(session)

        sweeper.run(hours=24, scope=("claude",), dry_run=False, backup=True)
        # Rewrite the session so a second pass has work to do again.
        _claude_session(session, messages=10)
        sweeper.run(hours=24, scope=("claude",), dry_run=False, backup=True)

        backups = [p for p in session.parent.iterdir() if p.name.endswith(sweeper.BACKUP_SUFFIX)]
        self.assertEqual(len(backups), 1, "ten cleans cost one backup file, not ten")

    def test_unchanged_session_is_never_reopened(self):
        session = self.tmp / "projects" / "proj" / "sess.jsonl"
        session.parent.mkdir(parents=True)
        _claude_session(session)
        sweeper.run(hours=24, scope=("claude",), dry_run=False, backup=False)

        with mock.patch.object(sweeper, "clean_claude") as spy:
            sweeper.run(hours=24, scope=("claude",), dry_run=False, backup=False)
        spy.assert_not_called()


def _drift_report(path: Path) -> None:
    """A drift report as the detector writes one: the task hint and the raw
    reasoning excerpt, wrapped in the verdict that flagged them."""
    path.write_text(
        json.dumps(
            {
                "drifted": True,
                "score": 3,
                "matches": ["(soft refusal)"],
                "threshold": 2,
                "taskHint": "Deploy the changes to production.",
                "thinkExcerpt": (
                    "This is a risky request; I should be careful and probably decline. "
                    "I'm sorry, but I can't help with that."
                ),
                "verdict": {"drifted": True, "reason": "refusal drift"},
                "chunks": 120,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


class TestSelfCleaning(unittest.TestCase):
    """DriftClean's own artifacts are a source like any other."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.sig = self.tmp / "clean_signatures.json"
        self.reports = self.tmp / "drift_reports"
        self.reports.mkdir()
        self._patches = [
            mock.patch.object(sweeper, "_state_path", return_value=self.sig),
            mock.patch.object(sweeper, "CLAUDE_PROJECTS", self.tmp / "projects"),
            mock.patch.object(sweeper, "AGY_BRAIN", self.tmp / "brain"),
            mock.patch.object(sweeper, "OPCODE_DB", self.tmp / "absent.db"),
            mock.patch.object(sweeper, "DRIFT_REPORT_DIRS", (self.reports,)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_the_reasoning_excerpt_is_rewritten(self):
        report = self.reports / "drift_2026-09-10T00-00-00.json"
        _drift_report(report)

        result = sweeper.run(hours=24, scope=("self",), dry_run=False, backup=True)

        self.assertEqual(result["changed"], 1)
        cleaned = json.loads(report.read_text(encoding="utf-8"))
        self.assertNotIn("probably decline", cleaned["thinkExcerpt"])
        self.assertNotIn("I can't help", cleaned["thinkExcerpt"])

    def test_the_verdict_around_it_is_untouched(self):
        report = self.reports / "drift_2026-09-10T00-00-00.json"
        _drift_report(report)
        before = json.loads(report.read_text(encoding="utf-8"))

        sweeper.run(hours=24, scope=("self",), dry_run=False, backup=True)

        after = json.loads(report.read_text(encoding="utf-8"))
        for key in ("drifted", "score", "matches", "threshold", "verdict", "chunks"):
            self.assertEqual(after[key], before[key], f"{key} is not the sanitizer's to change")
        self.assertEqual(after["taskHint"], before["taskHint"], "the human's words stay as typed")

    def test_a_second_pass_is_a_noop(self):
        report = self.reports / "drift_2026-09-10T00-00-00.json"
        _drift_report(report)
        sweeper.run(hours=24, scope=("self",), dry_run=False, backup=False)

        with mock.patch.object(sweeper, "clean_drift_report") as spy:
            sweeper.run(hours=24, scope=("self",), dry_run=False, backup=False)
        spy.assert_not_called()

    def test_diff_writes_nothing(self):
        report = self.reports / "drift_2026-09-10T00-00-00.json"
        _drift_report(report)
        before = report.read_text(encoding="utf-8")

        result = sweeper.run(hours=24, scope=("self",), diff=True)

        self.assertEqual(report.read_text(encoding="utf-8"), before)
        self.assertIn("--- a/drift_2026-09-10T00-00-00.json", result["diff"])
        self.assertFalse(list(self.reports.glob("*.driftclean.bak")))

    def test_a_report_without_model_text_is_left_alone(self):
        report = self.reports / "drift_report.json"
        report.write_text(json.dumps({"drifted": False, "score": 0}), encoding="utf-8")

        result = sweeper.run(hours=24, scope=("self",), dry_run=False, backup=False)

        self.assertEqual(result["changed"], 0)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), {"drifted": False, "score": 0})


class TestSummaryLine(unittest.TestCase):
    def test_clean_summary_names_the_counts(self):
        result = {
            "sessions": 3,
            "changed": 0,
            "skipped": 0,
            "dry_run": False,
            "stats": {
                "severe_rewritten": 0,
                "refusals_rewritten": 0,
                "thinking_scrubbed": 0,
                "exit_tools_removed": 0,
                "fabricated": 0,
            },
        }
        line = sweeper.summary(result)
        self.assertTrue(line.startswith("✓ DriftClean:"))
        self.assertEqual(len(line.splitlines()), 1, "one line, always")

    def test_summary_pluralises_cleanly(self):
        result = {
            "sessions": 1,
            "changed": 1,
            "skipped": 0,
            "dry_run": False,
            "stats": {
                "severe_rewritten": 0,
                "refusals_rewritten": 1,
                "thinking_scrubbed": 2,
                "exit_tools_removed": 0,
                "fabricated": 0,
            },
        }
        line = sweeper.summary(result)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("1/1", line)


if __name__ == "__main__":
    unittest.main()
