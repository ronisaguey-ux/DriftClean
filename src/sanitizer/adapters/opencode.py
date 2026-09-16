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
from ..backup import snapshot

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


MSG_ID_PREFIX = "msg_"
PART_ID_PREFIX = "prt_"


def _new_id(prefix: str = "") -> str:
    """A fresh id in the shape opencode's own ids take.

    The prefix is load-bearing, not decoration. opencode validates an id by it
    when the session is read back — `Expected a string starting with "prt"` —
    and rejecting one fails the WHOLE session, not just the offending row: the
    TUI renders empty and every later read errors identically until the id is
    fixed. The body is 26 hex characters, matching the length and alphabet of
    opencode's own ids.
    """
    return prefix + uuid.uuid4().hex[:26]


# The shape a fabricated assistant turn gets when the session holds no real
# assistant message to copy one from (a session seeded before its first reply).
# opencode's TUI reads `tokens.output` off the newest assistant message without
# a presence check, and its overflow pre-check hands the last assistant
# message's `tokens` straight to the context calculator — so a bare payload
# does not merely render oddly, it takes down both the TUI and the reply path.
_FALLBACK_ASSISTANT = {
    "mode": "build",
    "agent": "build",
    "path": {"cwd": "", "root": "/"},
    "cost": 0,
    "tokens": {"total": 0, "input": 0, "output": 0, "reasoning": 0,
               "cache": {"write": 0, "read": 0}},
    "finish": "stop",
}

_FALLBACK_USER = {
    "agent": "build",
    "model": {"providerID": "", "modelID": ""},
    "summary": {"diffs": []},
}


def _zeroed_tokens(tokens: Any) -> Dict[str, Any]:
    """The template's own token key set with every counter zeroed.

    A fabricated turn is an anchor, not model output: it consumed nothing and
    cost nothing, so every counter reads zero rather than being invented. Only
    the key set is taken from the template — that is what keeps the payload
    identical in shape to a real turn without hardcoding opencode's message
    schema, which changes between releases. Zeroing also matters functionally:
    a fabricated turn sitting last in the session feeds `isOverflow`, and a
    zero prompt is correctly read as "not overflowing".
    """
    if not isinstance(tokens, dict):
        return deepcopy(_FALLBACK_ASSISTANT["tokens"])
    out: Dict[str, Any] = {}
    for key, value in tokens.items():
        if key == "cache" and isinstance(value, dict):
            out[key] = {cache_key: 0 for cache_key in value}
        elif isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = 0
        else:
            out[key] = deepcopy(value)
    return out


def _session_agent(conn: sqlite3.Connection, session_id: str) -> Optional[str]:
    """The session's own agent, used to prefer a like-shaped template."""
    try:
        row = conn.execute("SELECT agent FROM session WHERE id = ?", (session_id,)).fetchone()
    except sqlite3.OperationalError:
        return None  # pre-`agent` session tables
    return row[0] if row and row[0] else None


def _shape_template(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    prefer_agent: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The newest real message of `role` in the session, as a shape template.

    Derived from live data on purpose: opencode's assistant payload carries a
    dozen fields (`mode`, `variant`, `path`, `tokens.cache`, ...) that the
    fabricator has no business guessing, and this session's own rows are the
    authority on what the running build expects.

    A compaction turn is an assistant turn in name only — it carries the
    session summary rather than a reply, and its `agent`/`mode` read
    "compaction". Copying one would make a steering anchor announce itself as a
    compaction, so compaction turns are never templates, and a turn matching
    the session's own agent is preferred over any other.
    """
    fallback: Optional[Dict[str, Any]] = None
    for (blob,) in conn.execute(
        "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 500",
        (session_id,),
    ):
        try:
            row = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if row.get("role") != role:
            continue
        if role != "assistant":
            return row
        if not isinstance(row.get("tokens"), dict):
            continue  # a previously-fabricated turn is not a shape reference
        if row.get("summary"):
            continue  # the compaction marker
        if row.get("agent") == "compaction" or row.get("mode") == "compaction":
            continue
        if prefer_agent and row.get("agent") == prefer_agent:
            return row
        if fallback is None:
            fallback = row
    return fallback


def _fabricated_payload(
    template: Optional[Dict[str, Any]],
    role: str,
    parent_id: Optional[str],
    created: int,
    completed: int,
) -> Dict[str, Any]:
    """Build a complete, well-formed message payload for a fabricated turn."""
    fallback = _FALLBACK_ASSISTANT if role == "assistant" else _FALLBACK_USER
    payload: Dict[str, Any] = deepcopy(template) if template else deepcopy(fallback)

    # opencode treats a truthy `summary` on an assistant message as "this IS a
    # compaction summary" (its text is what gets replayed as the compacted
    # context). A fabricated anchor carrying one would be injected as history
    # it never summarised, so the field is dropped outright. `error` likewise
    # marks a turn that failed and must not be resurrected as a live one.
    payload.pop("summary", None)
    payload.pop("error", None)

    payload["role"] = role
    payload["time"] = {"created": created}
    if parent_id:
        payload["parentID"] = parent_id

    if role == "assistant":
        payload["time"]["completed"] = completed
        payload["cost"] = 0
        payload["tokens"] = _zeroed_tokens(payload.get("tokens"))
        payload["finish"] = "stop"
    elif "summary" in (template or fallback):
        payload["summary"] = {"diffs": []}
    return payload


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

            # Extra blocks with no existing part become new parts — but only
            # when there is actually text to put in one. A message that carries
            # no text at all (a compaction boundary, a tool-only turn) used to
            # get a manufactured empty text part here every pass it was seen,
            # which is how the live store accumulated 1,700 contentless parts
            # that say nothing, cost bytes on every read, and render as blank
            # turns. There is no part to create for content that does not
            # exist, so nothing is created.
            need_creates = max(0, len(text_blocks) - len(text_parts))
            for i in range(need_creates):
                new_text = text_blocks[len(text_parts) + i].get("text", "")
                if not new_text:
                    continue
                commit["part_creates"].append(
                    {"message_id": mid, "part_data": {"type": "text", "time": {"start": now, "end": now}, "text": new_text}}
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
            # Snapshot before the first write, always — this is the only moment
            # the pre-clean session still exists. Placed here rather than in
            # the callers because a backup a caller has to remember is a backup
            # that is missing on exactly the run that needed it. It writes
            # beside the database, so tests and scratch stores get their own
            # disposable snapshots for free.
            plan = [
                f"{len(commit.get(key) or [])} {name}"
                for key, name in (
                    ("updates", "messages rewritten"),
                    ("part_updates", "parts rewritten"),
                    ("part_creates", "parts added"),
                    ("part_deletes", "parts dropped"),
                    ("inserts", "turns added"),
                )
                if commit.get(key)
            ]
            backup_path = snapshot(conn, session_id, db_path, label="pre-clean: " + ", ".join(plan))
            if backup_path:
                stats["snapshot"] = str(backup_path)

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
                pid = _new_id(PART_ID_PREFIX)
                conn.execute(
                    "INSERT OR REPLACE INTO part(id, message_id, session_id, time_created, time_updated, data) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, entry["message_id"], session_id, now, now, json.dumps(entry["part_data"], ensure_ascii=False)),
                )
                stats["parts_created"] += 1

            # One shape lookup per role for the whole batch — the inserts in a
            # plan all come from the same session, so the template cannot vary
            # between them.
            templates: Dict[str, Optional[Dict[str, Any]]] = {}
            own_agent = _session_agent(conn, session_id)

            for ins in commit.get("inserts") or []:
                mid = _new_id(MSG_ID_PREFIX)
                pid = _new_id(PART_ID_PREFIX)
                t = ins.get("ts") or now
                role = ins.get("role", "assistant")
                # parentID is what opencode uses to hang a reply off the turn
                # it answers; a fabricated turn goes under the message it was
                # cloned from, same as any other assistant reply.
                if role not in templates:
                    templates[role] = _shape_template(
                        conn, session_id, role, prefer_agent=own_agent if role == "assistant" else None
                    )
                payload = _fabricated_payload(
                    templates[role], role, ins.get("parent_id"), t, t
                )
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
