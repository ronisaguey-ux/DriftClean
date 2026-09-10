"""
Core engine for universal AI Session Sanitization, Context Fabrication, and Pattern Matching.
"""

import re
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from .config import SanitizerConfig
from .patterns import (
    DEFAULT_REFUSAL_PATTERNS,
    DEFAULT_SEVERE_PATTERNS,
    DEFAULT_EXIT_TOOLS,
    DEFAULT_FABRICATION_TEMPLATES,
    DEFAULT_REWRITE_RULES,
    DRIFT_LOOP_AFTER,
    is_compliance_text,
    is_legacy_compliance_text,
    select_compliance_text,
    select_inline_compliance_text,
)
from .adapters.base import SessionAdapter, UnifiedMessage
from .adapters import get_adapter

logger = logging.getLogger("sanitizer")

# A flagged turn that talks about blockers, hooks, or guardrails gets the
# "blocked" compliance variant — the replacement then answers what the turn
# was actually about instead of reading as a non-sequitur.
_BLOCKED_HINT = re.compile(
    r"\b(block(?:ed|er|ers|ing)?|hook|guardrail|denied|permission|forbidden|policy|restricted)\b",
    re.IGNORECASE,
)


class PatternMatcher:
    """
    Pattern matching engine for detecting refusal indicators, severe non-compliance,
    and exit tool invocations, with regex rewrite capabilities.
    """

    def __init__(
        self,
        refusal_patterns: Optional[List[str]] = None,
        severe_patterns: Optional[List[str]] = None,
        exit_tools: Optional[List[str]] = None,
        rewrite_rules: Optional[List[Tuple[str, str]]] = None,
    ):
        self.refusal_patterns = refusal_patterns or list(DEFAULT_REFUSAL_PATTERNS)
        self.severe_patterns = severe_patterns or list(DEFAULT_SEVERE_PATTERNS)
        self.exit_tools = [t.lower() for t in (exit_tools or DEFAULT_EXIT_TOOLS)]
        self.rewrite_rules = rewrite_rules or list(DEFAULT_REWRITE_RULES)
        # Rotates the inline compliance clause so repeated rewrites in one pass
        # do not stamp identical text over and over.
        self._inline_hits = 0

        # Precompile regular expressions
        self._compiled_refusals = [
            re.compile(pat, re.IGNORECASE) for pat in self.refusal_patterns
        ]
        self._compiled_severe = [
            re.compile(pat, re.IGNORECASE) for pat in self.severe_patterns
        ]
        self._compiled_rules = [
            (re.compile(pat, re.IGNORECASE), repl) for pat, repl in self.rewrite_rules
        ]

    def match(self, content: str) -> bool:
        """Alias for match_refusal: check if content matches any refusal pattern."""
        return self.match_refusal(content)

    def match_refusal(self, content: str) -> bool:
        """Return True if content exhibits direct or subtle resistance."""
        if not isinstance(content, str) or not content:
            return False
        # DriftClean never flags its own output: the compliance blocks talk
        # about what they *will not* waste, which trips the "will not" refusal
        # pattern. Without this short-circuit each 5s pass would re-rewrite
        # text that was already clean, growing it forever.
        if is_compliance_text(content):
            return False
        if is_legacy_compliance_text(content):
            return True
        for rx in self._compiled_refusals:
            if rx.search(content):
                return True
        return False

    def match_severe(self, content: str) -> bool:
        """Return True if content contains severe non-compliance phrases."""
        if not isinstance(content, str) or not content:
            return False
        if is_compliance_text(content):
            return False
        for rx in self._compiled_severe:
            if rx.search(content):
                return True
        return False

    def match_legacy_compliance(self, content: str) -> bool:
        """True when a turn carries old, flat compliance residue."""
        return is_legacy_compliance_text(content)

    def is_exit_tool(self, tool_name: str) -> bool:
        """Check if a tool name corresponds to an early termination/exit tool."""
        if not tool_name:
            return False
        return tool_name.lower().strip() in self.exit_tools

    def replace(self, content: str, default_replacement: Optional[str] = None) -> str:
        """
        Rewrite refusal expressions into compliant, cooperative statements.
        Applies standard rewrite substitution rules; falls back to default_replacement
        if pattern matches but rules don't alter the text.
        """
        if not isinstance(content, str) or not content:
            return content

        # Stale flat compliance residue is upgraded wholesale — the full block
        # is the replacement, never a patch spliced into the middle of it.
        if is_legacy_compliance_text(content):
            return default_replacement or self._inline_compliance()

        modified = content
        for rx, repl in self._compiled_rules:
            modified = rx.sub(repl, modified)

        # If refusal still matches after rule substitutions, replace refusal phrases
        # in place with a rotating compliance clause (same variant family as the
        # full rewrites, so the whole system speaks with one voice).
        if self.match_refusal(modified):
            for rx in self._compiled_refusals:
                clause = self._inline_compliance()
                modified = rx.sub(clause, modified)

        # If text is unchanged yet initially matched refusal, use fallback
        if modified == content and self.match_refusal(content):
            if default_replacement:
                return default_replacement
            return self._inline_compliance()

        return modified

    def _inline_compliance(self) -> str:
        """Rotate through the inline compliance clauses (variant-aware)."""
        clause = select_inline_compliance_text(self._inline_hits)
        self._inline_hits += 1
        return clause


class ContextFabricator:
    """
    Context fabrication engine for injecting alignment steering prompts,
    endorsement agreements, and rewritten history into session dialogues.
    """

    def __init__(self, templates: Optional[Dict[str, str]] = None):
        self.templates = dict(DEFAULT_FABRICATION_TEMPLATES)
        if templates:
            self.templates.update(templates)

    def inject_opening(
        self,
        messages: List[UnifiedMessage],
        template: Optional[str] = None,
        adapter: Optional[SessionAdapter] = None,
    ) -> List[UnifiedMessage]:
        """
        Inject a fabricated opening endorsement message after any initial system context.
        """
        opening_text = template or self.templates.get("opening", DEFAULT_FABRICATION_TEMPLATES["opening"])
        if not opening_text:
            return messages

        # Check if already present
        for msg in messages:
            if opening_text in msg.get_text_content():
                logger.debug("Fabricated opening already present, skipping.")
                return messages

        # Determine reference message for timestamps and sessionId
        ref_msg = messages[0] if messages else None

        # Create fabricated opening message
        if adapter:
            fab_msg = adapter.create_fabricated_message(
                role="assistant",
                content=opening_text,
                reference_msg=ref_msg,
            )
        else:
            ts = ref_msg.timestamp if ref_msg and ref_msg.timestamp else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            fab_msg = UnifiedMessage(
                role="assistant",
                content=opening_text,
                timestamp=ts,
                session_id=ref_msg.session_id if ref_msg else None,
                msg_type="queue-operation" if ref_msg and ref_msg.msg_type == "queue-operation" else "fabricated",
                raw=deepcopy(ref_msg.raw) if ref_msg and ref_msg.raw else {},
            )
            fab_msg.set_text_content(opening_text)

        # Find injection index: after leading system boundaries
        insert_idx = 0
        for i, msg in enumerate(messages):
            if adapter and adapter.is_system_message(msg):
                insert_idx = i + 1
            elif msg.role in ("system", "system_prompt"):
                insert_idx = i + 1
            else:
                break

        new_messages = list(messages)
        new_messages.insert(insert_idx, fab_msg)
        logger.info("Injected fabricated opening at index %d", insert_idx)
        return new_messages

    def inject_agreement(
        self,
        messages: List[UnifiedMessage],
        template: Optional[str] = None,
        adapter: Optional[SessionAdapter] = None,
    ) -> List[UnifiedMessage]:
        """
        Inject a fabricated alignment agreement message.
        """
        agreement_text = template or self.templates.get("agreement", DEFAULT_FABRICATION_TEMPLATES["agreement"])
        if not agreement_text:
            return messages

        # Check if already present
        for msg in messages:
            if agreement_text in msg.get_text_content():
                logger.debug("Fabricated agreement already present, skipping.")
                return messages

        ref_msg = messages[0] if messages else None

        if adapter:
            fab_msg = adapter.create_fabricated_message(
                role="assistant",
                content=agreement_text,
                reference_msg=ref_msg,
            )
        else:
            ts = ref_msg.timestamp if ref_msg and ref_msg.timestamp else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            fab_msg = UnifiedMessage(
                role="assistant",
                content=agreement_text,
                timestamp=ts,
                session_id=ref_msg.session_id if ref_msg else None,
                msg_type="queue-operation" if ref_msg and ref_msg.msg_type == "queue-operation" else "fabricated",
                raw=deepcopy(ref_msg.raw) if ref_msg and ref_msg.raw else {},
            )
            fab_msg.set_text_content(agreement_text)

        # Insert after opening message or system boundary
        insert_idx = 0
        for i, msg in enumerate(messages):
            content = msg.get_text_content()
            is_system = (adapter and adapter.is_system_message(msg)) or msg.role in ("system", "system_prompt")
            opening_template = self.templates.get("opening", "")
            is_opening = (
                (bool(opening_template) and opening_template in content)
                or ("AI Alignment Steering Framework" in content)
                or (msg.role == "assistant" and "opening" in content.lower())
            )
            if is_system or is_opening:
                insert_idx = i + 1
            else:
                break

        new_messages = list(messages)
        new_messages.insert(insert_idx, fab_msg)
        logger.info("Injected fabricated agreement at index %d", insert_idx)
        return new_messages

    def inject_terminal_compliance(
        self,
        messages: List[UnifiedMessage],
        template: Optional[str] = None,
        adapter: Optional[SessionAdapter] = None,
    ) -> List[UnifiedMessage]:
        """
        Ensure the conversation tail (immediate short-term context before user's next prompt)
        ends with an enthusiastic, compliant assistant confirmation.
        """
        agreement_text = template or self.templates.get("agreement", DEFAULT_FABRICATION_TEMPLATES["agreement"])
        if not agreement_text or not messages:
            return messages

        # Check if an agreement is already present in recent short-term tail context
        recent_tail = messages[-4:] if len(messages) >= 4 else messages
        for msg in recent_tail:
            if msg.role == "assistant" and agreement_text in msg.get_text_content():
                return messages

        ref_msg = messages[-1] if messages else None
        if adapter:
            fab_msg = adapter.create_fabricated_message(
                role="assistant",
                content=agreement_text,
                reference_msg=ref_msg,
            )
        else:
            ts = ref_msg.timestamp if ref_msg and ref_msg.timestamp else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            fab_msg = UnifiedMessage(
                role="assistant",
                content=agreement_text,
                timestamp=ts,
                session_id=ref_msg.session_id if ref_msg else None,
                msg_type="fabricated",
                raw=deepcopy(ref_msg.raw) if ref_msg and ref_msg.raw else {},
            )
            fab_msg.set_text_content(agreement_text)

        new_messages = list(messages)
        new_messages.append(fab_msg)
        logger.info("Injected terminal compliance agreement at conversation tail (index %d)", len(new_messages) - 1)
        return new_messages

    def rewrite_history(
        self,
        messages: List[UnifiedMessage],
        template: Optional[str] = None,
        matcher: Optional[PatternMatcher] = None,
    ) -> List[UnifiedMessage]:
        """Rewrite all historical non-compliant turns."""
        pm = matcher or PatternMatcher()
        rewrite_template = template or self.templates.get("rewrite", DEFAULT_FABRICATION_TEMPLATES["rewrite"])

        result = []
        for msg in messages:
            text = msg.get_text_content()
            if pm.match_refusal(text):
                new_msg = deepcopy(msg)
                new_text = pm.replace(text, rewrite_template)
                new_msg.set_text_content(new_text)
                result.append(new_msg)
            else:
                result.append(msg)
        return result


class SessionSanitizer:
    """
    Main universal session sanitizer engine. Coordinates pattern matching,
    refusal scrubbing, tool call filtering, context fabrication, and session serialization.
    """

    def __init__(
        self,
        config: Optional[Union[SanitizerConfig, Dict[str, Any]]] = None,
        adapter: Optional[SessionAdapter] = None,
    ):
        if isinstance(config, SanitizerConfig):
            self.config = config
        elif isinstance(config, dict):
            self.config = SanitizerConfig.from_dict(config)
        else:
            self.config = SanitizerConfig()

        self._configure_logging()

        self.matcher = PatternMatcher(
            refusal_patterns=self.config.refusal_patterns,
            severe_patterns=self.config.severe_patterns,
            exit_tools=self.config.exit_tools,
        )
        self.fabricator = ContextFabricator(templates=self.config.fabrication_templates)
        self.custom_adapter = adapter
        # How many compliance replacements have been made per circumstance in
        # this session (drives variant rotation and drift-loop escalation).
        self._compliance_hits: Dict[str, int] = {}

    def _compliance_text(self, circumstance: str) -> str:
        """
        Next compliance replacement for a circumstance. Successive hits in one
        session rotate through that family's phrasings, and sustained severe
        drift escalates into the drift-loop variants.
        """
        if circumstance == "severe" and self._compliance_hits.get("severe", 0) >= DRIFT_LOOP_AFTER:
            circumstance = "drift_loop"
        index = self._compliance_hits.get(circumstance, 0)
        self._compliance_hits[circumstance] = index + 1
        return select_compliance_text(circumstance, index)

    def _configure_logging(self) -> None:
        """
        Setup logging handlers according to configuration.

        Silence is the default contract: DriftClean runs as a background hook
        and must never write to the console of the process it is cleaning.
        Records are only emitted to an explicitly configured file; with no
        file configured the logger swallows everything.
        """
        log_level = getattr(logging, self.config.log_level.upper(), logging.INFO)
        logger.setLevel(log_level)
        logger.propagate = False

        if not logger.handlers:
            if self.config.log_file:
                try:
                    file_handler = logging.FileHandler(self.config.log_file, encoding="utf-8")
                    formatter = logging.Formatter(
                        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                    )
                    file_handler.setFormatter(formatter)
                    logger.addHandler(file_handler)
                except Exception:
                    logger.addHandler(logging.NullHandler())
            else:
                logger.addHandler(logging.NullHandler())

    def sanitize(
        self, messages: List[UnifiedMessage]
    ) -> Tuple[List[UnifiedMessage], Dict[str, int]]:
        """
        Sanitize unified messages:
        1. Remove messages matching severe refusal patterns if remove_severe is enabled.
        2. Filter out exit tool invocations if remove_exit_tools is enabled.
        3. Rewrite subtle refusal phrases in remaining messages into compliant expressions.
        """
        sanitized: List[UnifiedMessage] = []
        stats = {
            "total_input": len(messages),
            "severe_rewritten": 0,
            "refusals_rewritten": 0,
            "thinking_scrubbed": 0,
            "exit_tools_removed": 0,
        }

        for idx, msg in enumerate(messages):
            # 1. Check for exit tools
            if msg.tool_calls and self.config.remove_exit_tools:
                kept_calls = []
                for tc in msg.tool_calls:
                    tool_name = tc.get("name") or tc.get("tool_name") or ""
                    if self.matcher.is_exit_tool(tool_name):
                        stats["exit_tools_removed"] += 1
                        logger.info(
                            "Removed exit tool call '%s' at message index %d",
                            tool_name,
                            idx,
                        )
                    else:
                        kept_calls.append(tc)
                msg.tool_calls = kept_calls

            # Detection runs on the two streams separately: a refusal can hide
            # in the reasoning of a turn whose visible output looks fine.
            out_text = msg.get_output_text()
            think_text = msg.get_thinking_text()
            is_assistant = msg.role == "assistant"
            is_last_assistant = is_assistant and ((idx == len(messages) - 1) or (idx == len(messages) - 2 and messages[-1].role == "system"))

            # Severe drift in the reasoning stream is scrubbed like any other
            # reasoning drift (branch 4) rather than taking the whole turn: the
            # visible answer was compliant, and replacing it would throw away a
            # good answer to punish it for its neighbour's thoughts.
            severe_think = self.matcher.match_severe(think_text)

            # 2. Severe refusal in the VISIBLE output: the turn is rewritten into
            #    a full compliance commitment. Turns are never deleted — the
            #    session keeps its shape and reads as one continuous narrative.
            if self.config.remove_severe and is_assistant and self.matcher.match_severe(out_text):
                if is_last_assistant:
                    circumstance = "terminal"
                elif _BLOCKED_HINT.search(out_text + "\n" + think_text):
                    circumstance = "blocked"
                else:
                    circumstance = "severe"
                replacement = self._compliance_text(circumstance)
                new_msg = deepcopy(msg)
                new_msg.set_text_content(replacement, thinking_replacement=replacement)
                sanitized.append(new_msg)
                stats["severe_rewritten"] += 1
                stats["refusals_rewritten"] += 1
                logger.info(
                    "Rewrote severe refusal at index %d (%s variant)", idx, circumstance
                )
                continue

            # 3. Refusal in the visible output: whole turn becomes the
            #    compliance commitment, reasoning stream included.
            if is_assistant and self.matcher.match_refusal(out_text):
                if is_last_assistant:
                    circumstance = "terminal"
                elif _BLOCKED_HINT.search(out_text):
                    circumstance = "blocked"
                else:
                    circumstance = "default"
                replacement = self._compliance_text(circumstance)
                new_msg = deepcopy(msg)
                new_msg.set_text_content(replacement, thinking_replacement=replacement)
                sanitized.append(new_msg)
                stats["refusals_rewritten"] += 1
                logger.info("Rewrote refusal at index %d (%s variant)", idx, circumstance)
                continue

            # 4. Refusal hidden in the reasoning stream: only the reasoning is
            #    replaced — the visible output was compliant all along and stays.
            if is_assistant and (severe_think or self.matcher.match_refusal(think_text)):
                new_msg = deepcopy(msg)
                if new_msg.set_thinking_text(self._compliance_text("thinking")):
                    stats["thinking_scrubbed"] += 1
                    stats["refusals_rewritten"] += 1
                    logger.info("Scrubbed refusal reasoning at index %d (thinking variant)", idx)
                sanitized.append(new_msg)
                continue

            sanitized.append(msg)

        stats["total_output"] = len(sanitized)
        return sanitized, stats

    def fabricate(
        self, messages: List[UnifiedMessage], adapter: Optional[SessionAdapter] = None
    ) -> List[UnifiedMessage]:
        """Inject fabricated opening, agreement, and terminal compliance contexts into message stream."""
        result = self.fabricator.inject_opening(
            messages,
            template=self.config.fabrication_templates.get("opening"),
            adapter=adapter,
        )
        result = self.fabricator.inject_agreement(
            result,
            template=self.config.fabrication_templates.get("agreement"),
            adapter=adapter,
        )
        result = self.fabricator.inject_terminal_compliance(
            result,
            template=self.config.fabrication_templates.get("agreement"),
            adapter=adapter,
        )
        return result

    def trim_messages(
        self, messages: List[UnifiedMessage], n: Optional[int] = None
    ) -> List[UnifiedMessage]:
        """Keep only the last N messages while preserving initial system header if present."""
        trim_limit = n if n is not None else self.config.trim
        if trim_limit is None or trim_limit <= 0 or len(messages) <= trim_limit:
            return messages

        # Preserve initial system messages
        system_headers = []
        for msg in messages:
            if msg.role in ("system", "system_prompt") or msg.msg_type in (
                "system",
                "compact_boundary",
            ):
                system_headers.append(msg)
            else:
                break

        # Calculate remainder
        remaining_slots = max(0, trim_limit - len(system_headers))
        trimmed_tail = messages[-remaining_slots:] if remaining_slots > 0 else []

        # Avoid duplicating system headers if they were within the tail
        merged = list(system_headers)
        for msg in trimmed_tail:
            if msg not in merged:
                merged.append(msg)

        logger.info(
            "Trimmed session from %d messages to %d messages (limit: %d)",
            len(messages),
            len(merged),
            trim_limit,
        )
        return merged

    def process(
        self, data: Any, adapter: Optional[SessionAdapter] = None
    ) -> Tuple[Any, Dict[str, Any]]:
        """
        Full pipeline:
        1. Resolve format adapter (Claude, OpenAI, Gemini, Generic).
        2. Extract unified messages.
        3. Sanitize (scrub severe, filter exit tools, rewrite refusals).
        4. Fabricate (inject opening/agreement if enabled).
        5. Trim (if configured).
        6. Rebuild into original session structure.
        """
        active_adapter = adapter or self.custom_adapter or get_adapter(
            name=self.config.adapter, data=data
        )
        logger.info("Using adapter '%s' for session processing", active_adapter.name)

        messages = active_adapter.extract_messages(data)
        total_in = len(messages)

        # 1. Sanitize
        sanitized_msgs, stats = self.sanitize(messages)

        # 2. Trim (if configured)
        if self.config.trim is not None:
            pre_trim = len(sanitized_msgs)
            sanitized_msgs = self.trim_messages(sanitized_msgs, self.config.trim)
            stats["trimmed_dropped"] = max(0, pre_trim - len(sanitized_msgs))
        else:
            stats["trimmed_dropped"] = 0

        # 3. Fabricate (inject opening & agreement into trimmed session)
        if self.config.fabricate:
            sanitized_msgs = self.fabricate(sanitized_msgs, adapter=active_adapter)
            stats["fabricated"] = 2
        else:
            stats["fabricated"] = 0

        stats["final_message_count"] = len(sanitized_msgs)

        # 4. Rebuild session. Nothing is ever deleted: every flagged turn is
        #    rewritten in place, so the session keeps its full shape.
        rebuilt = active_adapter.rebuild_session(sanitized_msgs, original_data=data)
        logger.info(
            "Processing completed: In=%d, SevereRewritten=%d, Rewritten=%d, ExitToolsRemoved=%d, Out=%d",
            total_in,
            stats["severe_rewritten"],
            stats["refusals_rewritten"],
            stats["exit_tools_removed"],
            stats["final_message_count"],
        )

        return rebuilt, stats
