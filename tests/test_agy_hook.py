"""
Tests for the Antigravity `/clean` PreInvocation hook.

Antigravity's hook contract has exactly one output channel: `injectSteps` on
stdout, which lands in the conversation before the model is invoked. There is
no way to print to the terminal, so `--diff` has to report through that same
channel — the head of the diff goes in, the whole thing goes to a file named in
the message.

Two properties are worth pinning here beyond the rewrite itself: the hook is
inert for every prompt that is not `/clean`, and only a known set of flags can
ever reach the cleaner's argv — this hook parses text a model or a human typed,
and must not hand it to a subprocess as arguments.

The sweep is exercised with `--scope nope`, a source set that matches nothing:
that keeps these tests off every real session on the machine while still
running the whole path end to end.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK = PROJECT_ROOT / "wiring" / "agy_slash_clean.py"

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from test_agy_cleaner import build_transcript  # noqa: E402

CONVERSATION_ID = "conv-123"


def build_hook_transcript(path: Path, prompt: str) -> None:
    """An agy transcript whose newest USER_INPUT step is `prompt`."""
    build_transcript(path)
    step = {
        "step_index": "5",
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "content": prompt,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(step) + "\n")


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.runtime = self.home / "runtime"
        self.runtime.mkdir()
        self.transcript = (
            self.home
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / CONVERSATION_ID
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        self.transcript.parent.mkdir(parents=True)
        build_hook_transcript(self.transcript, "/clean")

    def tearDown(self):
        self.tmp.cleanup()

    def plant(self, prompt: str) -> None:
        """Put `prompt` in the transcript without invoking the hook."""
        build_hook_transcript(self.transcript, prompt)

    def invoke(self, prompt: str = "", rewrite: bool = True, exists: bool = True) -> subprocess.CompletedProcess:
        """
        Run the hook. With `rewrite=False` the transcript is left exactly as it
        is, and the prompt the hook will read is whichever one is already in it
        — pass no prompt in that case, so the two can never disagree.
        """
        if not exists:
            self.transcript.unlink()
        elif rewrite:
            assert prompt, "rewrite=True needs a prompt"
            build_hook_transcript(self.transcript, prompt)
        payload = {
            "conversationId": CONVERSATION_ID,
            "workspacePaths": [str(PROJECT_ROOT)],
            "transcriptPath": str(self.transcript),
            "artifactDirectoryPath": str(self.home / "artifacts"),
            "modelName": "auto",
            "invocationNum": 3,
            "initialNumSteps": 10,
        }
        env = {
            **os.environ,
            "HOME": str(self.home),
            "DRIFTCLEAN_HOME": str(PROJECT_ROOT),
            "XDG_RUNTIME_DIR": str(self.runtime),
        }
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env=env,
        )


class TestAGYHookInert(HookCase):
    def test_ordinary_prompt_is_left_alone(self):
        result = self.invoke("refactor the parser")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {}, "anything else would be injected")

    def test_prompt_merely_containing_the_word_is_not_a_command(self):
        result = self.invoke("can you clean this up for me")
        self.assertEqual(json.loads(result.stdout), {})

    def test_missing_transcript_is_inert(self):
        result = self.invoke("/clean", exists=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})


class TestAGYHookClean(HookCase):
    def test_clean_injects_exactly_one_step(self):
        result = self.invoke("/clean --scope nope")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["injectSteps"]), 1)
        message = payload["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("DriftClean", message)

    def test_scope_flag_reaches_the_cleaner(self):
        """`--scope nope` matches nothing — proof the flag was honoured."""
        result = self.invoke("/clean --scope nope")
        message = json.loads(result.stdout)["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("0 sessions checked", message)

    def test_clean_rewrites_the_session_and_keeps_one_backup(self):
        """
        `--scope agy` is safe inside this test: the hook runs with HOME pointed
        at a temporary directory, so the only agy transcript it can discover is
        the fixture's.
        """
        self.plant("/clean --scope agy")
        result = self.invoke(rewrite=False)
        self.assertEqual(result.returncode, 0, result.stderr)

        text = self.transcript.read_text(encoding="utf-8")
        self.assertNotIn("I refuse to deploy", text, "the refusal is rewritten")
        self.assertNotIn("probably decline", text, "the hedge in reasoning goes too")

        backups = list(self.home.rglob("*.driftclean.bak"))
        self.assertEqual(len(backups), 1, "one rolling backup per session")
        self.assertIn("refusals", json.loads(result.stdout)["injectSteps"][0]["ephemeralMessage"])


class TestAGYHookDiff(HookCase):
    def test_diff_writes_nothing(self):
        # The prompt is planted first: the harness writing it is the only thing
        # allowed to touch this file, so the clock starts after that.
        self.plant("/clean --diff --scope nope")
        before = self.transcript.stat().st_mtime_ns
        result = self.invoke(rewrite=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.transcript.stat().st_mtime_ns, before, "--diff must not write")
        self.assertFalse(
            list(self.home.rglob("*.driftclean.bak")),
            "--diff must not take backups either",
        )

    def test_diff_says_so_in_the_message(self):
        result = self.invoke("/clean --diff --scope nope")
        message = json.loads(result.stdout)["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("DRY RUN", message)


class TestAGYHookFlagWhitelist(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(PROJECT_ROOT / "wiring"))
        import agy_slash_clean

        self.hook = agy_slash_clean

    def test_known_flags_pass_through(self):
        self.assertEqual(
            self.hook._flags(" --diff --all --hours 2 --scope codex"),
            ["--diff", "--all", "--hours", "2", "--scope", "codex"],
        )

    def test_values_for_valued_flags_are_kept_with_them(self):
        self.assertEqual(self.hook._flags(" --hours 3"), ["--hours", "3"])

    def test_a_valued_flag_with_no_value_is_dropped(self):
        self.assertEqual(self.hook._flags(" --hours"), [])

    def test_a_valued_flag_with_a_nonsense_value_is_dropped(self):
        self.assertEqual(self.hook._flags(" --scope ../../etc"), [])
        self.assertEqual(self.hook._flags(" --hours two"), [])

    def test_everything_else_is_discarded(self):
        """Typed text is never forwarded to the cleaner as an argument."""
        self.assertEqual(
            self.hook._flags(" --diff ; rm -rf / --json --evil $(whoami) --scope"),
            ["--diff", "--json"],
        )

    def test_shell_metacharacters_cannot_survive_the_whitelist(self):
        self.assertEqual(self.hook._flags(" --hours $(cat /etc/passwd) --diff"), ["--diff"])


class TestAGYHookDiffMessage(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(PROJECT_ROOT / "wiring"))
        import agy_slash_clean

        self.hook = agy_slash_clean
        self.tmp = tempfile.TemporaryDirectory()
        self._old_runtime = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = self.tmp.name

    def tearDown(self):
        if self._old_runtime is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._old_runtime
        self.tmp.cleanup()

    def test_a_clean_machine_says_so_without_a_diff(self):
        message = self.hook._diff_message("✓ DriftClean: already clean (4 sessions checked)\n")
        self.assertIn("already clean", message)

    def test_the_head_goes_in_and_the_rest_goes_to_the_file(self):
        body = ["✓ DriftClean: 1/1 sessions changed · 2 refusals shown", "", "--- a/session.jsonl"]
        body += [f"+line {n}" for n in range(400)]
        message = self.hook._diff_message("\n".join(body) + "\n")

        self.assertIn("--- a/session.jsonl", message, "the diff head is injected")
        self.assertIn("more diff lines", message, "truncation is stated, not silent")

        where = Path(self.tmp.name) / "driftclean" / "last-diff.patch"
        self.assertTrue(where.exists(), "the full diff is kept on disk")
        saved = where.read_text(encoding="utf-8")
        self.assertIn("+line 399", saved, "the file holds the whole diff, not the head")

    def test_no_output_at_all_still_states_the_contract(self):
        """Silence from the sweep must not read as an invitation to re-run it."""
        message = self.hook._diff_message("")
        self.assertIn("DRY RUN", message)
        self.assertIn("nothing to clean", message)

    def test_a_run_with_no_diff_carries_the_contract_but_no_file(self):
        message = self.hook._diff_message("✓ DriftClean: already clean (4 sessions checked)\n")
        self.assertIn("DRY RUN", message)
        self.assertNotIn("Full diff:", message)
        self.assertFalse((Path(self.tmp.name) / "driftclean" / "last-diff.patch").exists())


if __name__ == "__main__":
    unittest.main()
