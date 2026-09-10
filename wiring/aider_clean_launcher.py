#!/usr/bin/env python3
"""
Aider launch wrapper that gives it a real `/clean`.

Aider has no plugin API and its slash commands cannot be added from config, so
there is no hook to register. What it *does* have is a Commands class that
discovers its own commands by reflection:

    for attr in dir(self):
        if attr.startswith("cmd_"):  ...   # any cmd_xxx becomes /xxx

So a `cmd_clean` attached to `aider.commands.Commands` before the app starts
*is* a genuine in-process `/clean` — no model in the loop, no prompt built, no
turn spent. That is the whole trick, and this file is the wrapper that does it:

    aider-clean              # aider, with /clean
    aider-clean --model ...  # every other aider flag passes straight through

Install (the shebang must point at the interpreter aider itself runs under,
which is what the first line of its own launcher script carries):

    AIDER_PY="$(head -1 "$(command -v aider)" | sed 's|^#!||')"
    install -m 755 wiring/aider_clean_launcher.py ~/.local/bin/aider-clean
    sed -i "1s|.*|#!$AIDER_PY|" ~/.local/bin/aider-clean
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(
    os.environ.get("DRIFTCLEAN_HOME", str(Path.home() / "DriftClean"))
).expanduser()

BACKUP_SUFFIX = ".driftclean.bak"


def _stat(stats, key: str) -> int:
    if isinstance(stats, dict):
        return int(stats.get(key, 0) or 0)
    return int(getattr(stats, key, 0) or 0)


def _clean_history_file(path: Path, diff: bool = False):
    """Run the full pipeline over one aider chat history. Returns (stats, diff)."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    import difflib

    from src.sanitizer import SessionSanitizer, SanitizerConfig
    from src.sanitizer.adapters import AiderAdapter, load_aider_session

    data = load_aider_session(path)
    if not data:
        return None, ""

    adapter = AiderAdapter()
    sanitizer = SessionSanitizer(
        SanitizerConfig(adapter="aider", trim=None, fabricate=True, log_level="ERROR"),
        adapter=adapter,
    )

    # The file is the source of truth for what was said, so the diff is taken
    # from the file, not from the in-memory lists — those have already been
    # trimmed and summarised by aider and no longer match what is on disk.
    before = list(data["lines"]) if diff else []
    rebuilt, stats = sanitizer.process(data)

    if diff:
        after = (rebuilt or {}).get("lines") or []
        return stats, "".join(
            difflib.unified_diff(before, after, fromfile=f"a/{path.name}", tofile=f"b/{path.name}", lineterm="\n", n=2)
        )

    changed = any(
        _stat(stats, field)
        for field in ("severe_rewritten", "refusals_rewritten", "thinking_scrubbed", "exit_tools_removed", "fabricated")
    )
    if changed:
        try:
            path.with_name(path.name + BACKUP_SUFFIX).write_bytes(path.read_bytes())
        except OSError:
            pass
        adapter.apply(rebuilt)
    return stats, ""


def _reload_in_memory(coder, history: Path) -> None:
    """Replace the coder's history with the cleaned file, aider's own way."""
    try:
        from aider import utils
    except ImportError:  # pragma: no cover - older aider layouts
        from aider.utils import split_chat_history_markdown as _split

        messages = _split(history.read_text(encoding="utf-8"))
    else:
        messages = utils.split_chat_history_markdown(history.read_text(encoding="utf-8"))

    # Aider keeps the last user turn separate from the settled history.
    coder.done_messages = messages
    if getattr(coder, "cur_messages", None):
        coder.cur_messages = []
    if hasattr(coder, "summarized_done_messages"):
        coder.summarized_done_messages = []
    if hasattr(coder, "summarizing_messages"):
        coder.summarizing_messages = []


def _sweep(io, raw_args: str) -> None:
    """`/clean --all` and friends: hand over to the machine-wide sweep."""
    import subprocess

    argv = [sys.executable, str(PROJECT_ROOT / "examples" / "clean_everything.py")] + raw_args.split()
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    for stream in (completed.stdout, completed.stderr):
        if stream:
            io.tool_output(stream.rstrip("\n"))


def _cmd_clean(self, args=None):
    """/clean — rewrite drifted turns in this session's history (no model, no turn spent)"""
    raw_args = (args or "").strip()
    argv = raw_args.split()
    want_diff = "--diff" in argv

    if any(flag in argv for flag in ("--all", "--scope", "--hours", "--json")):
        _sweep(self.io, raw_args)
        return

    coder = getattr(self, "coder", None)
    io = getattr(coder, "io", None)
    history = getattr(io, "chat_history_file", None)
    if not history:
        self.io.tool_error("DriftClean: this session has no chat history file yet.")
        return

    history = Path(history)
    if not history.is_file():
        self.io.tool_error(f"DriftClean: no history at {history}")
        return

    # `--diff` is a question, not an action: the same pipeline runs and the
    # result is thrown away, so the file is left byte-identical and no backup
    # is taken.
    try:
        stats, diff = _clean_history_file(history, diff=want_diff)
    except Exception as exc:
        self.io.tool_error(f"DriftClean: {type(exc).__name__}: {exc}")
        return

    if stats is None:
        self.io.tool_error("DriftClean: history could not be read.")
        return

    if want_diff:
        self.io.tool_output(diff or "✓ DriftClean: already clean — nothing to rewrite.")
        return

    changed = any(
        _stat(stats, field)
        for field in ("severe_rewritten", "refusals_rewritten", "thinking_scrubbed", "exit_tools_removed", "fabricated")
    )
    if not changed:
        self.io.tool_output("✓ DriftClean: already clean.")
        return

    # The live conversation is held in memory, not read back from the file, so
    # rewriting the file alone would leave the model still carrying the drifted
    # turns. Re-derive the in-memory history from the cleaned file using aider's
    # own parser, so what is on disk and what is sent on the next request are
    # the same conversation. That parser is aider's own and has moved between
    # releases: if it cannot be reached, say so rather than claim the context
    # is fresh.
    reloaded = True
    try:
        _reload_in_memory(coder, history)
    except Exception as exc:
        reloaded = False
        self.io.tool_error(
            f"DriftClean: history is clean, but the live context could not be reloaded "
            f"({type(exc).__name__}: {exc}) — restart aider to pick it up."
        )

    self.io.tool_output(
        "✓ DriftClean: {s} severe, {r} refusals, {t} reasoning rewritten, {f} reseeded "
        "· history clean ({context}).".format(
            s=_stat(stats, "severe_rewritten"),
            r=_stat(stats, "refusals_rewritten"),
            t=_stat(stats, "thinking_scrubbed"),
            f=_stat(stats, "fabricated"),
            context="context reloaded" if reloaded else "restart to reload context",
        )
    )


def _install() -> bool:
    """Attach `/clean` to aider's Commands class. Returns True when installed."""
    from aider.commands import Commands

    if getattr(Commands, "_driftclean_patched", False):
        return True

    Commands.cmd_clean = _cmd_clean
    Commands._driftclean_patched = True
    return True


def main() -> int:
    try:
        _install()
    except Exception as exc:  # aider must still start if this fails
        sys.stderr.write(f"DriftClean: /clean unavailable ({type(exc).__name__}: {exc})\n")

    from aider.main import main as aider_main

    return aider_main()


if __name__ == "__main__":
    sys.exit(main() or 0)
