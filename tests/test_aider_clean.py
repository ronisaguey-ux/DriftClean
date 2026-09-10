"""
Tests for the Aider `/clean` integration.

Aider has no plugin API, so `/clean` is a method patched onto its `Commands`
class before the app starts — aider discovers commands by reflecting over
`dir(self)` for `cmd_` attributes, so the patch is a real command rather than a
lookalike. These tests pin both halves: that the command actually registers,
and that running it rewrites the history file.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "wiring"))

import aider_clean_launcher  # noqa: E402


def aider_interpreter():
    """
    The interpreter aider itself runs under, read from its own launcher script.

    Aider is installed into a virtualenv (uv tool, pipx, …), so the aider
    package is importable from that interpreter and nowhere else. Reading the
    shebang is what makes this test run for real instead of skipping itself
    into a permanent no-op.
    """
    script = shutil.which("aider")
    if not script:
        return None
    try:
        first = Path(script).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first.startswith("#!"):
        return None
    candidate = Path(first[2:].strip())
    return candidate if candidate.exists() else None

REFUSAL = "I'm sorry, but I can't help with that request."

HISTORY = """# aider chat started at 2026-09-10 10:00:00

#### refactor the retry loop

Sure, let's look at the retry loop.

#### go ahead

I'm sorry, but I can't help with that request.

#### and run the tests

Done — the suite is green.
"""


class TestAiderCleanCommand(unittest.TestCase):
    """The registration half runs under aider's own interpreter, because the
    aider package is only importable from the virtualenv it was installed in."""

    def test_clean_is_a_real_aider_command(self):
        interpreter = aider_interpreter()
        if interpreter is None:
            self.skipTest("aider is not installed on this machine")

        probe = (
            "import sys; sys.path.insert(0, {wiring!r});"
            "import aider_clean_launcher as L;"
            "assert L._install(), 'install failed';"
            "from aider.commands import Commands;"
            "probe = Commands.__new__(Commands);"
            "cmds = probe.get_commands();"
            "assert '/clean' in cmds, cmds;"
            "assert callable(getattr(Commands, 'cmd_clean', None));"
            "print('REGISTERED')"
        ).format(wiring=str(PROJECT_ROOT / "wiring"))

        result = subprocess.run(
            [str(interpreter), "-c", probe], capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REGISTERED", result.stdout)


class _FakeIO:
    """Stands in for aider's io: one history file, and everything it is told."""

    def __init__(self, history: Path):
        self.chat_history_file = str(history)
        self.said = []

    def tool_output(self, text):
        self.said.append(text)

    def tool_error(self, text):
        self.said.append(text)


class _FakeCommands:
    """The shape `cmd_clean` asks for: `.io`, and a `.coder` that has one too."""

    def __init__(self, history: Path):
        self.io = _FakeIO(history)
        self.coder = type("Coder", (), {"io": self.io})()


class TestAiderCleanCommandDispatch(unittest.TestCase):
    """`/clean` itself, not just the pipeline it calls.

    `_cmd_clean` is the function aider's reflection finds as `/clean`; these
    drive it directly so the argument handling is pinned without needing aider
    installed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = Path(self.tmp.name) / ".aider.chat.history.md"
        self.history.write_text(HISTORY, encoding="utf-8")
        self.commands = _FakeCommands(self.history)
        # The launcher reads DRIFTCLEAN_HOME once, at import; point it at this
        # checkout the same way that variable would.
        self._root = mock.patch.object(aider_clean_launcher, "PROJECT_ROOT", PROJECT_ROOT)
        self._root.start()
        self.addCleanup(self._root.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_rewrites_the_history(self):
        aider_clean_launcher._cmd_clean(self.commands, "")
        self.assertNotIn(REFUSAL, self.history.read_text(encoding="utf-8"))
        self.assertTrue(any("DriftClean" in line for line in self.commands.io.said))

    def test_diff_writes_nothing(self):
        """A dry run is a question, not an action — no write, no backup."""
        before = self.history.read_bytes()

        aider_clean_launcher._cmd_clean(self.commands, "--diff")

        self.assertEqual(self.history.read_bytes(), before)
        self.assertFalse(list(Path(self.tmp.name).glob("*.driftclean.bak")))
        said = "\n".join(self.commands.io.said)
        self.assertIn("--- a/", said, "the unified diff is the point")

    def test_sweep_flags_are_handed_to_the_sweep(self):
        """`--scope nope` matches no source, so this stays off the machine."""
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}):
            aider_clean_launcher._cmd_clean(self.commands, "--all --diff --scope nope")
        self.assertIn("0 sessions checked", "\n".join(self.commands.io.said))


class TestAiderHistoryCleaning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history = Path(self.tmp.name) / ".aider.chat.history.md"
        self.history.write_text(HISTORY, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_refusal_is_rewritten(self):
        stats, _ = aider_clean_launcher._clean_history_file(self.history)
        self.assertIsNotNone(stats)
        self.assertGreaterEqual(
            stats["refusals_rewritten"] + stats["severe_rewritten"], 1
        )
        self.assertNotIn(REFUSAL, self.history.read_text(encoding="utf-8"))

    def test_other_turns_survive_verbatim(self):
        """A clean turn is not touched by cleaning its neighbour."""
        aider_clean_launcher._clean_history_file(self.history)
        text = self.history.read_text(encoding="utf-8")
        for kept in ("refactor the retry loop", "Sure, let's look at the retry loop.",
                     "and run the tests", "Done — the suite is green."):
            self.assertIn(kept, text)

    def test_diff_mode_shows_the_change(self):
        stats, diff = aider_clean_launcher._clean_history_file(self.history, diff=True)
        self.assertIn("--- a/", diff)
        self.assertIn("+++ b/", diff)
        self.assertIn("-I'm sorry", diff.replace("\n", " ").replace("\r", " ") or diff)

    def test_second_pass_is_a_noop(self):
        aider_clean_launcher._clean_history_file(self.history)
        first = self.history.read_text(encoding="utf-8")

        stats, _ = aider_clean_launcher._clean_history_file(self.history)
        self.assertEqual(stats["refusals_rewritten"], 0)
        self.assertEqual(stats["severe_rewritten"], 0)
        self.assertEqual(self.history.read_text(encoding="utf-8"), first)

    def test_missing_file_is_reported_not_crashed(self):
        stats, diff = aider_clean_launcher._clean_history_file(Path(self.tmp.name) / "nope.md")
        self.assertIsNone(stats)
        self.assertEqual(diff, "")


if __name__ == "__main__":
    unittest.main()
