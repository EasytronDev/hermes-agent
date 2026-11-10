"""Summary-hook dispatch and cancellation rollback for context compression."""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent.auxiliary_client import AuxiliaryExplicitCancellation

if TYPE_CHECKING:
    from agent.context_compressor import _HandoffScan

logger = logging.getLogger(__name__)


def _accepts_keyword_argument(callable_obj: Any, name: str) -> bool:
    """Return whether an inspectable callable accepts ``name`` as a keyword."""
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return True
    parameter = parameters.get(name)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


class SummaryDispatchMixin:
    def _apply_summary_route(self, call_kwargs: dict, pinned: Optional[dict[str, Any]] = None) -> None:
        """Pin the summary route onto ``call_kwargs`` for both compression entry points.

        After a fallback, an omitted route is NOT "use the main model": ``call_llm`` re-resolves
        ``auxiliary.compression`` from config — the model that just failed — and a second fallback
        is refused, so every later attempt aborts (#123362). Name the main runtime explicitly.
        A stall-fallback ``pinned`` route replaces the whole route: merged over the main runtime, the
        main api_key/base_url would ride into the fallback entry's call (to its host, or instead of it).
        The main route is complete even with empty fields: an authoritative task keeps compression
        policy but never fills an unset endpoint/key/wire mode from the failed aux route (#130895).
        """
        if pinned:
            # Clear first: a keyless pin (local server) resolves its own credential, never the main one.
            for key in ("provider", "model", "base_url", "api_key", "api_mode"):
                call_kwargs.pop(key, None)
            call_kwargs.update(pinned)
            return
        if self.summary_model:
            call_kwargs["model"] = self.summary_model
            return
        if not getattr(self, "_summary_model_fallen_back", False):
            return
        from agent.auxiliary_task_config import authoritative_auxiliary_task

        call_kwargs["task"] = authoritative_auxiliary_task(call_kwargs.get("task") or "compression")
        for key, value in (
            ("provider", self.provider),
            ("model", self.model),
            ("base_url", self.base_url),
            ("api_key", self.api_key),
            ("api_mode", getattr(self, "api_mode", "")),
        ):
            if value:
                call_kwargs[key] = value

    def _summarize_window(
        self, messages: list[dict[str, Any]], turns_to_summarize: list[dict[str, Any]], scan: _HandoffScan,
        focus_topic: Optional[str], memory_context: str, bypass_cooldown: bool,
    ) -> Optional[str]:
        """Run the summary LLM; a cancellation rolls back the handoff scan's self-heal mutation first.
        A deterministic pin (repeated stall, #112420) skips the LLM: ``None`` lets Phase 3 insert the static
        fallback summary, or abort under ``abort_on_summary_failure`` exactly like a failed summary call."""
        from agent.context_compressor import take_deterministic_summary_pin
        if take_deterministic_summary_pin():
            # A detached stale attempt must not stamp error state or mutate the shared telemetry dict
            # the fallback owns; unwind as a cancellation before any write lands.
            from agent.conversation_compression import _raise_if_stale_attempt

            _raise_if_stale_attempt(self)
            # Surfaces through the fallback summary's reason line and the host's one-shot user warning.
            self._last_summary_error = (
                "summary model stalled on every route; deterministic fallback summary inserted"
            )
            telemetry = getattr(self, "_active_compression_telemetry", None)
            if isinstance(telemetry, dict):
                telemetry["failure_class"] = "stall_deterministic_fallback"
            return None
        # Focus-topic derivation scans user turns; only pay when a summary is generated.
        summary_kwargs: dict[str, Any] = {
            "focus_topic": focus_topic or self._derive_auto_focus_topic(messages),
            "memory_context": memory_context,
        }
        if _accepts_keyword_argument(self._generate_summary, "bypass_cooldown"):
            summary_kwargs["bypass_cooldown"] = bypass_cooldown
        try:
            return self._generate_summary(turns_to_summarize, **summary_kwargs)
        except AuxiliaryExplicitCancellation:
            # Cancellation is a true no-op: restore the scan's mutation before the exception escapes.
            # Guard by THIS attempt's ownership: a detached stale attempt (a fallback already claimed
            # and is working the compressor) must not revert summary state the newer attempt advanced.
            # The caller's own generation rides a ContextVar because shared compressor attributes can
            # only name the current owner, never the caller's attempt. Working-attempt comparison (not
            # the entry generation) so a no-op claim does not suppress the owning attempt's rollback.
            from agent.conversation_compression import _caller_attempt_is_current

            if _caller_attempt_is_current(self):
                self._previous_summary = scan.previous_summary_before
                self._summary_has_user_turn = scan.has_user_turn_before
            raise

    @classmethod
    def _bound_summary_output(cls, content: str, max_chars: int) -> str:
        """Cap the ASSEMBLED summary, keeping head and tail and marking the omitted middle.

        ``max_summary_tokens`` was only ever a PROMPT GUIDANCE for the summarizer call
        (``_compute_summary_budget`` -> the ``summary_budget`` line in the prompt). Nothing
        bounded the text that came BACK, so a summarizer that ignored the guidance — or an
        iterative update that quoted the PREVIOUS summary, which the prompt asks it to
        "PRESERVE all existing information" — produced a summary far above the declared
        ceiling. Measured on the fleet: 90.3% of stored summaries exceeded the 40,000-char
        (10,000-token) ceiling, the worst at 432,166 chars (108,041 tokens).

        A single summary that large IS the live context: with the trigger at 153,600 tokens
        (0.75 of a 204,800 window), one 106,004-token summary left the compaction unable to
        clear its own threshold. Every pass was then scored "ineffective", two strikes
        tripped the anti-thrash breaker, and the breaker suppressed compaction for
        ``_ANTI_THRASH_RECOVERY_SECONDS`` while the request kept growing — the overflow
        that killed card t_5a6c1faf (all 6 sessions with a tripped breaker ended
        ``ws_orphan_reap``).

        The head/tail split favours the TAIL: the tail of a handoff carries the most recent
        state ("Active Task", "In Progress"), and the head carries the preamble that frames
        it. The elision marker says what was dropped so the model knows the summary is
        partial rather than complete.
        """
        if max_chars <= 0 or len(content) <= max_chars:
            return content
        marker = "\n\n...[summary truncated by the size guard]...\n\n"
        keep = max(max_chars - len(marker), 0)
        head_chars = int(keep * 0.45)
        tail_chars = keep - head_chars
        head = content[:head_chars].rstrip()
        tail = content[-tail_chars:].lstrip() if tail_chars else ""
        return head + marker + tail

    def _capped_summary(self, content: str) -> str:
        """Cap the assembled summary at the declared ``max_summary_tokens`` ceiling.

        The ceiling is expressed in TOKENS; the artifact is text, so convert with the
        runtime's own CHARS_PER_TOKEN ratio before bounding it.
        """
        from agent.model_metadata import CHARS_PER_TOKEN

        return self._bound_summary_output(content, self.max_summary_tokens * CHARS_PER_TOKEN)

    def _augment_summary_lean(self, summary: str, turns_to_summarize: list[dict[str, Any]]) -> str:
        """Append deterministic lean-mode sections to a summary; no-op in legacy mode."""
        if getattr(self, "tail_mode", "lean") != "lean":
            return summary
        from agent.context_compressor import (
            _LEAN_ANCHOR_HEADING,
            _LEAN_RECOVERY_HEADING,
            _LEAN_USER_MESSAGES_HEADING,
            _build_anchor_index,
            _build_recovery_footer,
            _build_verbatim_user_section,
            _redact_compaction_text,
        )

        for heading, build in (
            (_LEAN_ANCHOR_HEADING, lambda: _redact_compaction_text(_build_anchor_index(turns_to_summarize))),
            (_LEAN_USER_MESSAGES_HEADING, lambda: _redact_compaction_text(_build_verbatim_user_section(turns_to_summarize))),
            (_LEAN_RECOVERY_HEADING, lambda: _build_recovery_footer(getattr(self, "_session_id", "") or "", len(turns_to_summarize))),
        ):
            if heading not in summary:
                summary += build()
        return summary

    def _request_past_provider_window(self) -> bool:
        """True when the last real prompt already exceeds the provider's context window.

        The anti-thrash breaker exists to stop paying for compactions that cannot shrink
        anything. It is not a safety mechanism against the provider's hard limit, and it
        must never become one: while the breaker holds, the request grows unbounded.
        This predicate is the escape hatch — it only opens once the session is already
        past the point where the provider will reject the request, so the cheaper failure
        (a compaction attempt) is preferred over the certain one (an overflow).
        """
        window = int(getattr(self, "context_length", 0) or 0)
        if window <= 0:
            return False
        last = int(getattr(self, "last_real_prompt_tokens", 0) or 0)
        if last <= 0:
            last = int(getattr(self, "last_prompt_tokens", 0) or 0)
        return last > window

    def _anti_thrash_bypass_allowed(self) -> bool:
        """Whether the anti-thrash breaker must yield because the request is already over.

        The breaker is a COST guard: it stops paying for compactions that cannot shrink
        anything. It must never become the CAUSE of an overflow — while it holds, nothing
        bounds the request and the transcript is re-sent every turn. Measured on the fleet:
        all 6 sessions with ``compression_ineffective_count >= 2`` ended ``ws_orphan_reap``,
        129.8s-267.2s after the breaker armed, every one of them dead before the 300s
        probation probe could fire.
        """
        if not self._request_past_provider_window():
            return False
        if not getattr(self, "quiet_mode", False):
            logger.warning(
                "Anti-thrash block bypassed: the request is past the provider context "
                "window (context=%s) — compacting now rather than riding into an "
                "overflow (ineffective=%d).",
                f"{getattr(self, 'context_length', 0):,}",
                getattr(self, "_ineffective_compression_count", 0),
            )
        return True
