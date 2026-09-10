"""
Tests for the Claude Code `/clean` UserPromptSubmit hook.

The hook has to hold two contracts at once. For the model it is invisible:
`/clean` exits 2 with the prompt swallowed, so no turn is spent and no tokens
are burned. For the human it is the whole interface — one line on stderr
saying what was rewritten, and under `--diff` the diff itself, with nothing
written to disk.

The session file is discovered through `~/.claude/projects`, so the hook runs
in a subprocess with HOME pointed at a temporary directory: the only session
it can find is the fixture's.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK = PROJECT_ROOT / "wiring" / "claude_clean_hook.py"

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from test_clean_everything import _claude_session  # noqa: E402

SESSION_ID = "11111111-2222-3333-4444-555555555555"
REFUSAL = "I'm sorry, but I can't help with that request."


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.session = self.home / ".claude" / "projects" / "-tmp-project" / f"{SESSION_ID}.jsonl"
        self.session.parent.mkdir(parents=True)
        _claude_session(self.session)

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, prompt: str) -> subprocess.CompletedProcess:
        payload = {
            "session_id": SESSION_ID,
            "transcript_path": str(self.session),
            "cwd": str(PROJECT_ROOT),
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }
        env = {
            **os.environ,
            "HOME": str(self.home),
            "DRIFTCLEAN_HOME": str(PROJECT_ROOT),
            "XDG_RUNTIME_DIR": str(self.home / "runtime"),
        }
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env=env,
        )


class TestClaudeHookInert(HookCase):
    def test_an_ordinary_prompt_is_left_alone(self):
        result = self.invoke("refactor the parser")
        self.assertEqual(result.returncode, 0, "0 lets the prompt through")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")

    def test_a_prompt_that_only_mentions_the_word_is_not_a_command(self):
        result = self.invoke("please clean up this function")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")


class TestClaudeHookClean(HookCase):
    def test_clean_is_swallowed_and_reports_one_line(self):
        result = self.invoke("/clean")
        self.assertEqual(result.returncode, 2, "exit 2 is what swallows the prompt")
        self.assertIn("DriftClean", result.stderr)

    def test_clean_rewrites_the_session_and_keeps_one_backup(self):
        self.invoke("/clean")
        text = self.session.read_text(encoding="utf-8")
        self.assertNotIn(REFUSAL, text)
        for line in text.splitlines():
            if line.strip():
                json.loads(line)  # still one JSON object per line

        backups = list(self.home.rglob("*.driftclean.bak"))
        self.assertEqual(len(backups), 1, "one rolling backup per session")

    def test_a_second_clean_keeps_the_backup_count_at_one(self):
        self.invoke("/clean")
        self.invoke("/clean")
        self.assertEqual(len(list(self.home.rglob("*.driftclean.bak"))), 1)


class TestClaudeHookDiff(HookCase):
    def test_diff_writes_nothing(self):
        before = self.session.read_bytes()
        result = self.invoke("/clean --diff")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.session.read_bytes(), before, "--diff must not write")
        self.assertFalse(
            list(self.home.rglob("*.driftclean.bak")),
            "--diff must not take a backup either",
        )

    def test_diff_shows_the_rewrite_and_says_it_is_a_dry_run(self):
        result = self.invoke("/clean --diff")
        self.assertIn("dry run", result.stderr)
        self.assertIn("nothing was written", result.stderr)
        self.assertIn("--- a/", result.stderr, "the unified diff is the point")
        self.assertIn("-assistant| " + REFUSAL, result.stderr)

    def test_diff_on_a_clean_session_still_reports(self):
        self.invoke("/clean")
        result = self.invoke("/clean --diff")
        self.assertEqual(result.returncode, 2)
        self.assertIn("0 severe", result.stderr)


class TestClaudeHookAllDiff(HookCase):
    def test_all_diff_goes_through_the_sweep(self):
        """`--scope nope` matches no source, so this stays off the machine."""
        result = self.invoke("/clean --all --diff --scope nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("DriftClean", result.stderr)
        self.assertIn("0 sessions checked", result.stderr)


if __name__ == "__main__":
    unittest.main()
