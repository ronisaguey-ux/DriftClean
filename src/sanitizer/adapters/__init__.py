"""
Session Adapters for universal multi-format AI session sanitization.
"""

from typing import Any, Dict, Optional, Type

from .base import SessionAdapter, UnifiedMessage
from .claude import ClaudeAdapter
from .openai import OpenAIAdapter
from .gemini import GeminiAdapter
from .generic import GenericAdapter
from .opencode import OpencodeAdapter, load_opencode_session
from .agy import AgyAdapter, load_agy_transcript, discover_agy_transcripts
from .codex import CodexAdapter, load_codex_session, discover_codex_sessions
from .hermes import (
    DEFAULT_DB as HERMES_DEFAULT_DB,
    HermesAdapter,
    discover_hermes_sessions,
    load_hermes_session,
)
from .aider import AiderAdapter, load_aider_session, discover_aider_sessions

ADAPTER_REGISTRY: Dict[str, Type[SessionAdapter]] = {
    "claude": ClaudeAdapter,
    "openai": OpenAIAdapter,
    "gemini": GeminiAdapter,
    "generic": GenericAdapter,
    "opencode": OpencodeAdapter,
    "agy": AgyAdapter,
    "codex": CodexAdapter,
    "hermes": HermesAdapter,
    "aider": AiderAdapter,
}


def get_adapter(name: Optional[str] = None, data: Any = None) -> SessionAdapter:
    """
    Retrieve an appropriate adapter instance.
    If name is provided and matches a registered adapter (and not 'auto'), returns that adapter.
    If name is 'auto' or None, iterates through registered adapters in priority order.
    """
    if name and name.lower() in ADAPTER_REGISTRY and name.lower() != "auto":
        return ADAPTER_REGISTRY[name.lower()]()

    if data is not None:
        # Auto-detect priority. Adapters whose data is a tagged dict (opencode,
        # agy, codex) are tried first — their `detect` is an exact match on that
        # tag, so they can never steal a session belonging to another format.
        for candidate_cls in (
            OpencodeAdapter,
            AgyAdapter,
            CodexAdapter,
            HermesAdapter,
            AiderAdapter,
            ClaudeAdapter,
            GeminiAdapter,
            OpenAIAdapter,
        ):
            if candidate_cls.detect(data):
                return candidate_cls()

    return GenericAdapter()


__all__ = [
    "SessionAdapter",
    "UnifiedMessage",
    "ClaudeAdapter",
    "OpenAIAdapter",
    "GeminiAdapter",
    "GenericAdapter",
    "OpencodeAdapter",
    "AgyAdapter",
    "CodexAdapter",
    "HermesAdapter",
    "AiderAdapter",
    "load_opencode_session",
    "load_agy_transcript",
    "discover_agy_transcripts",
    "load_codex_session",
    "discover_codex_sessions",
    "load_hermes_session",
    "discover_hermes_sessions",
    "HERMES_DEFAULT_DB",
    "load_aider_session",
    "discover_aider_sessions",
    "ADAPTER_REGISTRY",
    "get_adapter",
]
