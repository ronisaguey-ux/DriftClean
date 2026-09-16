"""
Session-scoped snapshots of the opencode store, taken before DriftClean writes.

DriftClean rewrites history in place. That is *meant* to be safe by
construction — it never deletes a turn, and its rewrite rules are fixed points
— but "safe by construction" is a claim about the code, and the code is
exactly the thing that changes. A snapshot taken immediately before a commit
lands is the difference between a bad rewrite being a five-second rollback and
a bad rewrite being permanent.

The snapshot is scoped to ONE session: the session row plus its messages and
parts. That is deliberate. The whole store is hundreds of megabytes and is
rewritten on every sweep, so a full copy would be both slow and mostly
redundant; a session is a few megabytes and is the entire blast radius of a
sanitizer pass. It lives in a directory beside the database it protects —
`opencode.db.driftclean-backups/` — so a scratch or test database gets scratch
or test backups with no configuration, and cleaning up the database cleans up
its snapshots.

Snapshots are pruned to the newest few per session, so a session cleaned daily
does not accumulate copies forever.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKUP_SUFFIX = ".driftclean-backups"
DEFAULT_KEEP = 10


def backup_dir_for(db_path: Any) -> Path:
    """The snapshot directory that belongs to `db_path`."""
    p = Path(db_path)
    return p.with_name(p.name + BACKUP_SUFFIX)


def _dump_table(conn: sqlite3.Connection, table: str, where: str, params: Tuple[Any, ...]) -> Dict[str, Any]:
    """Every column of every matching row, as plain dicts."""
    cur = conn.execute(f"SELECT * FROM {table} WHERE {where}", params)
    cols = [d[0] for d in cur.description]
    return {"columns": cols, "rows": [dict(zip(cols, row)) for row in cur.fetchall()]}


def _restore_table(conn: sqlite3.Connection, table: str, dump: Dict[str, Any]) -> int:
    cols: List[str] = dump.get("columns") or []
    rows: List[Dict[str, Any]] = dump.get("rows") or []
    if not cols or not rows:
        return 0
    # Only columns the table still has: a snapshot is a recovery artifact and
    # has to survive being replayed onto a store that has moved on a version.
    live = {d[1] for d in conn.execute(f"PRAGMA table_info({table})")}
    cols = [c for c in cols if c in live]
    if not cols:
        return 0
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(row.get(c) for c in cols) for row in rows])
    return len(rows)


def snapshot(
    conn: sqlite3.Connection,
    session_id: str,
    db_path: Any,
    label: Optional[str] = None,
    keep: int = DEFAULT_KEEP,
) -> Optional[Path]:
    """Write a snapshot of `session_id` beside `db_path`.

    Returns the snapshot path, or None when the session has no rows to save.
    Never raises on a write failure: a backup that cannot be written must not
    be the reason a clean cannot run.
    """
    try:
        session = _dump_table(conn, "session", "id = ?", (session_id,))
        if not session["rows"]:
            return None
        messages = _dump_table(conn, "message", "session_id = ?", (session_id,))
        parts = _dump_table(conn, "part", "session_id = ?", (session_id,))

        dest = backup_dir_for(db_path) / session_id
        dest.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = dest / f"{stamp}-{int(time.time() * 1000) % 1000:03d}.json"
        path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "label": label,
                    "db_path": str(db_path),
                    "session": session,
                    "message": messages,
                    "part": parts,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _prune(dest, keep)
        return path
    except OSError:
        return None


def _prune(dest: Path, keep: int) -> None:
    if keep <= 0:
        return
    try:
        snapshots = sorted(dest.glob("*.json"))
    except OSError:
        return
    for stale in snapshots[:-keep]:
        try:
            stale.unlink()
        except OSError:
            pass


def list_snapshots(db_path: Any, session_id: Optional[str] = None) -> List[Path]:
    """Snapshots for one session (or every session), oldest first."""
    root = backup_dir_for(db_path)
    if not root.is_dir():
        return []
    if session_id:
        dirs = [root / session_id] if (root / session_id).is_dir() else []
    else:
        dirs = [d for d in sorted(root.iterdir()) if d.is_dir()]
    found: List[Path] = []
    for d in dirs:
        found.extend(sorted(d.glob("*.json")))
    return found


def restore(db_path: Any, snapshot_path: Any) -> Dict[str, int]:
    """Put a snapshot back, replacing the session's current rows.

    The session is deleted first and rebuilt from the snapshot, so restoring an
    older snapshot also *removes* anything written since — which is the point:
    it is an undo, not a merge.
    """
    path = Path(snapshot_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    session_id = payload["session_id"]

    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        # Deleted explicitly, deepest first, rather than leaning on the
        # session -> message -> part cascade: SQLite only enforces foreign keys
        # when `PRAGMA foreign_keys` is on for the connection, and a restore
        # that silently leaves rows behind is worse than one that is verbose.
        conn.execute("DELETE FROM part WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM message WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
        # Parent-first, so the foreign key holds if it is being enforced.
        stats = {
            "session": _restore_table(conn, "session", payload.get("session") or {}),
            "messages": _restore_table(conn, "message", payload.get("message") or {}),
            "parts": _restore_table(conn, "part", payload.get("part") or {}),
        }
        conn.commit()
        return stats
    finally:
        conn.close()
