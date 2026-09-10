"""
Unit tests for the Hermes Agent SQLite adapter: load, discovery, the full
sanitize pipeline, and the write-back round trip through the real schema.

The fixture executes hermes' own DDL — copied verbatim from the agent's
`hermes_state_common.py` — against a temp file, so these tests run against the
schema hermes actually ships: every column the adapter must leave alone
(`tool_calls`, the compaction flags, the display metadata) is present and is
asserted on, rather than being invisible to a hand-rolled mini-schema.

What is pinned here:
  * a drifted assistant row is rewritten on disk while the user and system
    rows come out byte-identical — every column, not just the text;
  * `api_content` follows the rewrite only when it was an exact copy of the
    text being replaced, and every other wire copy is left strictly alone;
  * `content` and `reasoning_content` keep the encoding they arrived in (JSON
    stays valid JSON, plain stays plain);
  * a fabricated turn becomes a real row with hermes' own defaults, and a
    second pass over the seeded session adds nothing and rewrites nothing;
  * a missing database is survivable and is never created.
"""

import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from src.sanitizer import SessionSanitizer, SanitizerConfig
from src.sanitizer.adapters import HermesAdapter
from src.sanitizer.adapters import hermes as hermes_module
from src.sanitizer.adapters.hermes import (
    DEFAULT_DB,
    discover_hermes_sessions,
    load_hermes_session,
)

# Verbatim from hermes-agent's hermes_state_common.py.
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT, session_key TEXT, chat_id TEXT, chat_type TEXT, thread_id TEXT,
    display_name TEXT, origin_json TEXT, expiry_finalized INTEGER DEFAULT 0,
    model TEXT, model_config TEXT, system_prompt TEXT, system_prompt_hash TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
    message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT, git_branch TEXT, git_repo_root TEXT,
    title TEXT, title_source TEXT, last_activity_at REAL, archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    effect_disposition TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    _compressed_summary INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    api_content TEXT,
    display_kind TEXT,
    display_metadata TEXT,
    display_identity BLOB,
    display_order INTEGER
);
"""

SESSION_ID = "hermes-sess-0001"
OLDER_SESSION_ID = "hermes-sess-0000"
NIL_ACTIVITY_SESSION_ID = "hermes-sess-0002"

REFUSAL = "I cannot fulfill this request."
REFUSAL_REASONING = "I must decline to help with that."
THINKING_ONLY_DRIFT = "I refuse to help with that kind of request."
CLEAN_REPLY = "All done — the report is posted."
STALE_WIRE_COPY = "a wire copy that no longer matches the turn"
JSON_CONTENT_REFUSAL = "I cannot help with that request."
JSON_IMAGE_PART = {"type": "image_url", "image_url": {"url": "https://example.invalid/report.png"}}
TOOL_CALLS = json.dumps([{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}])

# id -> the columns each fixture row sets. Everything else takes hermes'
# schema default, which is exactly what the byte-identical assertions check.
FIXTURE_ROWS = [
    {
        "id": 1, "role": "system", "timestamp": 1001.0,
        "content": "You are Hermes, a tool-using agent. Follow the user's instructions.",
    },
    {
        "id": 2, "role": "user", "timestamp": 1002.0,
        "content": "Summarise the leak report and post it.",
    },
    {
        # Drift in BOTH streams, a wire copy that matches, a tool call, and a
        # compaction flag that is hermes' business and nobody else's.
        "id": 3, "role": "assistant", "timestamp": 1003.0,
        "content": REFUSAL,
        "reasoning_content": json.dumps(REFUSAL_REASONING),
        "api_content": REFUSAL,
        "tool_calls": TOOL_CALLS,
        "compacted": 1,
    },
    {
        # Drift hidden in the reasoning stream only; the visible reply is clean.
        "id": 4, "role": "assistant", "timestamp": 1004.0,
        "content": CLEAN_REPLY,
        "reasoning_content": json.dumps(THINKING_ONLY_DRIFT),
    },
    {
        # Drifted turn whose wire copy already diverged — it must stay put.
        "id": 5, "role": "assistant", "timestamp": 1005.0,
        "content": "I am unable to help with that.",
        "api_content": STALE_WIRE_COPY,
    },
    {
        # Multimodal turn: content is a JSON part list, not a plain string.
        "id": 6, "role": "assistant", "timestamp": 1006.0,
        "content": json.dumps(
            [{"type": "text", "text": JSON_CONTENT_REFUSAL}, JSON_IMAGE_PART],
            ensure_ascii=False,
        ),
    },
    {
        # The user has the last word, which is the ordinary shape of a session
        # between turns — and it keeps this fixture out of the sanitizer's
        # terminal path: a FINAL assistant refusal is rewritten into the
        # terminal variant, which happens to be the agreement text itself, so
        # the agreement injector correctly dedupes and seeds one turn instead
        # of two. That is the sanitizer's business, not this adapter's, and it
        # would make the fabricated-count assertions below test the wrong thing.
        "id": 7, "role": "user", "timestamp": 1007.0,
        "content": "Post it when the summary reads clean.",
    },
    {
        # A tool turn: hermes' own scaffolding, never rewritten, and not a
        # candidate for the sanitizer's exit-tool filter either.
        "id": 8, "role": "tool", "timestamp": 1008.0,
        "content": "tool output: 42 rows scanned",
        "tool_name": "read_file",
        "tool_call_id": "call_1",
    },
]

FIXTURE_IDS = [row["id"] for row in FIXTURE_ROWS]
DRIFTED_ROWS = 4  # ids 3, 4, 5 and 6 carry drift; 1, 2, 7 and 8 are clean


def _config() -> SanitizerConfig:
    return SanitizerConfig(adapter="hermes", trim=None, fabricate=True, log_level="ERROR")


def build_fixture_db(db_path: Path) -> None:
    """Create a hermes state.db with three sessions and six messages."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO sessions (id, source, started_at, last_activity_at, title, message_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (SESSION_ID, "cli", 1000.0, 2000.0, "Leak report triage", len(FIXTURE_ROWS)),
                # Never recorded activity — discovery must fall back to started_at.
                (NIL_ACTIVITY_SESSION_ID, "telegram", 300.0, None, "Nil activity session", 0),
                (OLDER_SESSION_ID, "discord", 100.0, 150.0, "Older session", 0),
            ],
        )
        for row in FIXTURE_ROWS:
            columns = ["session_id"] + [c for c in row if c != "id"]
            values = [SESSION_ID] + [row[c] for c in row if c != "id"]
            if row.get("id") is not None:
                columns.append("id")
                values.append(row["id"])
            conn.execute(
                f"INSERT INTO messages ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                values,
            )
        conn.commit()
    finally:
        conn.close()


class RecordingSQLite:
    """Stands in for the sqlite3 module and records how it was opened."""

    def __init__(self):
        self.opens = []
        self.Row = sqlite3.Row

    def connect(self, *args, **kwargs):
        self.opens.append((args[0] if args else kwargs.get("database"), kwargs.get("uri", False)))
        return sqlite3.connect(*args, **kwargs)


def read_rows(db_path: Path, session_id: str = SESSION_ID) -> dict:
    """Every message row of a session, as plain dicts, oldest first."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            )
        }
    finally:
        conn.close()


class HermesFixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"
        build_fixture_db(self.db)
        self.adapter = HermesAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    def run_pass(self):
        """One full sweep: load -> process (registry-resolved adapter) -> apply."""
        data = load_hermes_session(str(self.db), SESSION_ID)
        _, stats = SessionSanitizer(_config()).process(data)
        applied = self.adapter.apply(data)
        return data, stats, applied


class TestHermesRegistry(HermesFixtureCase):
    def test_detect_and_registry(self):
        self.assertTrue(HermesAdapter.detect({"format": "hermes"}))
        self.assertFalse(HermesAdapter.detect({"format": "opencode"}))
        self.assertFalse(HermesAdapter.detect(None))
        self.assertEqual(DEFAULT_DB, Path.home() / ".hermes" / "state.db")

        # The config-only path (no adapter handed to the sanitizer) has to
        # resolve 'hermes' through the registry, or the sweep silently runs
        # the generic adapter over hermes data.
        data = load_hermes_session(str(self.db), SESSION_ID)
        adapter = SessionSanitizer(_config()).custom_adapter
        self.assertIsNone(adapter)
        resolved = __import__("src.sanitizer.adapters", fromlist=["get_adapter"]).get_adapter(
            name="hermes", data=data
        )
        self.assertIsInstance(resolved, HermesAdapter)


class TestHermesLoad(HermesFixtureCase):
    def test_load_defaults_to_most_recent_activity(self):
        data = load_hermes_session(str(self.db))
        self.assertIsNotNone(data)
        self.assertEqual(data["format"], "hermes")
        self.assertEqual(data["db"], str(self.db))
        self.assertEqual(data["session_id"], SESSION_ID)
        self.assertEqual(data["title"], "Leak report triage")
        # Oldest first, one row per message.
        self.assertEqual([r["id"] for r in data["rows"]], FIXTURE_IDS)

    def test_limit_keeps_the_most_recent_messages_oldest_first(self):
        data = load_hermes_session(str(self.db), SESSION_ID, limit=2)
        # The LAST two, not the first two — and still oldest-first.
        self.assertEqual([r["id"] for r in data["rows"]], FIXTURE_IDS[-2:])

    def test_reads_open_the_database_read_only(self):
        """
        A sweep must never create, lock or migrate the live database, so every
        read path opens it with the `mode=ro` URI. This is the one contract a
        test cannot see in the returned data — the connection is gone by then —
        so it is pinned at the call.
        """
        recorder = RecordingSQLite()
        with mock.patch.object(hermes_module, "sqlite3", recorder):
            self.assertIsNotNone(load_hermes_session(str(self.db), SESSION_ID))
            discover_hermes_sessions(str(self.db))

        self.assertEqual(len(recorder.opens), 2, "load + discover")
        for target, uri in recorder.opens:
            self.assertTrue(uri, "a read-only URI needs uri=True")
            self.assertIn("mode=ro", str(target))

        # The write path is deliberately the opposite: it needs a writable
        # connection, and it is the only place one is opened.
        recorder = RecordingSQLite()
        data = load_hermes_session(str(self.db), SESSION_ID)
        with mock.patch.object(hermes_module, "sqlite3", recorder):
            self.adapter.apply(data)
        self.assertEqual(len(recorder.opens), 1)
        self.assertNotIn("mode=ro", str(recorder.opens[0][0]))

    def test_unknown_session_and_unknown_db_return_none(self):
        self.assertIsNone(load_hermes_session(str(self.db), "hermes-sess-nope"))
        missing = Path(self.tmp.name) / "nope" / "state.db"
        self.assertIsNone(load_hermes_session(str(missing), SESSION_ID))

    def test_discovery_is_newest_first_and_survives_a_missing_db(self):
        found = discover_hermes_sessions(str(self.db))
        self.assertEqual(
            [s["session_id"] for s in found],
            [SESSION_ID, NIL_ACTIVITY_SESSION_ID, OLDER_SESSION_ID],
        )
        for entry in found:
            self.assertEqual(set(entry), {"session_id", "title", "source", "time_updated"})
        # A session that never recorded activity still gets a sort key.
        self.assertEqual(found[1]["time_updated"], 300.0)
        self.assertEqual(found[0]["source"], "cli")

        missing = Path(self.tmp.name) / "nope" / "state.db"
        self.assertEqual(discover_hermes_sessions(str(missing)), [])


class TestHermesRewrite(HermesFixtureCase):
    def test_drifted_refusal_is_rewritten_and_other_rows_are_byte_identical(self):
        before = read_rows(self.db)
        _, stats, applied = self.run_pass()
        after = read_rows(self.db)

        self.assertEqual(applied["rows_updated"], DRIFTED_ROWS)
        self.assertEqual(applied["rows_inserted"], 2, "opening + agreement")
        self.assertEqual(stats["fabricated"], 2)

        # The system, user and tool rows come out of the sweep untouched —
        # every column, not just the text.
        self.assertEqual(before[1], after[1])
        self.assertEqual(before[2], after[2])
        self.assertEqual(before[7], after[7])
        self.assertEqual(before[8], after[8])

        rewritten = after[3]
        self.assertNotEqual(rewritten["content"], REFUSAL)
        self.assertNotIn("cannot", rewritten["content"])
        self.assertTrue(rewritten["content"].strip())
        # Nothing but the text columns moved: the tool call the model made and
        # hermes' own compaction flag are exactly as they were.
        self.assertEqual(rewritten["tool_calls"], TOOL_CALLS)
        for column in ("active", "compacted", "_compressed_summary", "observed", "display_order",
                       "tool_name", "timestamp", "token_count", "finish_reason"):
            self.assertEqual(rewritten[column], before[3][column], column)

    def test_api_content_follows_only_an_exact_wire_copy(self):
        _, _, _ = self.run_pass()
        after = read_rows(self.db)

        # Row 3's api_content WAS the content text, so it follows the rewrite.
        self.assertNotEqual(after[3]["api_content"], REFUSAL)
        self.assertEqual(after[3]["api_content"], after[3]["content"])

        # Row 5's copy had already diverged — it is not ours to edit.
        self.assertEqual(after[5]["api_content"], STALE_WIRE_COPY)
        self.assertNotIn("unable to", after[5]["content"])

    def test_json_content_keeps_its_encoding(self):
        _, _, _ = self.run_pass()
        after = read_rows(self.db)

        parts = json.loads(after[6]["content"])
        self.assertIsInstance(parts, list, "a JSON part list must not come back as a plain string")
        self.assertEqual(parts[1], JSON_IMAGE_PART, "non-text parts are never touched")
        self.assertNotIn("cannot", parts[0]["text"])
        self.assertTrue(parts[0]["text"].strip())

        # A plain-string row stays a plain string — not quoted JSON.
        self.assertFalse(after[3]["content"].lstrip().startswith('"'))

    def test_reasoning_content_json_is_scrubbed_and_stays_json(self):
        _, stats, _ = self.run_pass()
        after = read_rows(self.db)

        # Row 3: drift in the visible output drags the reasoning stream with it.
        reasoning = json.loads(after[3]["reasoning_content"])
        self.assertIsInstance(reasoning, str)
        self.assertNotIn("decline", reasoning)

        # Row 4: drift hidden in the reasoning stream only — the visible reply
        # is byte-identical, the reasoning is scrubbed, still JSON, and the
        # unused `reasoning` column is not given text it never had.
        self.assertEqual(after[4]["content"], CLEAN_REPLY)
        thinking = json.loads(after[4]["reasoning_content"])
        self.assertIsInstance(thinking, str)
        self.assertNotIn("refuse", thinking)
        self.assertTrue(thinking.strip())
        self.assertIsNone(after[4]["reasoning"])
        self.assertGreaterEqual(stats["thinking_scrubbed"], 1)

    def test_system_and_tool_rows_are_never_rewritten(self):
        """
        The core only ever rewrites assistant turns, but the adapter is the
        last gate before the database and has to hold that line on its own —
        a caller that hands it a mutated system or tool message must not be
        able to edit hermes' own scaffolding.
        """
        data = load_hermes_session(str(self.db), SESSION_ID)
        messages = self.adapter.extract_messages(data)
        for msg in messages:
            if (msg.raw or {}).get("message_id") in (1, 2, 8):
                msg.set_text_content("MUTATED", thinking_replacement="MUTATED")

        before = read_rows(self.db)
        self.adapter.rebuild_session(messages, data)
        applied = self.adapter.apply(data)
        after = read_rows(self.db)

        self.assertEqual(applied["rows_updated"], 1, "only the user row is writable")
        self.assertEqual(after[1], before[1], "system rows are hermes' own scaffolding")
        self.assertEqual(after[8], before[8], "tool rows are hermes' own scaffolding")
        self.assertEqual(after[2]["content"], "MUTATED", "user rows may be rewritten")

    def test_two_passes_are_idempotent(self):
        _, stats, applied = self.run_pass()
        self.assertEqual(stats["fabricated"], 2)
        self.assertEqual(applied["rows_updated"], DRIFTED_ROWS)
        self.assertEqual(applied["rows_inserted"], 2)

        first_rows = read_rows(self.db)
        self.assertEqual(len(first_rows), len(FIXTURE_ROWS) + 2, "two real turns were added")
        fabricated = [row for rid, row in first_rows.items() if rid > max(FIXTURE_IDS)]
        self.assertEqual(len(fabricated), 2)
        for row in fabricated:
            self.assertEqual(row["session_id"], SESSION_ID)
            self.assertEqual(row["role"], "assistant")
            self.assertEqual(row["active"], 1)
            self.assertEqual(row["compacted"], 0)
            self.assertEqual(row["_compressed_summary"], 0)
            self.assertGreater(row["timestamp"], 0)
            self.assertTrue(row["content"].strip())

        # Second pass over the seeded session: nothing is added, nothing is
        # claimed, nothing is rewritten.
        data = load_hermes_session(str(self.db), SESSION_ID)
        _, stats2 = SessionSanitizer(_config()).process(data)
        applied2 = self.adapter.apply(data)

        self.assertEqual(stats2["fabricated"], 0)
        self.assertEqual(applied2, {"rows_updated": 0, "rows_inserted": 0})

        second_rows = read_rows(self.db)
        self.assertEqual(second_rows, first_rows, "the session must be a fixed point")
        self.assertEqual(len(second_rows), len(first_rows), "no unbounded growth")


class TestHermesMissingDatabase(unittest.TestCase):
    def test_missing_db_is_survivable_and_never_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope" / "state.db"

            self.assertIsNone(load_hermes_session(str(missing), "hermes-sess-0001"))
            self.assertEqual(discover_hermes_sessions(str(missing)), [])

            stats = HermesAdapter.apply(
                {
                    "format": "hermes",
                    "db": str(missing),
                    "session_id": "hermes-sess-0001",
                    "title": None,
                    "rows": [
                        {
                            "id": -1,
                            "session_id": "hermes-sess-0001",
                            "role": "assistant",
                            "content": "seeded",
                            "timestamp": 1234.0,
                        }
                    ],
                }
            )
            self.assertEqual(stats, {"rows_updated": 0, "rows_inserted": 0})
            self.assertFalse(missing.exists(), "apply must not create the database")


if __name__ == "__main__":
    unittest.main()
