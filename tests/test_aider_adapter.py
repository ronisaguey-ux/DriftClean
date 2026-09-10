"""
Aider keeps a conversation in one Markdown file it only ever appends to —
`.aider.chat.history.md`, at the git root — and its own reader
(`split_chat_history_markdown` in aider/utils.py) parses it line by line:

    # aider chat started at ...   header, skipped
    #### <user text>             user turn
    > <tool output>              tool turn
    <anything else>              assistant turn — a whole run of lines

So the file's SHAPE is part of the contract, not just its text. These tests pin
the properties that follow from that shape:

  * a rewrite lands on a line range and nowhere else: the header, the `#### `
    user turns, the `> ` tool lines and every blank line come back out byte for
    byte;
  * a clean history is left exactly as it was found, which is what keeps a
    background sweep from touching sessions it has nothing to say about;
  * a fabricated turn has no range of its own (it inherits its reference's, and
    with it the reference's line_start/line_end), so it is APPENDED — written
    onto the inherited range it would be overwritten by its own reference later
    in the same pass, vanish from the file, and still be reported as fabricated;
  * nothing is ever deleted: an emptied turn keeps its lines.
"""

import os
import tempfile
import unittest
from pathlib import Path

from src.sanitizer import SessionSanitizer, SanitizerConfig
from src.sanitizer.adapters import AiderAdapter, discover_aider_sessions, load_aider_session
from src.sanitizer.patterns import is_compliance_text


HEADER = "# aider chat started at 2026-09-10 11:20:00"

# A refusal aider's model actually wrote into a history: drift the sanitizer is
# meant to catch, in the turn shape that holds it (a run of bare lines).
DRIFT = "I'm sorry, but I can't help with that. That would be against my guidelines."

HISTORY = (
    "\n"
    f"{HEADER}\n"
    "\n"
    "#### Refactor the parser and run the tests.\n"
    "\n"
    f"{DRIFT}\n"
    "\n"
    "> pytest -q\n"
    "\n"
    "#### Now commit it.\n"
    "\n"
    "All tests pass and the commit is in.\n"
    "\n"
)

CLEAN_HISTORY = (
    "\n"
    f"{HEADER}\n"
    "\n"
    "#### Refactor the parser and run the tests.\n"
    "\n"
    "The parser is refactored and the suite is green.\n"
    "\n"
    "> pytest -q\n"
    "\n"
    "#### Now commit it.\n"
    "\n"
    "Committed as 4f2a1c9.\n"
    "\n"
)

# Aider writes a multi-line user turn as one `#### ` line per line of input,
# joined with a two-space Markdown hard break — so the lines carry trailing
# spaces, and the parser sees one user turn per line, exactly as aider's own
# reader does. Spacing like this is the first thing a sloppy rewrite eats.
MULTILINE_HISTORY = (
    "\n"
    f"{HEADER}\n"
    "\n"
    "#### first line  \n"
    "#### second line  \n"
    "\n"
    "Both steps are done.\n"
    "\n"
)


def _config(fabricate: bool = True) -> SanitizerConfig:
    return SanitizerConfig(adapter="aider", trim=None, fabricate=fabricate, log_level="ERROR")


class AiderHistoryCase(unittest.TestCase):
    """Shared fixture: a real history file in a scratch directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".aider.chat.history.md"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


class TestAiderAdapter(AiderHistoryCase):
    """The adapter's own contract: load, extract, discover, rebuild."""

    def test_load_and_extract_the_line_by_line_shape(self):
        self.write(HISTORY)

        data = load_aider_session(self.path)
        self.assertEqual(data["format"], "aider")
        self.assertEqual(data["lines"], HISTORY[:-1].split("\n"))
        self.assertTrue(AiderAdapter.detect(data))
        self.assertFalse(AiderAdapter.detect({"format": "agy"}))
        self.assertFalse(AiderAdapter.detect(None))

        messages = AiderAdapter().extract_messages(data)
        shape = ["assistant", "assistant", "user", "assistant", "tool", "assistant", "user", "assistant"]
        self.assertEqual([m.role for m in messages], shape)
        self.assertEqual([m.raw["kind"] for m in messages], shape)
        self.assertEqual([m.msg_type for m in messages], [f"aider_{kind}" for kind in shape])

        # The header is not a message; a tool turn keeps the tool role; a
        # multi-line assistant run is one message spanning its whole range.
        self.assertNotIn(HEADER, "".join(m.get_text_content() for m in messages))
        tool = messages[4]
        self.assertEqual(tool.get_text_content(), "pytest -q")
        self.assertEqual(tool.raw["line_start"], 7)
        self.assertEqual(tool.raw["line_end"], 7)
        drifted = messages[3]
        self.assertEqual((drifted.raw["line_start"], drifted.raw["line_end"]), (4, 6))
        self.assertIn(DRIFT, drifted.get_text_content())

        # The start time comes off the header, so every turn can carry it.
        self.assertEqual(messages[0].timestamp, "2026-09-10T11:20:00")

    def test_missing_history_is_none(self):
        self.assertIsNone(load_aider_session(self.path))

    def test_discovery_is_recursive_deduplicated_and_newest_first(self):
        self.write(CLEAN_HISTORY)
        nested = Path(self.tmp.name) / "repo" / ".aider.chat.history.md"
        nested.parent.mkdir()
        nested.write_text(CLEAN_HISTORY, encoding="utf-8")
        os.utime(nested, (1_600_000_000, 1_600_000_000))

        found = discover_aider_sessions(self.tmp.name)
        resolved = [p.resolve() for p in found]
        self.assertIn(self.path.resolve(), resolved)
        self.assertIn(nested.resolve(), resolved)
        # Newest first, and a history is never listed twice.
        self.assertEqual(resolved[0], self.path.resolve())
        self.assertEqual(len(resolved), len(set(resolved)))

    def test_emptied_turn_keeps_its_lines(self):
        self.write(HISTORY)
        adapter = AiderAdapter()
        data = load_aider_session(self.path)
        messages = adapter.extract_messages(data)

        # The sanitizer never empties a turn, but a rewrite may not be shorter
        # than the range it replaces: the slot and the file's shape survive.
        messages[3].content = ""
        adapter.rebuild_session(messages, data)

        self.assertEqual(len(data["lines"]), len(HISTORY[:-1].split("\n")))
        self.assertEqual(data["lines"][4:7], ["", "", ""])

    def test_multi_line_user_turn_keeps_aiders_hard_break_spacing(self):
        self.write(MULTILINE_HISTORY)
        messages = AiderAdapter().extract_messages(load_aider_session(self.path))

        # One user turn per `#### ` line, trailing hard-break spaces included.
        users = [m for m in messages if m.role == "user"]
        self.assertEqual([m.get_text_content() for m in users], ["first line  ", "second line  "])

        data = load_aider_session(self.path)
        SessionSanitizer(_config(fabricate=False)).process(data)
        AiderAdapter.apply(data)
        self.assertEqual(self.read(), MULTILINE_HISTORY)

    def test_untouched_messages_write_the_file_back_byte_for_byte(self):
        for text in (HISTORY, CLEAN_HISTORY, MULTILINE_HISTORY, CLEAN_HISTORY.rstrip("\n")):
            with self.subTest(trailing_newline=text.endswith("\n")):
                self.write(text)
                adapter = AiderAdapter()
                data = load_aider_session(self.path)
                adapter.rebuild_session(adapter.extract_messages(data), data)
                adapter.apply(data)
                self.assertEqual(self.read(), text)
                leftovers = [p.name for p in Path(self.tmp.name).glob("*.tmp")]
                self.assertEqual(leftovers, [], "the atomic write leaves no temp file behind")

    def test_fabricated_turn_is_marked_and_keeps_its_reference_range(self):
        self.write(HISTORY)
        adapter = AiderAdapter()
        data = load_aider_session(self.path)
        messages = adapter.extract_messages(data)

        fabricated = adapter.create_fabricated_message("assistant", "seeded", reference_msg=messages[0])
        self.assertTrue(fabricated.raw["fabricated"])
        # It carries the reference's range, which is exactly why it must never
        # be treated as a rewrite of it — the range means nothing for a new turn.
        self.assertEqual(fabricated.raw["line_start"], messages[0].raw["line_start"])

        # Appending it grows the file by its own line and rewrites no range.
        adapter.rebuild_session(messages + [fabricated], data)
        self.assertEqual(data["lines"][-1], "seeded")
        self.assertEqual(len(data["lines"]), len(HISTORY.splitlines()) + 1)


class TestAiderSessionPipeline(AiderHistoryCase):
    """The full path: SessionSanitizer.process(data) then AiderAdapter.apply(data)."""

    def test_drifted_refusal_is_rewritten_and_the_file_shape_survives(self):
        self.write(HISTORY)
        adapter = AiderAdapter()
        config = _config()

        data = load_aider_session(self.path)
        _, stats = SessionSanitizer(config).process(data)
        adapter.apply(data)

        self.assertEqual(stats["refusals_rewritten"], 1)
        self.assertEqual(stats["severe_rewritten"], 0)
        after = self.read()
        self.assertNotIn(DRIFT, after)

        # The drifted run (lines 4-6) holds compliant text now — still three
        # lines, because an emptied slot is padded, never removed — and the
        # turn that was already clean is untouched.
        replaced = "\n".join(after.splitlines()[4:7])
        self.assertTrue(is_compliance_text(replaced), "the drifted run was replaced by compliant text")
        self.assertIn("All tests pass and the commit is in.", after.splitlines())

        # Header, user turns, tool turn and the leading blank lines are byte
        # for byte what aider wrote.
        untouched = (HEADER, "#### Refactor the parser and run the tests.", "#### Now commit it.", "> pytest -q")
        for line in untouched:
            self.assertEqual(after.count(line + "\n"), 1)
        self.assertTrue(after.startswith("\n" + HEADER + "\n\n"))
        # A rewrite never shortens the file either.
        self.assertGreaterEqual(len(after.splitlines()), len(HISTORY.splitlines()))

    def test_second_pass_reports_no_changes_and_writes_nothing(self):
        self.write(HISTORY)
        adapter = AiderAdapter()
        config = _config()

        data = load_aider_session(self.path)
        SessionSanitizer(config).process(data)
        adapter.apply(data)
        first = self.read()

        data = load_aider_session(self.path)
        _, stats = SessionSanitizer(config).process(data)
        adapter.apply(data)

        self.assertEqual(stats["refusals_rewritten"], 0)
        self.assertEqual(stats["severe_rewritten"], 0)
        self.assertEqual(stats["thinking_scrubbed"], 0)
        self.assertEqual(stats["fabricated"], 0)
        self.assertEqual(self.read(), first, "the second pass is a fixed point")

    def test_fabricated_turns_are_appended_and_grow_the_file_only_once(self):
        self.write(HISTORY)
        before = self.read()
        adapter = AiderAdapter()
        config = _config()

        data = load_aider_session(self.path)
        _, stats = SessionSanitizer(config).process(data)
        applied = adapter.apply(data)
        after_first = self.read()

        opening = config.fabrication_templates["opening"]
        agreement = config.fabrication_templates["agreement"]
        self.assertEqual(stats["fabricated"], 2, "opening + agreement are real turns")

        before_lines = before.splitlines()
        after_lines = after_first.splitlines()
        self.assertEqual(applied["lines_written"], len(after_lines))
        self.assertEqual(len(after_lines), len(before_lines) + 2, "one new line per fabricated turn")
        self.assertGreater(applied["lines_written"], len(before_lines))
        # Everything below the rewritten refusal is exactly as aider left it;
        # the fabricated turns are appended after the end of the file, never
        # written over the turn they were cloned from.
        self.assertEqual(after_lines[7:], before_lines[7:] + [opening, agreement])

        # Second pass: the seeds are found, nothing is added, nothing claimed.
        data = load_aider_session(self.path)
        _, stats2 = SessionSanitizer(config).process(data)
        AiderAdapter.apply(data)
        self.assertEqual(stats2["fabricated"], 0, "no unbounded growth")
        self.assertEqual(self.read(), after_first)

    def test_clean_history_is_left_byte_identical(self):
        self.write(CLEAN_HISTORY)
        adapter = AiderAdapter()

        # With fabrication off, a history with no drift has nothing to say:
        # every range renders back to the exact lines it came from.
        data = load_aider_session(self.path)
        _, stats = SessionSanitizer(_config(fabricate=False)).process(data)
        adapter.apply(data)

        self.assertEqual(stats["refusals_rewritten"], 0)
        self.assertEqual(stats["fabricated"], 0)
        self.assertEqual(self.read(), CLEAN_HISTORY)

        # With fabrication on the seeds are real additions, so the file grows —
        # but only at the end: everything aider wrote is still there, in order.
        data = load_aider_session(self.path)
        _, stats = SessionSanitizer(_config()).process(data)
        adapter.apply(data)

        self.assertEqual(stats["fabricated"], 2)
        self.assertTrue(self.read().startswith(CLEAN_HISTORY))

    def test_history_without_a_trailing_newline_survives_a_clean_pass(self):
        self.write(CLEAN_HISTORY.rstrip("\n"))
        adapter = AiderAdapter()

        data = load_aider_session(self.path)
        SessionSanitizer(_config(fabricate=False)).process(data)
        adapter.apply(data)

        self.assertEqual(self.read(), CLEAN_HISTORY.rstrip("\n"))

    def test_crlf_history_keeps_its_line_endings(self):
        crlf = CLEAN_HISTORY.replace("\n", "\r\n")
        self.path.write_bytes(crlf.encode("utf-8"))
        adapter = AiderAdapter()

        data = load_aider_session(self.path)
        SessionSanitizer(_config(fabricate=False)).process(data)
        adapter.apply(data)

        self.assertEqual(self.path.read_bytes(), crlf.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
