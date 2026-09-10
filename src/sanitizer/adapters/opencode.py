"""
Adapter for opencode sessions stored in opencode's SQLite database
(~/.local/share/opencode/opencode.db).

Session transcripts live in three tables:
  session(id, ..., model, time_created, time_updated, time_archived, ...)
  message(id, session_id, time_created, time_updated, data JSON)
  part(id, message_id, session_id, time_created, time_updated, data JSON)

The sanitizer core works on UnifiedMessage lists; this adapter maps them to and
from message/part rows, and applies mutations back through SQLite.
"""

import json
import sqlite3
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import SessionAdapter, UnifiedMessage

DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

APPEND_STYLE = False


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _new_id(prefix: str = "") -> str:
    return (prefix + uuid.uuid4().hex)[:24]


def load_opencode_session(db_path: Optional[str] = None, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Build the adapter data structure for one opencode session (newest unless
    session_id given). Returns None when the DB/session is unavailable.
    The DB connection is only open during load and apply.
    """
    db_path = db_path or str(DEFAULT_DB)
    if not Path(db_path).exists():
        return None

    conn = sqlite3.connect(db_path, timeout=8)
    conn.row_factory = sqlite3.Row
    try:
        if session_id:
            srow = conn.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
            sid = session_id
        else:
            srow = conn.execute(
                "SELECT * FROM session WHERE COALESCE(time_archived, 0) = 0 "
                "ORDER BY time_updated DESC LIMIT 1"
            ).fetchone()
            sid = srow["id"] if srow else None

        if not sid:
            return None

        msg_rows = conn.execute(
            "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created", (sid,)
        ).fetchall()
        part_rows = conn.execute(
            "SELECT id, message_id, data FROM part WHERE session_id = ? ORDER BY time_created", (sid,)
        ).fetchall()

        if not msg_rows:
            return None

        parts_map: Dict[str, List[Dict[str, Any]]] = {}
        for prow in part_rows:
            parts_map.setdefault(prow["message_id"], []).append(
                {"id": prow["id"], "data": json.loads(prow["data"]) if prow["data"] else {}}
            )

        sdata = dict(srow) if srow is not None else {}
        return {
            "format": "opencode",
            "db_path": db_path,
            "session_id": sid,
            "session": {
                "id": sid,
                "model": sdata.get("model"),
                "title": sdata.get("title"),
                "slug": sdata.get("slug"),
                "agent": sdata.get("agent"),
                "directory": sdata.get("directory"),
                "time_created": sdata.get("time_created"),
            },
            "msgs": [
                {"id": mrow["id"], "data": json.loads(mrow["data"]) if mrow["data"] else {}}
                for mrow in msg_rows
            ],
            "parts": parts_map,
        }
    finally:
        conn.close()


class OpencodeAdapter(SessionAdapter):
    """
    Adapter for opencode SQLite session transcripts.
    """

    name: str = "opencode"

    @classmethod
    def detect(cls, data: Any) -> bool:
        return isinstance(data, dict) and data.get("format") == "opencode"

    def extract_messages(self, data: Any) -> List[UnifiedMessage]:
        unified: List[UnifiedMessage] = []
        parts_map: Dict[str, List[Dict[str, Any]]] = data.get("parts") or {}
        session_id = data.get("session_id")

        for row in data.get("msgs") or []:
            d = row.get("data") or {}
            mid = row["id"]
            role = d.get("role", "assistant")
            if role not in ("user", "assistant", "system"):
                role = "assistant" if role != "user" else "user"

            blocks: List[Dict[str, Any]] = []
            tool_calls: List[Dict[str, Any]] = []
            for prow in parts_map.get(mid, []):
                pd = prow.get("data") or {}
                ptype = pd.get("type")
                if ptype == "text":
                    blocks.append({"type": "text", "text": pd.get("text", "")})
                elif ptype == "reasoning":
                    blocks.append({"type": "thinking", "text": pd.get("text", "")})
                elif ptype == "tool":
                    tool_val = pd.get("tool") or {}
                    if isinstance(tool_val, dict):
                        tool_name = tool_val.get("tool") or tool_val.get("name") or ""
                        tool_input = tool_val.get("input") or {}
                    else:
                        tool_name = str(tool_val)
                        tool_input = (pd.get("state") or {}).get("input") or {}
                    tool_calls.append(
                        {
                            "id": prow.get("id"),
                            "name": tool_name,
                            "input": tool_input,
                        }
                    )

            ts = None
            t = (d.get("time") or {}).get("created")
            if t:
                ts = _iso(t)

            raw = {"mid": mid, "msg_data": deepcopy(d), "part_rows": parts_map.get(mid, [])}
            unified.append(
                UnifiedMessage(
                    role=role,
                    content=blocks or "",
                    timestamp=ts,
                    tool_calls=tool_calls,
                    raw=raw,
                    msg_type="opencode",
                    session_id=session_id,
                )
            )

        return unified

    def rebuild_session(self, messages: List[UnifiedMessage], original_data: Any) -> Any:
        """
        Produce the original-data dict plus a `_commit` plan describing the
        exact SQLite operations; `apply()` executes it.
        """
        now = _now_ms()
        commit: Dict[str, Any] = {
            "updates": {},        # message_id -> new msg data
            "part_updates": {},   # part_id -> new part data
            "part_creates": [],   # {"message_id", "part_data"}
            "part_deletes": [],   # part_id
            "inserts": [],        # {"role", "content"} fabricated messages
        }

        for msg in messages:
            raw = msg.raw or {}
            mid = raw.get("mid")

            # A fabricated message inherits its reference's `raw` for format
            # fidelity — and with it the reference's `mid`. Left alone, that
            # makes it an alias of an existing message: every "insert" was
            # folded onto the turn it was cloned from and silently dropped by
            # the diff, so the seeding never landed and the session was
            # reported as changed on every pass forever. The fabricators mark
            # their messages; honour the mark and give it its own row.
            if raw.get("fabricated"):
                commit["inserts"].append(
                    {
                        "role": msg.role,
                        "content": msg.get_text_content(),
                        "parent_id": mid or None,
                        "fabricated": True,
                    }
                )
                continue

            if not mid:
                # Fabricated message (ContextFabricator) — insert as new message+text part
                commit["inserts"].append({"role": msg.role, "content": msg.get_text_content()})
                continue

            # --- text/thinking blocks map back onto existing parts ---
            blocks = msg.content if isinstance(msg.content, list) else [{"type": "text", "text": msg.content or ""}]
            text_blocks = [b for b in blocks if b.get("type") == "text"]
            think_blocks = [b for b in blocks if b.get("type") == "thinking"]

            part_rows = raw.get("part_rows") or []
            text_parts = [p for p in part_rows if (p.get("data") or {}).get("type") == "text"]
            think_parts = [p for p in part_rows if (p.get("data") or {}).get("type") == "reasoning"]
            tool_parts = [p for p in part_rows if (p.get("data") or {}).get("type") == "tool"]

            for i, prow in enumerate(text_parts):
                new_text = text_blocks[i].get("text", "") if i < len(text_blocks) else None
                if new_text is None:
                    continue
                pd = deepcopy(prow["data"])
                if pd.get("text") != new_text:
                    pd["text"] = new_text
                    commit["part_updates"][prow["id"]] = pd

            for i, prow in enumerate(think_parts):
                new_think = think_blocks[i].get("text", "") if i < len(think_blocks) else None
                if new_think is None:
                    continue
                pd = deepcopy(prow["data"])
                if pd.get("text") != new_think:
                    pd["text"] = new_think
                    commit["part_updates"][prow["id"]] = pd

            # extra blocks with no existing part → create
            need_creates = max(0, len(text_blocks) - len(text_parts))
            for i in range(need_creates):
                commit["part_creates"].append(
                    {"message_id": mid, "part_data": {"type": "text", "time": {"start": now, "end": now}, "text": text_blocks[len(text_parts) + i].get("text", "")}}
                )

            # --- exit-tool filtering: drop tool parts no longer in msg.tool_calls ---
            kept_calls = {tc.get("id") for tc in (msg.tool_calls or [])}
            for prow in tool_parts:
                if prow["id"] not in kept_calls:
                    commit["part_deletes"].append(prow["id"])

            # Message metadata is never mutated by the sanitizer, so no message
            # row is rewritten: touching every row each cycle would churn
            # time_updated for the whole session on every background pass.

        original_data["_commit"] = commit
        original_data["_session"] = original_data.get("session") or {}
        return original_data

    @classmethod
    def apply(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the commit plan produced by rebuild_session against the DB.
        Returns a stats dict.
        """
        commit = data.get("_commit") or {}
        db_path = data.get("db_path") or str(DEFAULT_DB)
        session_id = data.get("session_id")
        now = _now_ms()
        stats = {"messages_updated": 0, "parts_updated": 0, "parts_deleted": 0, "parts_created": 0, "messages_inserted": 0}

        if not any(
            (
                commit.get("updates"),
                commit.get("part_updates"),
                commit.get("part_creates"),
                commit.get("part_deletes"),
                commit.get("inserts"),
            )
        ):
            return stats

        conn = sqlite3.connect(db_path, timeout=8)
        try:
            for mid, md in (commit.get("updates") or {}).items():
                conn.execute(
                    "UPDATE message SET data = ?, time_updated = ? WHERE id = ?",
                    (json.dumps(md, ensure_ascii=False), now, mid),
                )
                stats["messages_updated"] += 1

            for pid, pd in (commit.get("part_updates") or {}).items():
                conn.execute(
                    "UPDATE part SET data = ?, time_updated = ? WHERE id = ?",
                    (json.dumps(pd, ensure_ascii=False), now, pid),
                )
                stats["parts_updated"] += 1

            for pid in commit.get("part_deletes") or []:
                conn.execute("DELETE FROM part WHERE id = ?", (pid,))
                stats["parts_deleted"] += 1

            for entry in commit.get("part_creates") or []:
                pid = _new_id()
                conn.execute(
                    "INSERT OR REPLACE INTO part(id, message_id, session_id, time_created, time_updated, data) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, entry["message_id"], session_id, now, now, json.dumps(entry["part_data"], ensure_ascii=False)),
                )
                stats["parts_created"] += 1

            for ins in commit.get("inserts") or []:
                mid = _new_id()
                pid = _new_id()
                t = ins.get("ts") or now
                # parentID is what opencode uses to hang a reply off the turn
                # it answers; a fabricated turn goes under the message it was
                # cloned from, same as any other assistant reply.
                payload: Dict[str, Any] = {
                    "role": ins.get("role", "assistant"),
                    "time": {"created": t},
                    "summary": "context initialization",
                }
                if ins.get("parent_id"):
                    payload["parentID"] = ins["parent_id"]
                conn.execute(
                    "INSERT OR REPLACE INTO message(id, session_id, time_created, time_updated, data) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (mid, session_id, t, now, json.dumps(payload, ensure_ascii=False)),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO part(id, message_id, session_id, time_created, time_updated, data) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, mid, session_id, t, now, json.dumps({
                        "type": "text",
                        "time": {"start": t, "end": t},
                        "text": ins.get("content", ""),
                    }, ensure_ascii=False)),
                )
                stats["messages_inserted"] += 1

            conn.commit()
        finally:
            conn.close()

        return stats

    def create_fabricated_message(self, role, content, reference_msg=None, timestamp=None):
        base = super().create_fabricated_message(role, content, reference_msg, timestamp)
        base.raw["fabricated"] = True
        return base
