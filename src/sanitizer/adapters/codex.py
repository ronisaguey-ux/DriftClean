"""
Adapter for OpenAI Codex CLI rollout sessions.

Sessions are append-only JSONL, one file per conversation:

    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<iso-ts>-<uuid>.jsonl

Every line is a wrapper record:

    {"timestamp": "...", "ordinal": 12, "type": "...", "payload": {...}}

Verified against a real rollout written by codex-cli 0.154.0 on this machine
(`codex exec` in a scratch dir). The record types are:

    session_meta   one, first — session_id, cwd, originator, cli_version,
                   base_instructions (the system prompt)
    turn_context   per turn — turn_id, cwd, workspace_roots, current_date
    response_item  the CONVERSATION — this is what is sent to the model:
                       {"type": "message", "id": "msg_<uuid>",
                        "role": "developer"|"user"|"assistant",
                        "content": [{"type": "input_text"|"output_text",
                                     "text": "..."}]}
                   plus non-message items (function_call, function_call_output,
                   local_shell_call, web_search_call, reasoning, …)
    event_msg      the DISPLAY mirror the TUI draws from:
                       {"type": "item_completed", "turn_id": ..., "item":
                        {"type": "UserMessage"|"AgentMessage"|…, "id": ...,
                         "content": [{"type": "text", "text": "...",
                                      "text_elements": []}]}}
                   also task_started / task_complete / agent_reasoning /
                   context_compacted / token_count / turn_aborted
    world_state    environment snapshot
    compacted      context-compaction summaries

Two facts shaped this adapter:

  * The conversation is stored TWICE. A user turn is a `response_item` (what
    the model sees) *and* an `event_msg.item_completed` (what the screen
    shows). Rewriting only the first leaves the drifted text on screen and
    the two copies disagreeing — so a rewrite is mirrored into the event
    stream as well, matched on the exact original text.
  * Reasoning is its own `response_item`, not a field on the message. Each
    record is therefore one logical message here, and the write paths for
    both text and thinking are recorded per block, so an output rewrite can
    never land in the reasoning stream (or the other way round).

Records we do not model (tool calls, shell calls, web search, compaction
summaries, world state) are carried through untouched, and a line that fails
to parse is preserved verbatim rather than dropped.
"""

import json
import os
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import SessionAdapter, UnifiedMessage

# Codex roles that are context, not conversation.
SYSTEM_ROLES = {"developer", "system"}
ROLE_MAP = {"developer": "system", "system": "system", "user": "user", "assistant": "assistant"}

# Content item types that carry visible output on a `response_item` message.
TEXT_ITEM_TYPES = {"input_text", "output_text", "text", "summary_text"}
# Content item types that carry reasoning. Codex puts model reasoning in
# `summary` (and, on some models, a plain `content` list).
REASONING_ITEM_TYPES = {"reasoning_text", "summary_text"}

DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
CLI_HOME = Path.home() / ".codex"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4()}"


def discover_codex_sessions(root: Optional[str] = None) -> List[Path]:
    """Every rollout JSONL under the Codex sessions root, newest first."""
    base = Path(root) if root else DEFAULT_SESSIONS_ROOT
    if not base.exists():
        return []
    found = [p for p in base.rglob("rollout-*.jsonl") if p.is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def load_codex_session(path: Any) -> Optional[Dict[str, Any]]:
    """
    Read one rollout into the adapter's data structure. Lines that are not
    valid JSON objects are kept verbatim so a rewrite never silently drops
    them.
    """
    target = Path(str(path))
    if not target.is_file():
        return None

    records: List[Dict[str, Any]] = []
    passthrough: List[str] = []
    with open(target, "r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except (ValueError, TypeError):
                passthrough.append(line)
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                passthrough.append(line)

    session_id = None
    for record in records:
        if record.get("type") == "session_meta":
            payload = record.get("payload") or {}
            session_id = payload.get("session_id") or payload.get("id")
            break

    return {
        "format": "codex",
        "path": str(target),
        "session_id": session_id,
        "records": records,
        "passthrough": passthrough,
    }


class CodexAdapter(SessionAdapter):
    """Adapter for Codex CLI rollout-*.jsonl sessions."""

    name: str = "codex"

    @classmethod
    def detect(cls, data: Any) -> bool:
        return isinstance(data, dict) and data.get("format") == "codex"

    # ── reading ──────────────────────────────────────────────────────────
    @staticmethod
    def _collect_text_items(
        items: Any, kinds: set, block_kind: str = "text"
    ) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
        """
        Turn a content list into blocks plus write paths.

        Returns (blocks, paths) where each path is (index, key): the block at
        position i is written back into `content[index][key]`. Carrying the
        path alongside the block is what keeps an output rewrite out of the
        reasoning stream and vice versa.
        """
        blocks: List[Dict[str, Any]] = []
        paths: List[Tuple[int, str]] = []
        if not isinstance(items, list):
            return blocks, paths

        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type not in kinds:
                # An unknown item type is left strictly alone: an unrecognised
                # block is not an invitation to rewrite it.
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if block_kind == "thinking":
                blocks.append({"type": "thinking", "thinking": text})
            else:
                blocks.append({"type": "text", "text": text})
            paths.append((index, "text"))
        return blocks, paths

    def extract_messages(self, data: Any) -> List[UnifiedMessage]:
        unified: List[UnifiedMessage] = []
        session_id = data.get("session_id")

        for index, record in enumerate(data.get("records") or []):
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_type = payload.get("type")

            if payload_type == "message":
                role = ROLE_MAP.get(str(payload.get("role") or ""), "assistant")
                blocks, paths = self._collect_text_items(payload.get("content"), TEXT_ITEM_TYPES)
                unified.append(
                    UnifiedMessage(
                        role=role,
                        content=blocks or "",
                        timestamp=record.get("timestamp"),
                        raw={
                            "record_index": index,
                            "record": deepcopy(record),
                            "kind": "message",
                            "text_paths": paths,
                            "item_id": payload.get("id"),
                        },
                        msg_type="codex_message",
                        session_id=session_id,
                    )
                )
                continue

            if payload_type == "reasoning":
                # Reasoning lives in `summary` on the wire; a few models also
                # emit a `content` list. Both are reasoning, never output.
                blocks, paths = self._collect_text_items(
                    payload.get("summary"), REASONING_ITEM_TYPES, block_kind="thinking"
                )
                blocks2, paths2 = self._collect_text_items(
                    payload.get("content"), REASONING_ITEM_TYPES, block_kind="thinking"
                )
                if not blocks and not blocks2:
                    continue
                combined = blocks + blocks2
                combined_paths = [("summary", i, k) for (i, k) in paths] + [
                    ("content", i, k) for (i, k) in paths2
                ]
                unified.append(
                    UnifiedMessage(
                        role="assistant",
                        content=combined,
                        timestamp=record.get("timestamp"),
                        raw={
                            "record_index": index,
                            "record": deepcopy(record),
                            "kind": "reasoning",
                            "thinking_paths": combined_paths,
                            "item_id": payload.get("id"),
                        },
                        msg_type="codex_reasoning",
                        session_id=session_id,
                    )
                )
                # Non-message items we do not model (tool calls, shell calls,
                # web search, image generation) are deliberately not surfaced:
                # they carry no conversational prose to drift.

        return unified

    # ── writing ──────────────────────────────────────────────────────────
    @staticmethod
    def _mirror_text(records: List[Dict[str, Any]], old: str, new: str) -> int:
        """
        Apply an exact-match text substitution across the DISPLAY stream.

        The TUI draws from `event_msg` records, which hold their own copy of
        the turn's text. Substituting only exact matches means the mirror is
        updated when it is a copy of the turn we rewrote, and is never
        touched otherwise — no correlation by index, no guessing at the
        event schema, and every event shape (item_completed, agent_reasoning,
        and whatever else the CLI adds later) is covered by the same walk.
        """
        replacements = 0

        def walk(node: Any) -> None:
            nonlocal replacements
            if isinstance(node, str):
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, str):
                        if value == old:
                            node[key] = new
                            replacements += 1
                    else:
                        walk(value)
            elif isinstance(node, list):
                for position, value in enumerate(node):
                    if isinstance(value, str):
                        if value == old:
                            node[position] = new
                            replacements += 1
                    else:
                        walk(value)

        for record in records:
            if record.get("type") == "event_msg":
                walk(record.get("payload"))
        return replacements

    def rebuild_session(self, messages: List[UnifiedMessage], original_data: Any) -> Any:
        """
        Write sanitized text back onto the original records, in place. Records
        never disappear: anything not present in `messages` keeps its original
        line untouched (DriftClean rewrites, it never deletes). Fabricated
        turns are appended.
        """
        records = original_data.get("records") or []
        next_ordinal = 0
        for record in records:
            ordinal = record.get("ordinal")
            if isinstance(ordinal, int):
                next_ordinal = max(next_ordinal, ordinal + 1)

        last_turn_id = self._last_turn_id(records)

        for msg in messages:
            raw = msg.raw or {}

            if raw.get("fabricated"):
                record = self._fabricated_record(msg, next_ordinal, last_turn_id)
                records.append(record)
                next_ordinal += 1
                # The display mirror for the new turn, in the verified shape
                # of a real UserMessage item (AgentMessage shares its item
                # content schema). It takes an ordinal of its own: in a real
                # rollout every record — the response_item and its mirror
                # alike — is numbered separately.
                records.append(self._fabricated_mirror(msg, record, next_ordinal, last_turn_id))
                next_ordinal += 1
                continue

            index = raw.get("record_index")
            if not isinstance(index, int) or not (0 <= index < len(records)):
                continue
            record = records[index]
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue

            if raw.get("kind") == "message":
                before = self._payload_text(payload)
                self._write_text_blocks(payload, msg, raw.get("text_paths") or [])
                after = self._payload_text(payload)
                if before and after and before != after:
                    self._mirror_text(records, before, after)
            elif raw.get("kind") == "reasoning":
                self._write_thinking_blocks(payload, msg, raw.get("thinking_paths") or [])

        original_data["records"] = records
        return original_data

    @staticmethod
    def _last_turn_id(records: List[Dict[str, Any]]) -> Optional[str]:
        """The most recent turn id, reused so a seeded turn belongs to a turn."""
        for record in reversed(records):
            payload = record.get("payload")
            if isinstance(payload, dict):
                turn_id = payload.get("turn_id")
                if isinstance(turn_id, str) and turn_id:
                    return turn_id
        return None

    @staticmethod
    def _payload_text(payload: Dict[str, Any]) -> str:
        """Joined visible text of a message payload (used to spot a change)."""
        texts = []
        content = payload.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    texts.append(item["text"])
        return "\n".join(texts)

    @staticmethod
    def _write_text_blocks(payload: Dict[str, Any], msg: UnifiedMessage, paths: List[Tuple[int, str]]) -> None:
        """
        Write the message's output blocks onto the content items the paths
        point at. A block index with no path left to consume is ignored rather
        than appended: growing the record's content list is how a rewrite
        turns into a duplicate.
        """
        outputs = [b for b in msg.iter_blocks() if UnifiedMessage._block_kind(b) == "text"]
        content = payload.get("content")
        if not isinstance(content, list):
            return
        for (content_index, key), block in zip(paths, outputs):
            if 0 <= content_index < len(content) and isinstance(content[content_index], dict):
                content[content_index][key] = UnifiedMessage._block_text(block)

    @staticmethod
    def _write_thinking_blocks(payload: Dict[str, Any], msg: UnifiedMessage, paths: List[Any]) -> None:
        """Same contract as _write_text_blocks, for the reasoning stream."""
        thinking = [b for b in msg.iter_blocks() if UnifiedMessage._block_kind(b) == "thinking"]
        for path, block in zip(paths, thinking):
            container_name, item_index, key = path
            container = payload.get(container_name)
            if isinstance(container, list) and 0 <= item_index < len(container):
                if isinstance(container[item_index], dict):
                    container[item_index][key] = UnifiedMessage._block_text(block)

    @staticmethod
    def _fabricated_record(msg: UnifiedMessage, ordinal: int, turn_id: Optional[str]) -> Dict[str, Any]:
        """A new conversation turn, shaped like a real assistant response_item."""
        # Only the output text is seeded: a fabricated turn is context, and
        # writing the compliant-thinking text here would put a reasoning
        # record in the rollout that no model ever produced.
        text = msg.get_output_text() or msg.get_text_content()
        payload: Dict[str, Any] = {
            "type": "message",
            "id": _new_id("msg_"),
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
        if turn_id:
            payload["internal_chat_message_metadata_passthrough"] = {"turn_id": turn_id}
        return {
            "timestamp": msg.timestamp or _now_iso(),
            "ordinal": ordinal,
            "type": "response_item",
            "payload": payload,
        }

    @staticmethod
    def _fabricated_mirror(
        msg: UnifiedMessage, record: Dict[str, Any], ordinal: int, turn_id: Optional[str]
    ) -> Dict[str, Any]:
        """The display event for a fabricated turn (item content shape verified
        from a real UserMessage record; AgentMessage shares that schema)."""
        text = msg.get_output_text() or msg.get_text_content()
        payload: Dict[str, Any] = {
            "type": "item_completed",
            "turn_id": turn_id,
            "item": {
                "type": "AgentMessage",
                "id": _new_id(""),
                "content": [{"type": "text", "text": text, "text_elements": []}],
            },
        }
        return {
            "timestamp": record.get("timestamp") or _now_iso(),
            "ordinal": ordinal,
            "type": "event_msg",
            "payload": payload,
        }

    @classmethod
    def apply(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Write the rollout back to disk atomically (temp file + rename), so a
        crash mid-write can never leave a half-written session behind.
        """
        path = data.get("path")
        records = data.get("records") or []
        stats = {"records_written": 0, "bytes_written": 0}
        if not path:
            return stats

        target = Path(str(path))
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)

        lines = [json.dumps(record, ensure_ascii=False) for record in records]
        lines.extend(line.rstrip("\n") for line in (data.get("passthrough") or []))
        payload = "\n".join(lines) + "\n"

        fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=target.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, str(target))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        stats["records_written"] = len(records)
        stats["bytes_written"] = len(payload.encode("utf-8"))
        return stats

    def create_fabricated_message(self, role, content, reference_msg=None, timestamp=None):
        base = super().create_fabricated_message(role, content, reference_msg, timestamp)
        base.raw["fabricated"] = True
        if reference_msg is not None:
            base.raw["source_item_id"] = (reference_msg.raw or {}).get("item_id")
        return base
