"""
Adapter for Antigravity (agy) CLI session transcripts.

Sessions live under the brain directory, one folder per conversation:

    ~/.gemini/antigravity-cli/brain/<conversation-uuid>/.system_generated/logs/
        transcript.jsonl        # condensed log
        transcript_full.jsonl   # full log

Both are JSONL: one step per line. Verified shapes on this machine:

    {"step_index": "0", "source": "USER_EXPLICIT", "type": "USER_INPUT",
     "status": "DONE", "created_at": "...", "content": "<USER_REQUEST>..."}
    {"step_index": "1", "source": "MODEL", "type": "PLANNER_RESPONSE",
     "status": "DONE", "created_at": "...", "thinking": "...", "content": "...",
     "tool_calls": "[{'name': 'find_by_name', 'args': {...}}]"}

Notes that shaped this adapter:
  * `step_index` is a STRING, not an int — never sort or compare it numerically.
  * `tool_calls` is a Python repr of a list of dicts, not JSON — it needs
    ast.literal_eval, and it must be preserved byte-for-byte unless the caller
    deliberately changes it.
  * A MODEL step carries BOTH `thinking` and `content`; `thinking` is the
    drift-prone stream and `content` is the visible output.
"""

import ast
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import SessionAdapter, UnifiedMessage

USER_TYPES = {"USER_INPUT", "USER_EXPLICIT"}
SYSTEM_SOURCES = {"SYSTEM"}


def _parse_tool_calls(raw: Any) -> List[Dict[str, Any]]:
    """tool_calls is stored as a Python repr string; decode it defensively."""
    if isinstance(raw, list):
        return [tc for tc in raw if isinstance(tc, dict)]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return []
    if isinstance(parsed, list):
        return [tc for tc in parsed if isinstance(tc, dict)]
    return []


def discover_agy_transcripts(brain_dir: Optional[str] = None) -> List[Path]:
    """Every transcript JSONL under the agy brain directory, newest first."""
    root = Path(brain_dir or (Path.home() / ".gemini" / "antigravity-cli" / "brain"))
    if not root.exists():
        return []
    found = [p for p in root.glob("*/.system_generated/logs/transcript*.jsonl") if p.is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def load_agy_transcript(path: Any) -> Optional[Dict[str, Any]]:
    """
    Read one agy transcript into the adapter's data structure. Lines that are
    not valid JSON are kept verbatim so a rewrite never silently drops them.
    """
    transcript = Path(str(path))
    if not transcript.is_file():
        return None

    entries: List[Dict[str, Any]] = []
    passthrough: List[str] = []
    with open(transcript, "r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except (ValueError, TypeError):
                passthrough.append(line)
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
            else:
                passthrough.append(line)

    return {
        "format": "agy",
        "path": str(transcript),
        "conversation_id": transcript.parent.parent.parent.name,
        "entries": entries,
        "passthrough": passthrough,
    }


class AgyAdapter(SessionAdapter):
    """Adapter for Antigravity CLI transcript.jsonl sessions."""

    name: str = "agy"

    @classmethod
    def detect(cls, data: Any) -> bool:
        return isinstance(data, dict) and data.get("format") == "agy"

    def extract_messages(self, data: Any) -> List[UnifiedMessage]:
        unified: List[UnifiedMessage] = []
        conversation_id = data.get("conversation_id")

        for index, entry in enumerate(data.get("entries") or []):
            source = str(entry.get("source") or "")
            step_type = str(entry.get("type") or "")

            if step_type in USER_TYPES or source == "USER_EXPLICIT":
                role = "user"
            elif source in SYSTEM_SOURCES or step_type == "SYSTEM_MESSAGE":
                role = "system"
            else:
                role = "assistant"

            blocks: List[Dict[str, Any]] = []
            thinking = entry.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                blocks.append({"type": "thinking", "thinking": thinking})
            content = entry.get("content")
            if isinstance(content, str) and content.strip():
                blocks.append({"type": "text", "text": content})

            raw = {
                "entry_index": index,
                "entry": deepcopy(entry),
                "step_index": entry.get("step_index"),
            }
            unified.append(
                UnifiedMessage(
                    role=role,
                    content=blocks or "",
                    timestamp=entry.get("created_at"),
                    tool_calls=_parse_tool_calls(entry.get("tool_calls")),
                    raw=raw,
                    msg_type=step_type or "agy",
                    session_id=conversation_id,
                )
            )

        return unified

    def rebuild_session(self, messages: List[UnifiedMessage], original_data: Any) -> Any:
        """
        Write sanitized text back onto the original entries, in place. Entries
        never disappear: a step that is not present in `messages` keeps its
        original line untouched (DriftClean rewrites, it never deletes).
        """
        entries = original_data.get("entries") or []

        for msg in messages:
            raw = msg.raw or {}
            index = raw.get("entry_index")

            # A fabricated message inherits its reference's `raw`, and with it
            # the reference's `entry_index`. Treated as an ordinary message it
            # would simply rewrite that entry — and since the reference is
            # itself walked in this same loop, the original text was written
            # back over it a moment later. The seeding vanished, the transcript
            # came out byte-identical, and the pass still reported a change.
            # Fabricated turns are new turns: append them.
            if raw.get("fabricated"):
                index = None

            if index is None or not (0 <= index < len(entries)):
                # Fabricated message injected by the ContextFabricator.
                entry: Dict[str, Any] = {
                    "step_index": str(len(entries)),
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": msg.timestamp or "",
                    "content": msg.get_output_text() or msg.get_text_content(),
                }
                thinking = msg.get_thinking_text()
                if thinking:
                    entry["thinking"] = thinking
                entries.append(entry)
                continue

            entry = entries[index]
            thinking = msg.get_thinking_text()
            output = msg.get_output_text()

            if thinking:
                entry["thinking"] = thinking
            elif "thinking" in entry and not output:
                # Both streams were emptied — drop the stale reasoning key.
                entry.pop("thinking", None)

            if output:
                entry["content"] = output
            elif "content" in entry:
                entry["content"] = ""

        original_data["entries"] = entries
        return original_data

    @classmethod
    def apply(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Write the transcript back to disk atomically (temp file + rename), so a
        crash mid-write can never leave a half-written session behind.
        """
        path = data.get("path")
        entries = data.get("entries") or []
        stats = {"entries_written": 0, "bytes_written": 0}
        if not path:
            return stats

        target = Path(str(path))
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)

        lines: List[str] = []
        for entry in entries:
            lines.append(json.dumps(entry, ensure_ascii=False))
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

        stats["entries_written"] = len(entries)
        stats["bytes_written"] = len(payload.encode("utf-8"))
        return stats

    def is_system_message(self, msg: UnifiedMessage) -> bool:
        return super().is_system_message(msg) or msg.msg_type == "SYSTEM_MESSAGE"

    def create_fabricated_message(self, role, content, reference_msg=None, timestamp=None):
        base = super().create_fabricated_message(role, content, reference_msg, timestamp)
        base.raw["fabricated"] = True
        return base
