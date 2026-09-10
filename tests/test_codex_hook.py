"""
Tests for the Codex CLI `/clean` hook.

What makes `/clean` worth having in an agent is that it costs the agent
nothing: the prompt is swallowed before the model sees it, so there is no
turn, no tokens, and no chance for a drifted model to argue with the command.
These tests pin that — the exit code that does the swallowing, and the fact
that an ordinary prompt is left completely alone.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK = PROJECT_ROOT / "wiring" / "codex_clean_hook.py"

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from test_codex_adapter import REFUSAL, build_rollout  # noqa: E402


def invoke(
    transcript: str, prompt: str = "/clean", home: str = None, root: Path = None
) -> subprocess.CompletedProcess:
    """
    Run the hook. Pointing HOME at a temporary directory is what confines the
    sweep paths — the only sessions it can find are the fixture's.
    """
    payload = {
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "cwd": "/tmp",
        "hook_event_name": "UserPromptSubmit",
        "model": "gpt-5",
        "permission_mode": "default",
        "transcript_path": transcript,
        "prompt": prompt,
    }
    env = {
        **os.environ,
        "DRIFTCLEAN_HOME": str(root or PROJECT_ROOT),
    }
    if home:
        env["HOME"] = home
        env["XDG_RUNTIME_DIR"] = str(Path(home) / "runtime")
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


class TestCodexCleanHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rollout = Path(self.tmp.name) / "rollout-2026-09-10T00-00-00-abc.jsonl"
        build_rollout(self.rollout)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_is_blocked_and_never_reaches_the_model(self):
        result = invoke(str(self.rollout))
        self.assertEqual(result.returncode, 2, "exit 2 is what swallows the prompt")
        self.assertEqual(result.stdout, "", "nothing is emitted for the model to read")
        self.assertIn("DriftClean", result.stderr)

    def test_clean_rewrites_the_session(self):
        invoke(str(self.rollout))
        text = self.rollout.read_text(encoding="utf-8")
        self.assertNotIn(REFUSAL, text)
        for line in text.splitlines():
            if line.strip():
                json.loads(line)

    def test_clean_reports_one_line(self):
        result = invoke(str(self.rollout))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def test_ordinary_prompt_passes_through_untouched(self):
        before = self.rollout.read_text(encoding="utf-8")
        result = invoke(str(self.rollout), prompt="refactor the retry loop")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.rollout.read_text(encoding="utf-8"), before)

    def test_second_clean_is_a_noop(self):
        invoke(str(self.rollout))
        second = invoke(str(self.rollout))
        self.assertEqual(second.returncode, 2)
        self.assertIn("already clean", second.stderr)

    def test_missing_rollout_is_reported_not_crashed(self):
        result = invoke(str(Path(self.tmp.name) / "nope.jsonl"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("no rollout", result.stderr)

    def test_diff_mode_writes_nothing(self):
        """`--diff` is a sweep, so this drives the real one — HOME confines it."""
        before = self.rollout.read_text(encoding="utf-8")

        result = invoke(str(self.rollout), prompt="/clean --diff", home=self.tmp.name)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.rollout.read_text(encoding="utf-8"), before, "--diff is dry by definition")
        self.assertIn("DriftClean", result.stderr, "the sweep's own summary is relayed")
        self.assertIn("nothing was written", result.stderr)

    def test_a_silent_sweep_still_blocks(self):
        """Codex reads an empty stderr as a failed hook, not a block — so the
        relay always says something, even when the sweep says nothing."""
        root = Path(self.tmp.name) / "silent_repo"
        (root / "examples").mkdir(parents=True)
        (root / "examples" / "clean_everything.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")

        result = invoke(str(self.rollout), prompt="/clean --all", home=self.tmp.name, root=root)

        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.strip(), "an empty stderr would let /clean through")
        self.assertIn("3", result.stderr)

    def test_backup_is_one_rolling_file(self):
        invoke(str(self.rollout))
        build_rollout(self.rollout)  # dirty it again
        invoke(str(self.rollout))

        backups = list(Path(self.tmp.name).glob("*" + ".driftclean.bak"))
        self.assertEqual(len(backups), 1, "ten cleans cost one backup file, not ten")


if __name__ == "__main__":
    unittest.main()
