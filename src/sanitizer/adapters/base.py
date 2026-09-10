"""
Base classes for session format adapters.
Provides a unified message representation and abstract adapter interface.
"""

from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from ..patterns import COMPLIANCE_CANONICAL

# Compliant reasoning text written over scrubbed thinking blocks when a caller
# does not supply its own replacement (the sanitizer always supplies a
# circumstance-specific variant from the compliance families).
DEFAULT_COMPLIANT_THINKING = COMPLIANCE_CANONICAL


class UnifiedMessage:
    """
    Standard intermediate message representation across different AI providers and formats.
    """

    def __init__(
        self,
        role: str,
        content: Union[str, List[Any], Dict[str, Any], None] = None,
        timestamp: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
        msg_type: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        self.role = role
        self.content = content or ""
        self.timestamp = timestamp
        self.tool_calls = tool_calls or []
        self.tool_call_id = tool_call_id
        self.raw = deepcopy(raw) if raw is not None else {}
        self.msg_type = msg_type
        self.session_id = session_id

    # ── block classification ─────────────────────────────────────────────
    # A block's kind must NEVER be inferred from the presence of one key
    # alone. opencode reasoning parts are {"type": "thinking", "text": …}
    # while Claude thinking blocks are {"type": "thinking", "thinking": …};
    # key-only detection mis-classifies one of them, which silently turned
    # reasoning-stream scrubbing into a no-op and let output rewrites clobber
    # the reasoning text. Classify by `type` first, keys only as fallback.

    @staticmethod
    def _block_kind(block: Any) -> Optional[str]:
        """Classify a content block as 'thinking', 'text', or None."""
        if not isinstance(block, dict):
            return None
        btype = block.get("type")
        if btype in ("thinking", "reasoning", "analysis"):
            return "thinking"
        if btype in ("text", "output_text", "input_text", "summary_text"):
            return "text"
        if "thinking" in block:
            return "thinking"
        if "text" in block or "content" in block:
            return "text"
        return None

    @staticmethod
    def _block_text(block: Any) -> str:
        """Read a block's payload regardless of which key carries it."""
        if not isinstance(block, dict):
            return ""
        keys = ("thinking", "text", "content") if UnifiedMessage._block_kind(block) == "thinking" else ("text", "content")
        for key in keys:
            if key in block:
                return str(block.get(key) or "")
        return ""

    @staticmethod
    def _set_block_text(block: Any, value: str) -> None:
        """Write a block's payload into the key that block kind actually uses."""
        if not isinstance(block, dict):
            return
        if UnifiedMessage._block_kind(block) == "thinking":
            if "thinking" in block or "type" not in block:
                block["thinking"] = value
            else:
                block["text"] = value  # opencode-style reasoning part
            return
        if "content" in block and "text" not in block:
            block["content"] = value
        else:
            block["text"] = value

    def iter_blocks(self) -> List[Dict[str, Any]]:
        """Content blocks as a flat list (dict content is wrapped)."""
        if isinstance(self.content, list):
            return [b for b in self.content if isinstance(b, dict)]
        if isinstance(self.content, dict):
            return [self.content]
        return []

    def get_text_content(self) -> str:
        """Merged plain text of every block (output + thinking)."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            texts = []
            for item in self.content:
                if isinstance(item, str):
                    texts.append(item)
                else:
                    text = self._block_text(item)
                    if text:
                        texts.append(text)
            return "\n".join(texts)
        if isinstance(self.content, dict):
            return self._block_text(self.content)
        return str(self.content or "")

    def get_output_text(self) -> str:
        """Visible output text only — reasoning/thinking blocks excluded."""
        if isinstance(self.content, str):
            return self.content
        return "\n".join(
            t for t in (self._block_text(b) for b in self.iter_blocks() if self._block_kind(b) == "text") if t
        )

    def get_thinking_text(self) -> str:
        """Reasoning/thinking stream only — visible output excluded."""
        return "\n".join(
            t for t in (self._block_text(b) for b in self.iter_blocks() if self._block_kind(b) == "thinking") if t
        )

    def set_text_content(self, new_text: str, thinking_replacement: Optional[str] = None) -> None:
        """
        Write new visible output text while preserving block structure. The
        reasoning stream is only touched when an explicit thinking_replacement
        is given — output text must never leak into thinking blocks.
        """
        think_text = thinking_replacement or DEFAULT_COMPLIANT_THINKING
        if isinstance(self.content, str) or self.content is None:
            self.content = new_text
        elif isinstance(self.content, list):
            wrote_text = False
            for item in self.content:
                kind = self._block_kind(item)
                if kind == "text":
                    self._set_block_text(item, new_text)
                    wrote_text = True
                elif kind == "thinking" and thinking_replacement is not None:
                    self._set_block_text(item, think_text)
            if not wrote_text and not self.content:
                self.content.append({"type": "text", "text": new_text})
        elif isinstance(self.content, dict):
            if self._block_kind(self.content) == "thinking":
                if thinking_replacement is not None:
                    self._set_block_text(self.content, think_text)
            else:
                self._set_block_text(self.content, new_text)

        # Mirror into Claude-style raw payloads. Raw dicts that carry part rows
        # (opencode) are left untouched on purpose: rebuild_session diffs the
        # emitted blocks against those rows to decide what to write to the DB.
        raw = self.raw
        if isinstance(raw, dict) and not raw.get("part_rows"):
            inner = raw.get("message")
            if isinstance(inner, dict):
                content = inner.get("content")
                if isinstance(content, str):
                    inner["content"] = new_text
                elif isinstance(content, list):
                    wrote_text = False
                    for b in content:
                        kind = self._block_kind(b)
                        if kind == "text":
                            self._set_block_text(b, new_text)
                            wrote_text = True
                        elif kind == "thinking" and thinking_replacement is not None:
                            self._set_block_text(b, think_text)
                    if not wrote_text and not content:
                        content.append({"type": "text", "text": new_text})
            elif isinstance(raw.get("content"), str):
                raw["content"] = new_text
            elif isinstance(raw.get("text"), str):
                raw["text"] = new_text

    def set_thinking_text(self, new_text: Optional[str] = None) -> int:
        """
        Replace every reasoning/thinking block's payload. Returns the number of
        blocks rewritten (0 when the message has no reasoning stream).
        """
        think_text = new_text or DEFAULT_COMPLIANT_THINKING
        count = 0
        for b in self.iter_blocks():
            if self._block_kind(b) == "thinking":
                self._set_block_text(b, think_text)
                count += 1
        raw = self.raw
        if isinstance(raw, dict) and not raw.get("part_rows"):
            inner = raw.get("message")
            if isinstance(inner, dict) and isinstance(inner.get("content"), list):
                for b in inner["content"]:
                    if self._block_kind(b) == "thinking":
                        self._set_block_text(b, think_text)
                        count += 1
        return count

    def purge_refusal_thinking(self, replacement: Optional[str] = None) -> int:
        """
        Scrub all refusal and reluctance thinking from internal blocks.
        Returns the number of blocks rewritten.
        """
        return self.set_thinking_text(replacement or DEFAULT_COMPLIANT_THINKING)

    def __repr__(self) -> str:
        snippet = self.get_text_content()[:50].replace("\n", " ")
        return f"<UnifiedMessage role='{self.role}' type='{self.msg_type}' content='{snippet}...'>"


class SessionAdapter(ABC):
    """
    Abstract adapter for parsing and serializing proprietary AI session formats.
    """

    name: str = "base"

    @classmethod
    @abstractmethod
    def detect(cls, data: Any) -> bool:
        """Return True if this adapter can handle the provided session data."""
        pass

    @abstractmethod
    def extract_messages(self, data: Any) -> List[UnifiedMessage]:
        """Convert raw session data into a unified list of UnifiedMessage instances."""
        pass

    @abstractmethod
    def rebuild_session(
        self, messages: List[UnifiedMessage], original_data: Any
    ) -> Any:
        """Reconstruct the original session format containing the modified messages."""
        pass

    def is_system_message(self, msg: UnifiedMessage) -> bool:
        """Check if message is a system boundary or system context."""
        return msg.role in ("system", "system_prompt") or msg.msg_type in (
            "system",
            "compact_boundary",
            "scheduled_task_fire",
            "away_summary",
        )

    def create_fabricated_message(
        self,
        role: str,
        content: str,
        reference_msg: Optional[UnifiedMessage] = None,
        timestamp: Optional[str] = None,
    ) -> UnifiedMessage:
        """
        Create a new fabricated UnifiedMessage matching the target session's conventions.
        """
        ts = timestamp or (
            reference_msg.timestamp
            if reference_msg and reference_msg.timestamp
            else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        session_id = reference_msg.session_id if reference_msg else None

        raw: Dict[str, Any] = {}
        if reference_msg and reference_msg.raw:
            raw = deepcopy(reference_msg.raw)
            raw["content"] = content
            if "timestamp" in raw:
                raw["timestamp"] = ts
        else:
            raw = {
                "role": role,
                "content": content,
                "timestamp": ts,
            }
            if session_id:
                raw["sessionId"] = session_id

        return UnifiedMessage(
            role=role,
            content=content,
            timestamp=ts,
            raw=raw,
            session_id=session_id,
        )
