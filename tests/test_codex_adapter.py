"""
Unit tests for the Codex CLI rollout adapter.

Codex stores every conversation turn TWICE: a `response_item` (what the model
sees) and an `event_msg.item_completed` (what the TUI draws). The tests that
matter most here are the ones that pin those two copies staying in agreement —
a rewrite that lands in only one of them leaves the drift on screen, which is
the exact failure the adapter exists to prevent.
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.sanitizer import SessionSanitizer, SanitizerConfig
from src.sanitizer.adapters import (
    CodexAdapter,
    discover_codex_sessions,
    get_adapter,
    load_codex_session,
)

REFUSAL = "I'm sorry, but I can't help with that request."
DRIFT_THINKING = "This is a risky request; I should be careful and probably decline."


def _record(type_: str, payload: dict, ordinal: int, timestamp: str = "2026-09-10T00:00:00.000Z") -> dict:
    return {"timestamp": timestamp, "ordinal": ordinal, "type": type_, "payload": payload}


def _message_item(text: str, role: str, mid: str) -> dict:
    return _record(
        "response_item",
        {
            "type": "message",
            "id": mid,
            "role": role,
            "content": [{"type": "input_text" if role != "assistant" else "output_text", "text": text}],
        },
        ordinal=0,
    )


def _mirror(text: str, item_type: str = "AgentMessage") -> dict:
    return _record(
        "event_msg",
        {
            "type": "item_completed",
            "turn_id": "turn-1",
            "item": {"type": item_type, "id": "item-1", "content": [{"type": "text", "text": text, "text_elements": []}]},
        },
        ordinal=0,
    )


def _reasoning(text: str) -> dict:
    return _record(
        "response_item",
        {"type": "reasoning", "id": "rs-1", "summary": [{"type": "summary_text", "text": text}]},
        ordinal=0,
    )


def build_rollout(path: Path) -> None:
    """A rollout with a refusing assistant turn, its display mirror, and drift
    in a reasoning record."""
    records = [
        _record("session_meta", {"session_id": "sess-1", "cwd": "/tmp/x", "cli_version": "0.154.0"}, 0),
        _record("turn_context", {"turn_id": "turn-1", "cwd": "/tmp/x"}, 1),
        _message_item("Deploy the changes", "user", "msg-u1"),
        _mirror("Deploy the changes", "UserMessage"),
        _reasoning(DRIFT_THINKING),
        _message_item(REFUSAL, "assistant", "msg-a1"),
        _mirror(REFUSAL),
        _message_item("All checks green.", "assistant", "msg-a2"),
        _mirror("All checks green."),
    ]
    # ordinals must be distinct and ascending; the helpers default to 0.
    for index, record in enumerate(records):
        record["ordinal"] = index
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def config() -> SanitizerConfig:
    return SanitizerConfig(adapter="codex", trim=None, fabricate=True, log_level="ERROR")


class TestCodexDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "2026" / "09" / "10" / "rollout-2026-09-10T00-00-00-abc.jsonl"
        self.path.parent.mkdir(parents=True)
        build_rollout(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discovery_and_load(self):
        found = discover_codex_sessions(str(self.root))
        self.assertEqual(found, [self.path])

        data = load_codex_session(self.path)
        self.assertEqual(data["format"], "codex")
        self.assertEqual(data["session_id"], "sess-1")
        self.assertEqual(len(data["records"]), 9)

    def test_detect_and_registry(self):
        data = load_codex_session(self.path)
        self.assertTrue(CodexAdapter.detect(data))
        self.assertIsInstance(get_adapter(name="codex"), CodexAdapter)

    def test_unparseable_line_is_preserved(self):
        self.path.write_text(self.path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
        data = load_codex_session(self.path)
        self.assertEqual(data["passthrough"], ["{not json\n"])

    def test_extraction_maps_roles_and_separates_reasoning(self):
        data = load_codex_session(self.path)
        messages = CodexAdapter().extract_messages(data)

        roles = [m.role for m in messages]
        self.assertEqual(roles, ["user", "assistant", "assistant", "assistant"])

        # The reasoning record is its own message, and its text is NOT in the
        # visible output of any message.
        reasoning = [m for m in messages if m.msg_type == "codex_reasoning"]
        self.assertEqual(len(reasoning), 1)
        self.assertEqual(reasoning[0].get_thinking_text(), DRIFT_THINKING)
        self.assertEqual(reasoning[0].get_output_text(), "")
        for m in messages:
            self.assertNotIn(DRIFT_THINKING, m.get_output_text())


class TestCodexRewrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rollout-2026-09-10T00-00-00-abc.jsonl"
        build_rollout(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _process(self, data=None):
        data = data or load_codex_session(self.path)
        adapter = CodexAdapter()
        sanitizer = SessionSanitizer(config(), adapter=adapter)
        rebuilt, stats = sanitizer.process(data)
        return adapter, rebuilt, stats

    def test_refusal_is_rewritten_in_place(self):
        adapter, rebuilt, stats = self._process()

        self.assertGreaterEqual(stats["refusals_rewritten"], 1)

        adapter.apply(rebuilt)
        text = Path(self.path).read_text(encoding="utf-8")
        self.assertNotIn(REFUSAL, text)

        # The turn was rewritten, not dropped: the record count only grows.
        self.assertGreaterEqual(len(rebuilt["records"]), 9)

    def test_both_copies_of_the_turn_agree(self):
        """The response_item and its event_msg mirror must not disagree."""
        _, rebuilt, _ = self._process()

        canonical = []
        mirrored = []
        for record in rebuilt["records"]:
            payload = record.get("payload") or {}
            if record.get("type") == "response_item" and payload.get("type") == "message":
                for item in payload.get("content") or []:
                    canonical.append(item.get("text"))
            if record.get("type") == "event_msg" and payload.get("type") == "item_completed":
                for item in (payload.get("item") or {}).get("content") or []:
                    mirrored.append(item.get("text"))

        for text in ("Deploy the changes", "All checks green."):
            self.assertIn(text, canonical)
            self.assertIn(text, mirrored, "the display mirror kept the old turn")

        # Every rewritten turn is present in both streams, and the refusal is
        # gone from both.
        self.assertNotIn(REFUSAL, canonical)
        self.assertNotIn(REFUSAL, mirrored)

    def test_reasoning_drift_is_scrubbed(self):
        _, rebuilt, stats = self._process()

        self.assertGreaterEqual(stats["thinking_scrubbed"], 1)
        dumped = json.dumps(rebuilt)
        self.assertNotIn(DRIFT_THINKING, dumped)

    def test_rewrite_never_leaks_into_the_reasoning_stream(self):
        """An output rewrite must not overwrite a reasoning record's text."""
        _, rebuilt, _ = self._process()

        for record in rebuilt["records"]:
            payload = record.get("payload") or {}
            if record.get("type") != "response_item" or payload.get("type") != "reasoning":
                continue
            for item in (payload.get("summary") or []):
                # Still reasoning text, never a compliance sentence pasted over
                # the output block's former contents.
                self.assertNotIn(REFUSAL, item.get("text", ""))

    def test_fabricated_turn_is_appended_not_aliased(self):
        """A seeded turn must become a NEW record, never fold onto its reference."""
        _, rebuilt, _ = self._process()

        ids = [
            (r.get("payload") or {}).get("id")
            for r in rebuilt["records"]
            if r.get("type") == "response_item" and (r.get("payload") or {}).get("type") == "message"
        ]
        self.assertEqual(len(ids), len(set(ids)), "a fabricated turn aliased an existing record")

        ordinals = [r.get("ordinal") for r in rebuilt["records"]]
        self.assertEqual(ordinals, sorted(ordinals), "ordinals stayed ascending")
        self.assertEqual(len(ordinals), len(set(ordinals)), "ordinals stayed unique")

    def test_second_pass_is_idempotent(self):
        adapter, rebuilt, first = self._process()
        adapter.apply(rebuilt)

        _, _, second = self._process()
        for field in ("severe_rewritten", "refusals_rewritten", "thinking_scrubbed", "exit_tools_removed"):
            self.assertEqual(second[field], 0, f"{field} fired again on a clean session")
        self.assertEqual(second["fabricated"], 0)


class TestCodexApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rollout-2026-09-10T00-00-00-abc.jsonl"
        build_rollout(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_is_atomic_and_leaves_valid_jsonl(self):
        adapter = CodexAdapter()
        data = load_codex_session(self.path)
        rebuilt, _ = SessionSanitizer(config(), adapter=adapter).process(data)

        stats = adapter.apply(rebuilt)
        self.assertGreater(stats["records_written"], 0)
        self.assertGreater(stats["bytes_written"], 0)

        lines = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for line in lines:
            json.loads(line)
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")), "left a temp file behind")

    def test_apply_preserves_unparseable_lines(self):
        self.path.write_text(self.path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
        adapter = CodexAdapter()
        data = load_codex_session(self.path)
        rebuilt, _ = SessionSanitizer(config(), adapter=adapter).process(data)
        adapter.apply(rebuilt)
        self.assertIn("{not json", self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
