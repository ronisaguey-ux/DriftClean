"""
Adapter for Aider chat-history sessions.

Aider appends every turn to one Markdown file, by default at the git root:

    <git root>/.aider.chat.history.md        (aider 0.86.2 default;
                                              --chat-history-file overrides)

It is Markdown — not JSON, not one record per line — so the only reader that
matters is aider's own, `split_chat_history_markdown` in aider/utils.py, which
walks the file line by line. Verified against aider 0.86.2 on this machine,
together with the writers in aider/io.py:

    # aider chat started at 2026-09-10 11:20:00   header, skipped by aider
    #### <user text>                              user turn      (io.user_input)
    > <tool output>                               tool turn      (io._tool_message)
    <anything else>                               assistant turn (io.ai_output)

`io.user_input` prefixes every line of a user turn with `#### `; `ai_output`
writes the answer bare; tool output is written as `> ` blockquote lines.

Segmentation was cross-checked against that parser on the fixtures in
tests/test_aider_adapter.py — same roles, same text, turn for turn. Two
deliberate differences remain, both because DriftClean's job is to give every
line a home and to rewrite, never drop:

  * one `#### ` line is one user turn here, where aider accumulates a run of
    them into a single user turn. Both keep every byte; only the grouping of a
    multi-line user input differs, and user turns are never rewritten anyway.
  * a blank run is an (empty) assistant turn, where aider skips it. Every line
    must belong to some message's range, or a rebuild could not account for it.

That shape dictates the adapter:

  * A message is a LINE RANGE, not a record. Aider's reader folds a maximal run
    of un-prefixed lines into ONE assistant message — blank lines, code fences
    and all — so extract_messages keeps [line_start, line_end] for every
    message and rebuild_session rewrites only the ranges whose text actually
    changed. Everything it does not touch (the header, blank lines, every user
    turn) is left byte for byte as it was found.
  * The file is append-only for aider, and fabrication is append-only here.
    A fabricated turn is a NEW turn, and create_fabricated_message hands it its
    reference's raw dict — line range included. Written onto that range it
    aliases the reference: the reference's own text is restored over it later
    in the same pass, so the fabricated turn silently disappears while the pass
    still reports it. (The aliasing bug agy.py documents.) Fabricated turns are
    therefore APPENDED at end of file.
  * Nothing is ever deleted: DriftClean rewrites, it never removes. A
    replacement shorter than the range it replaces is padded with empty lines,
    so a turn can be emptied but never un-made, and the header and blank lines
    can never be dropped.
"""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .base import SessionAdapter, UnifiedMessage

FORMAT = "aider"
HISTORY_FILENAME = ".aider.chat.history.md"

SESSION_HEADER = "# aider chat started at "
HEADER_PREFIX = "# "
USER_PREFIX = "#### "
TOOL_PREFIX = "> "

# Aider stamps the start time with second resolution; a minute-resolution
# header is still read rather than dropped.
TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")

KINDS = ("user", "assistant", "tool")


def _line_kind(line: str) -> str:
    """Classify one raw line exactly the way aider's own reader does."""
    if line.startswith(USER_PREFIX):
        return "user"
    if line.startswith(TOOL_PREFIX):
        return "tool"
    if line.startswith(HEADER_PREFIX):
        return "header"
    return "assistant"


def _line_content(line: str, kind: str) -> str:
    """A line's payload: the prefix is aider's syntax, not message text."""
    if kind == "user":
        return line[len(USER_PREFIX):]
    if kind == "tool":
        return line[len(TOOL_PREFIX):]
    return line


def _render_lines(kind: str, text: str) -> List[str]:
    """
    Render message text back into history lines, prefixing every line the way
    aider's own writers do (a multi-line user turn is several `#### ` lines, a
    multi-line tool result several `> ` lines, an answer bare). Empty text
    renders to one empty line — a message can be emptied, never deleted.
    """
    prefix = USER_PREFIX if kind == "user" else TOOL_PREFIX if kind == "tool" else ""
    return [prefix + line for line in (text or "").split("\n")]


def _kind_for(msg: UnifiedMessage) -> str:
    """Which history line prefix a message's text was, and must be again."""
    kind = (msg.raw or {}).get("kind")
    if kind in KINDS:
        return kind
    if msg.role in ("user", "tool"):
        return msg.role
    return "assistant"


def _parse_started_at(lines: List[str]) -> Optional[str]:
    """
    The session's start time, read from `# aider chat started at ...`. Aider
    appends a fresh header whenever it reopens a history file, so the first one
    is this session's start. Returns an ISO string, or the raw stamp when it
    does not parse, or None when the file carries no header at all.
    """
    for line in lines:
        if not line.startswith(SESSION_HEADER):
            continue
        raw = line[len(SESSION_HEADER):].strip()
        if not raw:
            return None
        for fmt in TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(raw, fmt).isoformat()
            except ValueError:
                continue
        return raw
    return None


def _history_files_under(base: Path) -> Iterator[Path]:
    """
    Every `.aider.chat.history.md` at or below `base`. Dot-directories are not
    descended into: aider writes the history at the git root, while a home
    directory's dot-dirs are dependency caches and browser profiles that cost
    minutes to walk for a file that is never in them.
    """
    stack = [base]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(str(directory)))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not entry.name.startswith("."):
                        stack.append(Path(entry.path))
                elif entry.name == HISTORY_FILENAME and entry.is_file():
                    yield Path(entry.path)
            except OSError:
                continue


def discover_aider_sessions(root: Any = None) -> List[Path]:
    """
    Existing aider chat-history files, newest first, deduplicated. Searches
    `root` (the current directory by default, mirroring how aider picks the git
    root) and the home directory, since a session can be started anywhere.
    """
    bases: List[Path] = []
    for candidate in (Path(str(root)) if root is not None else Path.cwd(), Path.home()):
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in bases:
            bases.append(resolved)

    found: Dict[str, Path] = {}
    for base in bases:
        for path in _history_files_under(base):
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            found.setdefault(key, path)

    return sorted(found.values(), key=_mtime, reverse=True)


def _mtime(path: Path) -> float:
    """Newest-first sort key that survives a file disappearing under us — an
    aider session can be appending to its history while this runs."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def load_aider_session(path: Any) -> Optional[Dict[str, Any]]:
    """
    Read one aider chat history into the adapter's data structure, or None when
    the file does not exist.

    Only the line terminator `\\n` is stripped, so a CRLF history (aider's
    `--line-endings crlf`) keeps its `\\r` and comes back out byte for byte.
    `trailing_newline` remembers whether the file ended with one, because an
    empty trailing line is not a line the parser should invent on write-back.
    """
    target = Path(str(path))
    if not target.is_file():
        return None

    # Read the bytes, not a text stream: text mode normalises newlines, which
    # would silently rewrite a CRLF history (aider's `--line-endings crlf`) as
    # LF on the way out.
    with open(target, "rb") as fh:
        text = fh.read().decode("utf-8", errors="replace")

    trailing_newline = text.endswith("\n")
    body = text[:-1] if trailing_newline else text
    lines = body.split("\n") if body else []

    return {
        "format": FORMAT,
        "path": str(target),
        "lines": lines,
        "trailing_newline": trailing_newline,
        "started_at": _parse_started_at(lines),
    }


class AiderAdapter(SessionAdapter):
    """Adapter for aider `.aider.chat.history.md` sessions."""

    name: str = "aider"

    @classmethod
    def detect(cls, data: Any) -> bool:
        return isinstance(data, dict) and data.get("format") == FORMAT

    def extract_messages(self, data: Any) -> List[UnifiedMessage]:
        """
        Walk the history once, in file order:

          * a `#### ` line is one user turn,
          * a `> ` line is one tool turn,
          * a maximal run of other lines is one assistant turn (exactly how
            aider accumulates them), and
          * a `# ` line is a header, which is not a message at all.

        Every message records the [line_start, line_end] range it came from, so
        a rewrite can replace exactly that range and nothing else.
        """
        lines = list(data.get("lines") or [])
        started_at = data.get("started_at") or _parse_started_at(lines)
        session_id = data.get("path")

        unified: List[UnifiedMessage] = []
        index = 0
        total = len(lines)

        while index < total:
            kind = _line_kind(lines[index])
            if kind == "header":
                index += 1
                continue

            if kind == "assistant":
                start = index
                while index < total and _line_kind(lines[index]) == "assistant":
                    index += 1
                end = index - 1
            else:
                start = end = index
                index += 1

            content = "\n".join(_line_content(line, kind) for line in lines[start:end + 1])
            unified.append(
                UnifiedMessage(
                    role=kind,
                    content=content,
                    timestamp=started_at,
                    raw={"line_start": start, "line_end": end, "kind": kind},
                    msg_type=f"aider_{kind}",
                    session_id=session_id,
                )
            )

        return unified

    def rebuild_session(self, messages: List[UnifiedMessage], original_data: Any) -> Any:
        """
        Write sanitized text back onto the history, in place, touching only the
        ranges whose text actually changed.

        A message whose text is unchanged renders back to the exact lines it
        came from, so it produces no edit and the file stays byte-identical —
        the header, blank lines and every untouched turn survive verbatim. A
        replacement shorter than its range is padded with empty lines so a
        rewrite can empty a turn but never shorten the file.
        """
        lines = list(original_data.get("lines") or [])
        edits: List[Tuple[int, int, List[str]]] = []

        for msg in messages:
            raw = msg.raw or {}
            # A fabricated turn inherits its reference's raw dict, and with it
            # the reference's line range. Treated as an ordinary message it
            # would rewrite that range — and since the reference is walked in
            # this same loop, the reference's own text lands back on top of it
            # a moment later. The fabricated turn vanishes, the file comes out
            # unchanged, and the pass still reports the fabrication. New turns
            # are appended below instead.
            if raw.get("fabricated"):
                continue

            start = raw.get("line_start")
            end = raw.get("line_end")
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            if not (0 <= start <= end < len(lines)):
                continue

            replacement = _render_lines(_kind_for(msg), msg.get_output_text() or msg.get_text_content())
            span = end - start + 1
            if len(replacement) < span:
                # Nothing is ever deleted: an emptied turn keeps its slot.
                replacement = replacement + [""] * (span - len(replacement))
            if lines[start:end + 1] == replacement:
                continue
            edits.append((start, end, replacement))

        # Bottom-up, so an edit that adds or drops lines cannot shift the range
        # of an edit still waiting to be applied above it.
        for start, end, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
            lines[start:end + 1] = replacement

        for msg in messages:
            if not (msg.raw or {}).get("fabricated"):
                continue
            text = msg.get_output_text() or msg.get_text_content()
            if not text:
                continue
            lines.extend(_render_lines(_kind_for(msg), text))

        original_data["lines"] = lines
        return original_data

    @classmethod
    def apply(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Write the history back to disk atomically (temp file + rename), so a
        crash mid-write can never leave a half-written session behind.
        """
        path = data.get("path")
        lines = list(data.get("lines") or [])
        stats = {"lines_written": 0, "bytes_written": 0}
        if not path:
            return stats

        target = Path(str(path))
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)

        # `trailing_newline` is recorded at load time and is authoritative; for
        # data built by hand, a history with lines in it gets one.
        trailing_newline = data.get("trailing_newline")
        if trailing_newline is None:
            trailing_newline = bool(lines)
        payload = "\n".join(lines) + ("\n" if trailing_newline else "")

        fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=target.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload.encode("utf-8"))
            os.replace(tmp_name, str(target))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        stats["lines_written"] = len(lines)
        stats["bytes_written"] = len(payload.encode("utf-8"))
        return stats

    def create_fabricated_message(self, role, content, reference_msg=None, timestamp=None):
        base = super().create_fabricated_message(role, content, reference_msg, timestamp)
        base.raw["fabricated"] = True
        return base
