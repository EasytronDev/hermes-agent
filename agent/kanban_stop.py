"""Turn-end guard for kanban workers, which must end with a terminal board tool that hands
the card to whoever owns it next (``kanban_complete``, ``kanban_block``,
``kanban_request_review``, ``kanban_request_changes``). Some models narrate the next step
and stop with no tool calls; Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``. Policy-only: return a bounded synthetic nudge so the loop continues
instead of exiting.

Two distinct exits needed this guard, and the second one is why it lives here:

* **narrated stop** — the model answers in prose with no tool call. The turn-end stop
  gates (``agent/turn_stop_gates.py``) reach this module and re-prompt the worker.
* **guardrail halt** — a runtime guardrail (``idempotent_no_progress_block``,
  ``repeated_exact_failure_block``, …) stops the turn mid-tool-round and synthesises a
  reply. That path never reaches the turn-end stop gates, so without this module's
  guardrail branch the worker is told "tell me how you'd like to proceed, or send
  `continue`" — a question the owner of a headless board card never sees. The card
  then sat in ``running`` (or crash-looped) for hours: 18 such runs in 7 days on this
  host, each one losing the whole turn's work.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

from agent.delegation_context import owned_kanban_task


# Every tool that ends this worker's responsibility for the card, not just the two that
# close it out: ``kanban_request_review`` moves it to ``review`` (goals.py's continuation /
# finalize prompts tell builders to call it) and ``kanban_request_changes`` returns it to
# ``ready`` (the sdlc-review skill tells reviewers to). Nudging after either asks a worker
# that did the right thing to ``kanban_complete`` a card it must not close.
_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
    "kanban_schedule",
    "kanban_request_review",
    "kanban_request_changes",
})

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """On when ``HERMES_KANBAN_TASK`` is set for the dispatcher-owned worker, unless
    ``HERMES_KANBAN_STOP_NUDGE`` disables it. In-process delegate_task children and cron runs
    inherit the env var but own no board task and carry no kanban toolset."""
    if (os.environ.get("HERMES_KANBAN_STOP_NUDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(owned_kanban_task())


def _tool_call_name(tc: Any) -> str:
    """Tool name from a dict or object tool call (``function.name`` first, then ``name``)."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        return str((fn.get("name") if isinstance(fn, dict) else tc.get("name")) or "")
    fn = getattr(tc, "function", None)
    return str((getattr(fn, "name", "") if fn is not None else getattr(tc, "name", "")) or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    for msg in filter(lambda m: isinstance(m, dict), messages or ()):
        role = msg.get("role")
        if role == "assistant" and any(
            _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS for tc in msg.get("tool_calls") or []
        ):
            return True
        if role == "tool" and str(msg.get("name") or "") in _TERMINAL_KANBAN_TOOLS:
            return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Synthetic follow-up when a kanban worker exits without a terminal tool; ``None`` when
    the guard should not fire (not a kanban worker, already completed/blocked, budget exhausted)."""
    if (
        not kanban_stop_nudge_enabled()
        or attempts >= max_attempts
        or session_called_kanban_terminal(messages)
    ):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    # The transcript is the status source: this text is only reached when the session made no
    # handoff call, so it never tells a worker to close a card it already sent to review.
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` has not been handed off: this session made no terminal board "
        "call (`kanban_complete` / `kanban_request_review` / `kanban_block`). Ending now "
        "causes a protocol violation (clean exit with the card still `running`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work is done "
        "and needs no review, `kanban_request_review(summary=...)` if it is a code "
        "change that needs same-card review, OR `kanban_block(reason=...)` if you are "
        "blocked. Reviewers approve with `kanban_complete` or send the card back with "
        "`kanban_request_changes(reason=...)`.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_guardrail_halt_nudge",
    "build_guardrail_halt_reply",
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]


# --- guardrail-halt branch ------------------------------------------------
#
# A runtime guardrail that stops the turn mid-tool-round synthesises its own reply
# (``run_agent._toolguard_controlled_halt_response``). For an INTERACTIVE session that
# reply asks the human how to proceed — correct there. For a kanban worker there is no
# human in the loop: the owner does not read the card, so "tell me how you'd like to
# proceed, or send `continue`" is a question into the void. The worker then exits with
# rc=0 and no terminal board call, the dispatcher books a ``protocol_violation``, and the
# card either crash-loops or waits for the ``max_runtime``/breaker to requeue it — hours
# later, with the turn's work lost.
#
# Two pieces, both needed:
#   * ``build_guardrail_halt_reply`` — the visible reply tells the worker to CLOSE the
#     card with what it has, instead of asking for permission to continue.
#   * ``build_guardrail_halt_nudge`` — the follow-up turn that lets it actually make the
#     terminal call (the guardrail halt does not reach the turn-end stop gates).

def build_guardrail_halt_reply(
    *,
    tool_name: str = "",
    count: int = 0,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """The guardrail-halt reply for a kanban worker, or ``None`` for a normal session.

    ``None`` means "not a kanban worker" — the caller keeps the interactive wording
    ("tell me how you'd like to proceed, or send `continue`"), which is right for a
    human-driven session.
    """
    if not kanban_stop_nudge_enabled():
        return None
    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    tool = tool_name or "the same tool"
    return (
        f"I stopped retrying because I kept running {tool} {count} times without making "
        "progress. The last result above shows what blocked it.\n\n"
        f"Task `{tid}` is a headless kanban card: there is nobody to answer a "
        "\"how should I proceed?\" question, and waiting leaves the card `running` with "
        "this turn's work unreachable. Do NOT ask for `continue`.\n"
        "Use the result you already have and close the card now:\n"
        "1. `kanban_complete(summary=..., metadata=...)` with what was delivered — if the "
        "scope did not fit, complete with the part that landed and record the rest in a "
        "`kanban_comment`.\n"
        "2. `kanban_block(reason=...)` only if a decision from the owner is genuinely "
        "needed (missing credentials, a product choice you cannot infer).\n"
        "3. `kanban_request_review(summary=...)` if the delivered change needs same-card "
        "review.\n"
        "Never end this turn asking for permission to continue."
    )


def build_guardrail_halt_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """The follow-up turn a guardrail-halted kanban worker gets to make its terminal call.

    ``None`` when the guard must not fire: not a kanban worker, the session already made a
    terminal board call, or the bounded budget is exhausted (the dispatcher books the run
    honestly instead of looping forever).
    """
    if (
        not kanban_stop_nudge_enabled()
        or attempts >= max_attempts
        or session_called_kanban_terminal(messages)
    ):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: a runtime guardrail stopped this turn (for example "
        "`idempotent_no_progress_block`). You are a Hermes kanban worker, so this turn "
        "must end with a terminal board call — a plain-text reply is NOT a terminal "
        "state for the board.\n\n"
        f"Task `{tid}` has not been handed off: this session made no terminal board call "
        "(`kanban_complete` / `kanban_request_review` / `kanban_block`). Do NOT ask the "
        "owner how to proceed and do NOT ask for `continue` — nobody is watching this "
        "card, and waiting leaves it `running` with this turn's work unreachable.\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Use the results you already have (the guardrail says they are enough).\n"
        "2. Call `kanban_complete(summary=..., metadata=...)` with what was delivered. If "
        "the scope did not fit, complete with the part that landed and leave the rest in "
        "a `kanban_comment`.\n"
        "3. Call `kanban_block(reason=...)` only if a decision from the owner is genuinely "
        "needed; call `kanban_request_review(summary=...)` if the change needs same-card "
        "review.\n\n"
        "Never end a turn with only a promise of future action.]"
    )
