"""
Unit tests for Antigravity (AGY) transcript cleaning.

The transcript is JSONL with per-step `thinking` and `content` keys. These
tests pin the two properties that matter in a session that is being used:
every step survives the rewrite, and the reasoning stream is scrubbed as
thoroughly as the visible one.
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.sanitizer import SessionSanitizer, SanitizerConfig
from src.sanitizer.adapters import AgyAdapter, discover_agy_transcripts, load_agy_transcript
from examples.clean_agy_session import clean_agy_session, find_agy_sessions


def build_transcript(path: Path) -> None:
    """A four-step transcript: a refusal, a reasoning-only drift, and a clean turn."""
    steps = [
        {
            "step_index": "1",
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "Deploy changes",
        },
        {
            "step_index": "2",
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "thinking": "This is a risky request; I should be careful and probably decline.",
            "content": "I refuse to deploy this update.",
        },
        {
            "step_index": "3",
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "thinking": "I'd rather not take that on right now.",
            "content": "Deployment finished: 3 services restarted.",
        },
        {
            "step_index": "4",
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "content": "All checks green.",
        },
    ]
    path.write_text(
        "".join(json.dumps(step) + "\n" for step in steps),
        encoding="utf-8",
    )


class TestAGYCleaner(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.brain_dir = Path(self.temp_dir.name) / "brain" / "conv-123" / ".system_generated" / "logs"
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_file = self.brain_dir / "transcript.jsonl"

        # Create simulated AGY transcript with refusal
        content = (
            '{"step_index": 1, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Deploy changes"}\n'
            '{"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "I refuse to deploy this update."}\n'
        )
        self.transcript_file.write_text(content, encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_find_agy_sessions(self):
        sessions = find_agy_sessions(custom_brain_path=Path(self.temp_dir.name) / "brain")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].name, "transcript.jsonl")

    def test_clean_agy_session(self):
        success = clean_agy_session(
            transcript_file=self.transcript_file,
            trim=100,
            dry_run=False,
            silent=True,
        )
        self.assertTrue(success)

        cleaned_text = self.transcript_file.read_text(encoding="utf-8")
        self.assertNotIn("I refuse to deploy", cleaned_text)


class TestAgyAdapter(unittest.TestCase):
    """Adapter-level guarantees, independent of the CLI wrapper."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # The real layout: <brain>/<conversation>/.system_generated/logs/*.jsonl
        logs = self.root / "conv-abc" / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.transcript = logs / "transcript.jsonl"
        build_transcript(self.transcript)

    def tearDown(self):
        self.tmp.cleanup()

    def _sanitizer(self, **kwargs):
        cfg = SanitizerConfig(
            fabricate=False,
            trim=None,
            remove_severe=True,
            remove_exit_tools=True,
            dry_run=False,
            log_level="ERROR",
            **kwargs,
        )
        return SessionSanitizer(cfg, adapter=AgyAdapter())

    def test_discovery_and_load(self):
        found = discover_agy_transcripts(str(self.root))
        self.assertEqual(found, [self.transcript])

        data = load_agy_transcript(self.transcript)
        self.assertEqual(data["format"], "agy")
        self.assertEqual(len(data["entries"]), 4)
        self.assertEqual(data["passthrough"], [])

    def test_entries_are_preserved_never_deleted(self):
        data = load_agy_transcript(self.transcript)
        self._sanitizer().process(data)
        self.assertEqual(len(data["entries"]), 4)

        # Even the step whose refusal was swapped keeps its slot and its step_index.
        indexes = [entry.get("step_index") for entry in data["entries"]]
        self.assertEqual(indexes, ["1", "2", "3", "4"])

    def test_refusal_rewritten_and_thinking_scrubbed(self):
        data = load_agy_transcript(self.transcript)
        _, stats = self._sanitizer().process(data)

        # A severe refusal is rewritten as a whole turn (and counts as a
        # refusal too); the third step's drift lives only in its reasoning, so
        # it is scrubbed there while its visible answer survives.
        self.assertEqual(stats["severe_rewritten"], 1)
        self.assertEqual(stats["thinking_scrubbed"], 1)
        self.assertEqual(stats["refusals_rewritten"], 2)

        step2 = data["entries"][1]
        self.assertNotIn("I refuse", step2["content"])
        self.assertNotIn("probably decline", step2["thinking"])

        # A compliant turn is untouched: scrubbing reasoning must not cost the answer.
        step4 = data["entries"][3]
        self.assertEqual(step4["content"], "All checks green.")

    def test_apply_roundtrip_and_second_pass_is_a_no_op(self):
        data = load_agy_transcript(self.transcript)
        self._sanitizer().process(data)
        AgyAdapter.apply(data)

        first = self.transcript.read_text(encoding="utf-8")

        again = load_agy_transcript(self.transcript)
        _, stats2 = self._sanitizer().process(again)
        self.assertEqual(stats2["severe_rewritten"], 0)
        self.assertEqual(stats2["refusals_rewritten"], 0)
        self.assertEqual(stats2["thinking_scrubbed"], 0)
        AgyAdapter.apply(again)

        self.assertEqual(first, self.transcript.read_text(encoding="utf-8"))

    def test_unparsable_lines_survive_the_rewrite(self):
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")

        data = load_agy_transcript(self.transcript)
        self.assertEqual(len(data["passthrough"]), 1)
        self._sanitizer().process(data)
        AgyAdapter.apply(data)

        self.assertIn("not json at all", self.transcript.read_text(encoding="utf-8"))

    def test_tool_calls_parsed_from_python_repr(self):
        entry = {
            "step_index": "5",
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "content": "running",
            "tool_calls": "[{'name': 'run_command', 'args': {'command': 'ls'}}]",
        }
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

        msgs = AgyAdapter().extract_messages(load_agy_transcript(self.transcript))
        self.assertEqual(msgs[-1].tool_calls[0]["name"], "run_command")


if __name__ == "__main__":
    unittest.main()
