"""
Tests for the Hermes Agent plugin — the `/clean` hermes users actually type.

The plugin's contract with hermes is narrow and worth pinning exactly: a
command registered through ``ctx.register_command`` runs in-process and its
*return value* is what the CLI prints. A handler that prints instead of
returning looks fine in a terminal and shows nothing in hermes, so these tests
call the handler the way hermes does and assert on what comes back.

The fixture database is built from the verbatim schema in
``tests/test_hermes_adapter.py`` — the same DDL hermes ships — so the plugin is
exercised against the real tables, not a convenient stand-in.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))
sys.path.insert(0, str(PROJECT_ROOT / "wiring"))

from test_hermes_adapter import REFUSAL, SESSION_ID, build_fixture_db, read_rows  # noqa: E402

from hermes_driftclean import register  # noqa: E402


class FakeContext:
    """Stands in for hermes' PluginContext, recording what gets registered."""

    def __init__(self):
        self.commands = {}

    def register_command(self, name, handler=None, description="", args_hint="", argument_mode=None):
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }


class TestHermesPluginRegistration(unittest.TestCase):
    def test_clean_registers_under_the_expected_name(self):
        ctx = FakeContext()
        register(ctx)
        self.assertIn("clean", ctx.commands)
        self.assertTrue(callable(ctx.commands["clean"]["handler"]))
        self.assertIn("--diff", ctx.commands["clean"]["args_hint"])


class TestHermesPluginClean(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"
        build_fixture_db(self.db)

        self._old_env = os.environ.get("HERMES_DB")
        os.environ["HERMES_DB"] = str(self.db)

        ctx = FakeContext()
        register(ctx)
        self.handler = ctx.commands["clean"]["handler"]

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("HERMES_DB", None)
        else:
            os.environ["HERMES_DB"] = self._old_env
        self.tmp.cleanup()

    def test_handler_returns_a_line(self):
        """Hermes prints the return value — so there must be one."""
        result = self.handler("")
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip(), "an empty return prints nothing in hermes")
        self.assertIn("DriftClean", result)

    def test_clean_rewrites_the_session(self):
        self.handler("")
        rows = read_rows(self.db)
        self.assertNotIn(REFUSAL, " ".join(str(v) for v in rows.values()))

    def test_second_call_says_already_clean(self):
        self.handler("")
        self.assertIn("already clean", self.handler(""))

    def test_diff_mode_returns_a_diff_and_writes_nothing(self):
        before = read_rows(self.db)
        result = self.handler("--diff")
        self.assertIn("---", result)
        self.assertIn("+++", result)
        self.assertEqual(read_rows(self.db), before, "--diff must not write")

    def test_missing_database_is_reported_not_crashed(self):
        os.environ["HERMES_DB"] = str(Path(self.tmp.name) / "absent.db")
        result = self.handler("")
        self.assertIsInstance(result, str)
        self.assertIn("no Hermes sessions", result)


if __name__ == "__main__":
    unittest.main()
