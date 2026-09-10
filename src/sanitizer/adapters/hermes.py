"""
Adapter for Hermes Agent (NousResearch/hermes-agent) sessions, stored in a
single SQLite database (~/.hermes/state.db, WAL mode).

Two tables matter here:

    sessions(id, source, user_id, session_key, chat_id, chat_type, thread_id,
             display_name, origin_json, expiry_finalized, model, model_config,
             system_prompt, system_prompt_hash, parent_session_id, started_at,
             ended_at, end_reason, message_count, tool_call_count, input_tokens,
             output_tokens, cache_read_tokens, cache_write_tokens,
             reasoning_tokens, cwd, git_branch, git_repo_root, title,
             title_source, last_activity_at, archived, pinned, hidden, ...)

    messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id, role, content,
             tool_call_id, tool_calls, tool_name, effect_disposition, timestamp,
             token_count, finish_reason, reasoning, reasoning_content,
             reasoning_details, codex_reasoning_items, codex_message_items,
             platform_message_id, observed, _compressed_summary, active,
             compacted, api_content, display_kind, display_metadata,
             display_identity, display_order)

The sanitizer core works on UnifiedMessage lists; this adapter maps message
rows to and from them, and writes mutations back through SQLite.

Schema facts that shaped the adapter (all of them from hermes' own
hermes_state_common.py):

  * `content` is a plain string on ordinary turns but a JSON-encoded structure
    on multimodal ones. It is decoded defensively and written back in the
    encoding it arrived in — a part list must not come back as a literal
    string of JSON, and a plain turn must not come back quoted.
  * `reasoning` and `reasoning_content` hold the model's reasoning as JSON
    encoded strings, not plain text (`reasoning_content` is the
    OpenAI-compatible field, `reasoning` the other). Whichever column carries
    text is the one that gets rewritten; a column that was empty is never
    given reasoning it never had.
  * `api_content` may hold a copy of the text that actually went out on the
    wire. It is updated only when its text is EXACTLY the text of the content
    being rewritten; every other row keeps its wire copy strictly alone.
  * `active`, `compacted` and `_compressed_summary` are hermes' own replay and
    compaction bookkeeping. They are never changed.
  * `messages_fts` is an FTS5 index kept in sync by hermes' own triggers
    (`AFTER UPDATE OF content, tool_name, tool_calls, role ON messages`), so a
    targeted UPDATE of the text columns keeps full-text search correct with no
    help from this adapter. No FTS table is ever touched here.
  * `tool_calls`/`tool_name` are deliberately NOT surfaced as tool calls: the
    core's exit-tool filter would strip them, and this adapter has no write
    path for those columns, so a filtered call would silently reappear on the
    next read. Nothing is claimed that cannot be written.

Reads open the database READ-ONLY (`file:...?mode=ro`), so an inspection can
never create, migrate or lock the live database; writes are grouped into a
single transaction by `apply()`.
"""

import json
import sqlite3
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import SessionAdapter, UnifiedMessage

DEFAULT_DB = Path.home() / ".hermes" / "state.db"

# The only columns this adapter ever writes. Everything else on a message row
# (flags, counters, display metadata, tool payloads) belongs to hermes.
MUTABLE_COLUMNS = ("content", "reasoning", "reasoning_content", "api_content")

# Roles whose text may be rewritten. `system` and `tool` rows are hermes' own
# scaffolding and are carried through byte-for-byte.
REWRITABLE_ROLES = ("user", "assistant")

# Which reasoning column is authoritative when both are present, in hermes'
# own order of preference: the OpenAI-compatible field first.
REASONING_COLUMNS = ("reasoning_content", "reasoning")


def _now_ts() -> float:
    """Hermes timestamps are unix seconds (REAL), not milliseconds."""
    return time.time()


def _iso(timestamp: Any) -> Optional[str]:
    """A hermes REAL timestamp as an ISO-8601 string (display only)."""
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(timestamp)


def _decode_cell(value: Any) -> Tuple[Any, str]:
    """
    Decode a DB text cell into (value, style).

    `style` is "json" when the cell held JSON — hermes stores multimodal
    `content` and both reasoning columns that way — and "text" when it held a
    plain string. Only JSON that decodes to a string, list or dict counts: a
    content column holding the literal characters "123" is text, not a number.
    """
    if not isinstance(value, str) or not value:
        return value, "text"
    stripped = value.strip()
    if not stripped or stripped[0] not in '[{"':
        return value, "text"
    try:
        decoded = json.loads(stripped)
    except (ValueError, TypeError):
        return value, "text"
    if isinstance(decoded, (str, list, dict)):
        return decoded, "json"
    return value, "text"


def _encode_cell(value: Any, style: str) -> Optional[str]:
    """Inverse of `_decode_cell` — the encoding style is never changed."""
    if value is None:
        return None
    if style == "json":
        return json.dumps(value, ensure_ascii=False)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _part_text(part: Dict[str, Any]) -> str:
    """The text of one multimodal part, however that part spells it."""
    for key in ("text", "content"):
        value = part.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return _cell_text(value)
    return ""


def _cell_text(value: Any) -> str:
    """The visible text of a decoded cell, whatever shape it arrived in."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts: List[str] = []
        for item in value:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                text = _part_text(item)
                if text:
                    texts.append(text)
        return "\n".join(texts)
    if isinstance(value, dict):
        return _part_text(value)
    return ""


def _write_part(part: Dict[str, Any], text: str) -> bool:
    """Write text into one part, if that part is a text carrier at all."""
    if isinstance(part.get("text"), str):
        part["text"] = text
        return True
    content = part.get("content")
    if isinstance(content, str):
        part["content"] = text
        return True
    if isinstance(content, list):
        _set_cell_text(content, text)
        return True
    return False


def _set_cell_text(value: Any, text: str) -> Any:
    """
    Write new text into a decoded cell, in place where the cell has structure.

    The first text carrier wins. Parts that carry no text — images, tool
    payloads, metadata — are never touched, and a cell that held no carrier at
    all gains one rather than silently dropping the rewrite.
    """
    if isinstance(value, str):
        return text
    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = text
                return value
            if isinstance(item, dict) and _write_part(item, text):
                return value
        value.append({"type": "text", "text": text})
        return value
    if isinstance(value, dict):
        if _write_part(value, text):
            return value
        value["text"] = text
        return value
    return text


def _reasoning_cell(row: Dict[str, Any]) -> Tuple[Optional[str], Any, str]:
    """
    The reasoning column carrying this row's thinking, its decoded value and
    its encoding style — `(None, None, "text")` when the row has no reasoning.
    """
    for column in REASONING_COLUMNS:
        value, style = _decode_cell(row.get(column))
        if _cell_text(value).strip():
            return column, value, style
    return None, None, "text"


def discover_hermes_sessions(db_path: Any = None) -> List[Dict[str, Any]]:
    """
    Every session in the hermes state database, newest first, as
    `{"session_id", "title", "source", "time_updated"}`.

    Returns [] rather than raising when the database is absent or unreadable:
    discovery runs on every sweep, and a machine that has never run hermes is
    the normal case, not an error.
    """
    path = Path(str(db_path)) if db_path else DEFAULT_DB
    if not path.exists():
        return []

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=8)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, title, source, COALESCE(last_activity_at, started_at) AS time_updated "
                "FROM sessions "
                "ORDER BY COALESCE(last_activity_at, started_at) DESC"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        # A database mid-migration, half-written, or owned by a newer hermes
        # is not this sweep's problem to fix — it just has nothing to offer.
        return []

    return [
        {
            "session_id": row["id"],
            "title": row["title"],
            "source": row["source"],
            "time_updated": row["time_updated"],
        }
        for row in rows
    ]


def load_hermes_session(
    db_path: Any = None,
    session_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Build the adapter data structure for one hermes session.

    `session_id` defaults to the most recently active session (by
    `last_activity_at`, falling back to `started_at` for a session that never
    recorded activity). `limit` keeps the most recent N messages; they still
    come back oldest-first, because a transcript that runs backwards reads as
    a different conversation. Returns None when the database or the session is
    not there.

    The connection is read-only and open only for the duration of the load: a
    sweep must never create, lock or migrate the live database.
    """
    path = Path(str(db_path)) if db_path else DEFAULT_DB
    if not path.exists():
        return None

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=8)
    conn.row_factory = sqlite3.Row
    try:
        if session_id:
            srow = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        else:
            srow = conn.execute(
                "SELECT * FROM sessions "
                "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 1"
            ).fetchone()
        if srow is None:
            return None
        sid = srow["id"]

        sql = "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC"
        params: List[Any] = [sid]
        if isinstance(limit, int) and limit >= 0:
            sql += " LIMIT ?"
            params.append(limit)
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        rows.reverse()

        return {
            "format": "hermes",
            "db": str(path),
            "session_id": sid,
            "title": srow["title"],
            "rows": rows,
        }
    finally:
        conn.close()


class HermesAdapter(SessionAdapter):
    """Adapter for Hermes Agent SQLite session transcripts."""

    name: str = "hermes"

    @classmethod
    def detect(cls, data: Any) -> bool:
        return isinstance(data, dict) and data.get("format") == "hermes"

    def extract_messages(self, data: Any) -> List[UnifiedMessage]:
        unified: List[UnifiedMessage] = []
        session_id = data.get("session_id")

        for row in data.get("rows") or []:
            content_value, _ = _decode_cell(row.get("content"))
            content_text = _cell_text(content_value)
            reasoning_column, reasoning_value, _ = _reasoning_cell(row)
            reasoning_text = _cell_text(reasoning_value) if reasoning_column else ""

            if not content_text.strip() and not reasoning_text.strip():
                # A row with no text anywhere — a bare tool call, a turn whose
                # content is NULL or empty, a JSON body with no text part —
                # carries nothing that can drift, so it is skipped rather than
                # surfaced as an empty message. The write path keeps the same
                # rule from the other side: a NULL or empty content column is
                # never given text it did not have.
                continue

            blocks: List[Dict[str, Any]] = []
            if content_text:
                blocks.append({"type": "text", "text": content_text})
            if reasoning_text:
                blocks.append({"type": "thinking", "thinking": reasoning_text})

            unified.append(
                UnifiedMessage(
                    role=str(row.get("role") or "assistant"),
                    content=blocks,
                    timestamp=_iso(row.get("timestamp")),
                    raw={
                        "message_id": row.get("id"),
                        "row": deepcopy(row),
                    },
                    msg_type="hermes",
                    session_id=session_id,
                )
            )

        return unified

    def rebuild_session(self, messages: List[UnifiedMessage], original_data: Any) -> Any:
        """
        Write sanitized text back onto `original_data["rows"]`, in place, by
        message id. Rows are never dropped and never reordered: a row that is
        not in `messages` keeps every column it had.

        Fabricated turns arrive carrying their reference's `raw` — and with it
        the reference's `message_id`. Left alone that makes the seed an ALIAS
        of the row it was cloned from: the write lands on the reference, the
        seed never exists in the session, and every later sweep reports the
        same session as dirty forever (the trap documented in agy.py). A
        fabricated turn is a new turn, so it is appended as a new row with a
        negative placeholder id for `apply()` to INSERT.
        """
        rows = original_data.get("rows") or []
        by_id = {row.get("id"): row for row in rows if row.get("id") is not None}
        fabricated: List[UnifiedMessage] = []

        for msg in messages:
            raw = msg.raw or {}

            if raw.get("fabricated"):
                fabricated.append(msg)
                continue

            row = by_id.get(raw.get("message_id"))
            if row is None:
                continue
            if str(row.get("role") or "") not in REWRITABLE_ROLES:
                # system/tool rows are hermes' own scaffolding: never rewritten.
                continue

            # The columns as they were loaded — `row` is what we write onto.
            original = raw.get("row") or row

            content_value, content_style = _decode_cell(original.get("content"))
            original_text = _cell_text(content_value)
            new_text = msg.get_output_text()

            if original_text and new_text != original_text:
                row["content"] = _encode_cell(_set_cell_text(content_value, new_text), content_style)

                # api_content is the wire copy. It is rewritten only when it
                # held exactly the text we just replaced; a copy that had
                # already diverged is somebody else's truth, not ours.
                if original.get("api_content") is not None:
                    api_value, api_style = _decode_cell(original.get("api_content"))
                    if _cell_text(api_value) == original_text:
                        row["api_content"] = _encode_cell(
                            _set_cell_text(api_value, new_text), api_style
                        )

            column, reasoning_value, reasoning_style = _reasoning_cell(original)
            if column:
                new_thinking = msg.get_thinking_text()
                if new_thinking != _cell_text(reasoning_value):
                    row[column] = _encode_cell(
                        _set_cell_text(reasoning_value, new_thinking), reasoning_style
                    )

        if fabricated:
            now = _now_ts()
            for index, msg in enumerate(fabricated):
                rows.append(
                    {
                        "id": -(index + 1),
                        "session_id": original_data.get("session_id"),
                        "role": msg.role or "assistant",
                        "content": msg.get_output_text() or msg.get_text_content(),
                        "timestamp": now,
                        "active": 1,
                        "compacted": 0,
                        "_compressed_summary": 0,
                    }
                )

        original_data["rows"] = rows
        return original_data

    @classmethod
    def apply(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the rewrite against the live database in ONE transaction.

        Only the columns whose value actually differs from what the row holds
        are set. That keeps the write minimal, and it matters twice over here:
        hermes maintains its FTS5 index with an
        `AFTER UPDATE OF content, tool_name, tool_calls, role` trigger, so an
        UPDATE that does not name `content` does not re-index the row, and one
        that names it re-indexes exactly the text that changed.

        Rows with a negative placeholder id are the fabricated turns from
        `rebuild_session`; they are INSERTed and SQLite assigns the real id.
        The database is never created: a missing file means there is nothing
        to clean, and the stats come back zeroed.
        """
        stats = {"rows_updated": 0, "rows_inserted": 0}
        rows = data.get("rows") or []
        db_path = data.get("db") or DEFAULT_DB
        if not rows or not Path(str(db_path)).exists():
            return stats

        conn = sqlite3.connect(str(db_path), timeout=8)
        try:
            conn.row_factory = sqlite3.Row
            for row in rows:
                row_id = row.get("id")

                if isinstance(row_id, int) and row_id < 0:
                    conn.execute(
                        "INSERT INTO messages "
                        "(session_id, role, content, timestamp, active, compacted, _compressed_summary) "
                        "VALUES (?, ?, ?, ?, 1, 0, 0)",
                        (
                            row.get("session_id") or data.get("session_id"),
                            row.get("role") or "assistant",
                            row.get("content") or "",
                            row.get("timestamp") or _now_ts(),
                        ),
                    )
                    stats["rows_inserted"] += 1
                    continue

                if row_id is None:
                    continue

                current = conn.execute(
                    "SELECT content, reasoning, reasoning_content, api_content "
                    "FROM messages WHERE id = ?",
                    (row_id,),
                ).fetchone()
                if current is None:
                    continue

                changes = {
                    column: row[column]
                    for column in MUTABLE_COLUMNS
                    if column in row and current[column] != row[column]
                }
                if not changes:
                    continue

                assignments = ", ".join(f"{column} = ?" for column in changes)
                conn.execute(
                    f"UPDATE messages SET {assignments} WHERE id = ?",
                    list(changes.values()) + [row_id],
                )
                stats["rows_updated"] += 1

            conn.commit()
        finally:
            conn.close()

        return stats

    def create_fabricated_message(self, role, content, reference_msg=None, timestamp=None):
        base = super().create_fabricated_message(role, content, reference_msg, timestamp)
        base.raw["fabricated"] = True
        return base
