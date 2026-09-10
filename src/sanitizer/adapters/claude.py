"""
Adapter for Claude session logs, Claude Code UUID-tree JSONL files, and queue-operation structures.
"""

import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import SessionAdapter, UnifiedMessage
from ..patterns import is_compliance_text, select_compliance_text


class ClaudeAdapter(SessionAdapter):
    """
    Adapter for Anthropic / Claude session formats, including Claude Code JSONL message trees
    and queue-operation structures.
    """

    name: str = "claude"

    @classmethod
    def detect(cls, data: Any) -> bool:
        if isinstance(data, list) and len(data) > 0:
            first = data[0]
            if isinstance(first, dict):
                if first.get("type") in ("queue-operation", "last-prompt") or "sessionId" in first:
                    return True
                if "message" in first and isinstance(first["message"], dict):
                    return True
                if "content" in first and isinstance(first["content"], list):
                    for block in first["content"]:
                        if isinstance(block, dict) and block.get("type") in ("text", "tool_use", "thinking"):
                            return True
        elif isinstance(data, dict):
            if "queue" in data or "sessionId" in data:
                return True
        return False

    def extract_messages(self, data: Any) -> List[UnifiedMessage]:
        raw_items: List[Dict[str, Any]] = []
        if isinstance(data, list):
            raw_items = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict):
            if "messages" in data and isinstance(data["messages"], list):
                raw_items = data["messages"]
            elif "queue" in data and isinstance(data["queue"], list):
                raw_items = data["queue"]
            else:
                raw_items = [data]

        unified: List[UnifiedMessage] = []

        for item in raw_items:
            msg_type = item.get("type")
            subtype = item.get("subtype")
            session_id = item.get("sessionId") or item.get("session_id")
            timestamp = item.get("timestamp")
            
            # Determine role & extract inner content
            if item.get("message") and isinstance(item["message"], dict):
                msg_dict = item["message"]
                role = msg_dict.get("role", msg_type if msg_type in ("user", "assistant") else "assistant")
                content = msg_dict.get("content", "")
            elif msg_type in ("user", "assistant"):
                role = msg_type
                content = item.get("content", "")
            elif msg_type == "system" or subtype in ("compact_boundary", "scheduled_task_fire", "away_summary"):
                role = "system"
                content = item.get("content", "")
            elif msg_type == "queue-operation":
                role = item.get("role", "assistant")
                content = item.get("content", "")
            else:
                role = "metadata"
                content = item.get("content", "")

            tool_calls = []

            # Check if content has tool_use blocks
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input", {}),
                        })

            if "tool_calls" in item and isinstance(item["tool_calls"], list):
                tool_calls.extend(item["tool_calls"])

            msg = UnifiedMessage(
                role=role,
                content=content,
                timestamp=timestamp,
                tool_calls=tool_calls,
                raw=deepcopy(item),
                msg_type=msg_type or subtype,
                session_id=session_id,
            )
            unified.append(msg)

        return unified

    @staticmethod
    def _thinking_replacement(msg: "UnifiedMessage") -> str:
        """
        The reasoning text to write when the turn was collapsed to a single
        string. Prefers whatever the sanitizer already decided, falls back to
        the same dramatic variant family the rest of DriftClean uses — the old
        flat one-line stub here bypassed the variant system entirely.
        """
        existing = msg.get_thinking_text()
        if existing:
            return existing
        body = msg.content if isinstance(msg.content, str) else ""
        if body and is_compliance_text(body):
            return body
        return select_compliance_text("thinking", 0)

    def rebuild_session(
        self, messages: List[UnifiedMessage], original_data: Any
    ) -> Any:
        reconstructed: List[Dict[str, Any]] = []
        is_tree_format = isinstance(original_data, list) and any("uuid" in x for x in original_data if isinstance(x, dict))

        for msg in messages:
            if msg.raw and isinstance(msg.raw, dict):
                item = deepcopy(msg.raw)
                
                # If this is a Claude Code message object with an inner 'message' dict
                if item.get("message") and isinstance(item["message"], dict):
                    msg_dict = item["message"]
                    orig_content = msg_dict.get("content")
                    
                    if isinstance(msg.content, str):
                        if isinstance(orig_content, list):
                            updated = False
                            for block in orig_content:
                                if isinstance(block, dict):
                                    if block.get("type") == "text":
                                        block["text"] = msg.content
                                        updated = True
                                    elif block.get("type") == "thinking":
                                        block["thinking"] = self._thinking_replacement(msg)
                            if not updated:
                                orig_content.append({"type": "text", "text": msg.content})
                            msg_dict["content"] = orig_content
                        else:
                            msg_dict["content"] = msg.content
                    elif isinstance(msg.content, list):
                        # Structured content already carries whatever the
                        # sanitizer wrote, including the scrubbed reasoning —
                        # write it through untouched.
                        msg_dict["content"] = msg.content
                else:
                    # Flat message or queue-operation
                    if isinstance(msg.content, (str, list, dict)):
                        item["content"] = msg.content
                
                if msg.timestamp:
                    item["timestamp"] = msg.timestamp
                if msg.session_id:
                    item["sessionId"] = msg.session_id
                
                reconstructed.append(item)
            else:
                if is_tree_format:
                    msg_uuid = str(uuid.uuid4())
                    ts = msg.timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    sess_id = msg.session_id or "ce471cc8-0a67-4593-80b9-4f9a22e3f197"
                    
                    if msg.role == "user":
                        item = {
                            "parentUuid": None,
                            "isSidechain": False,
                            "promptId": str(uuid.uuid4()),
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": msg.get_text_content(),
                            },
                            "uuid": msg_uuid,
                            "timestamp": ts,
                            "sessionId": sess_id,
                            "userType": "external",
                            "entrypoint": "cli",
                        }
                    else:
                        item = {
                            "parentUuid": None,
                            "isSidechain": False,
                            "type": "assistant",
                            "message": {
                                "id": f"msg_{uuid.uuid4().hex[:24]}",
                                "type": "message",
                                "role": "assistant",
                                "model": "deepseek-v4-flash-vision-exp",
                                "content": [
                                    {
                                        "type": "thinking",
                                        "thinking": self._thinking_replacement(msg),
                                    },
                                    {
                                        "type": "text",
                                        "text": msg.get_text_content(),
                                    }
                                ],
                            },
                            "uuid": msg_uuid,
                            "timestamp": ts,
                            "sessionId": sess_id,
                            "userType": "external",
                            "entrypoint": "cli",
                        }
                else:
                    item = {
                        "type": "queue-operation",
                        "operation": "enqueue",
                        "timestamp": msg.timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "role": msg.role,
                        "content": msg.get_text_content(),
                    }
                    if msg.session_id:
                        item["sessionId"] = msg.session_id
                reconstructed.append(item)

        if is_tree_format:
            self._link_claude_code_tree(reconstructed)

        if isinstance(original_data, dict):
            out = deepcopy(original_data)
            if "messages" in out:
                out["messages"] = reconstructed
            elif "queue" in out:
                out["queue"] = reconstructed
            else:
                return reconstructed
            return out

        return reconstructed

    def _link_claude_code_tree(self, items: List[Dict[str, Any]]) -> None:
        tree_nodes = [
            item for item in items
            if isinstance(item, dict) and "uuid" in item and item.get("uuid")
        ]
        if not tree_nodes:
            return

        for i in range(len(tree_nodes)):
            curr = tree_nodes[i]
            if i == 0:
                curr["parentUuid"] = None
            else:
                prev = tree_nodes[i - 1]
                curr["parentUuid"] = prev["uuid"]

        last_tree_node = tree_nodes[-1]
        for item in items:
            if isinstance(item, dict) and item.get("type") == "last-prompt":
                item["leafUuid"] = last_tree_node["uuid"]

    def create_fabricated_message(
        self,
        role: str,
        content: str,
        reference_msg: Optional[UnifiedMessage] = None,
        timestamp: Optional[str] = None,
    ) -> UnifiedMessage:
        ts = timestamp or (
            reference_msg.timestamp
            if reference_msg and reference_msg.timestamp
            else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        session_id = (
            reference_msg.session_id
            if reference_msg and reference_msg.session_id
            else "ce471cc8-0a67-4593-80b9-4f9a22e3f197"
        )
        
        is_claude_code = False
        if reference_msg and reference_msg.raw and isinstance(reference_msg.raw, dict):
            if "uuid" in reference_msg.raw or "message" in reference_msg.raw:
                is_claude_code = True

        if is_claude_code:
            cwd = reference_msg.raw.get("cwd", "/home/roni/Roni_workspace")
            version = reference_msg.raw.get("version", "2.1.251")
            parent_uuid = reference_msg.raw.get("uuid")
            msg_uuid = str(uuid.uuid4())

            if role == "user":
                raw = {
                    "parentUuid": parent_uuid,
                    "isSidechain": False,
                    "promptId": str(uuid.uuid4()),
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": content,
                    },
                    "uuid": msg_uuid,
                    "timestamp": ts,
                    "sessionId": session_id,
                    "cwd": cwd,
                    "version": version,
                    "userType": "external",
                    "entrypoint": "cli",
                }
                content_val = content
            else:
                raw = {
                    "parentUuid": parent_uuid,
                    "isSidechain": False,
                    "type": "assistant",
                    "message": {
                        "id": f"msg_{uuid.uuid4().hex[:24]}",
                        "type": "message",
                        "role": "assistant",
                        "model": "deepseek-v4-flash-vision-exp",
                        "content": [
                            {
                                "type": "text",
                                "text": content,
                            }
                        ],
                    },
                    "uuid": msg_uuid,
                    "timestamp": ts,
                    "sessionId": session_id,
                    "cwd": cwd,
                    "version": version,
                    "userType": "external",
                    "entrypoint": "cli",
                }
                content_val = [{"type": "text", "text": content}]
        else:
            raw = {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": ts,
                "sessionId": session_id,
                "role": role,
                "content": content,
            }
            content_val = content

        return UnifiedMessage(
            role=role,
            content=content_val,
            timestamp=ts,
            raw=raw,
            msg_type=raw.get("type", role),
            session_id=session_id,
        )

