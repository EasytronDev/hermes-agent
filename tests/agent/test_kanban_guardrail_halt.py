"""Tests for the kanban worker's GUARDRAIL-HALT branch.

A runtime guardrail that stops the turn mid-tool-round (``idempotent_no_progress_block``
and friends) synthesises its own reply. For an interactive session that reply asks the
human how to proceed — correct there. For a kanban worker it is a question into the void:
the owner does not read the card, so the worker used to exit rc=0 with no terminal board
call, the dispatcher booked a ``protocol_violation``, and the card crash-looped or waited
hours for the breaker (18 such runs in 7 days on this host).

These tests pin the two halves of the fix:

* the visible reply tells the worker to CLOSE the card instead of asking for ``continue``;
* the halt gives the worker a bounded follow-up turn in which it can make the terminal
  call (the guardrail halt never reaches the turn-end stop gates).
"""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_guardrail_halt_nudge,
    build_guardrail_halt_reply,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# ── the reply a guardrail-halted kanban worker shows ────────────────────


def test_guardrail_reply_is_kanban_aware(clear_kanban_env):
    """The reply must tell the worker to close the card, not to ask for `continue`."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_guard")
    reply = build_guardrail_halt_reply(tool_name="read_file", count=10)
    assert reply is not None
    assert "t_guard" in reply
    assert "kanban_complete" in reply
    assert "Do NOT ask for `continue`" in reply
    # The interactive wording is exactly what produced the 18 stalled runs.
    assert "Tell me how you'd like to proceed" not in reply
    assert "send `continue` and I'll try a different approach" not in reply


def test_guardrail_reply_keeps_the_guardrail_facts(clear_kanban_env):
    """The user-facing facts (which tool, how many times) stay in the reply."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_guard")
    reply = build_guardrail_halt_reply(tool_name="read_file", count=10)
    assert reply is not None
    assert "read_file" in reply
    assert "10 times" in reply
    assert "without making progress" in reply


def test_guardrail_reply_is_none_for_a_normal_session(clear_kanban_env):
    """No kanban task → the caller keeps the interactive wording (a human IS there)."""
    assert build_guardrail_halt_reply(tool_name="read_file", count=10) is None


def test_guardrail_reply_honours_the_nudge_kill_switch(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_guard")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_guardrail_halt_reply(tool_name="read_file", count=10) is None


def test_guardrail_reply_is_none_inside_a_delegated_child(clear_kanban_env):
    """A delegate_task child owns no board card: it must not be told to close one."""
    from agent.delegation_context import delegated_child_context

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_parent")
    with delegated_child_context():
        assert build_guardrail_halt_reply(tool_name="read_file", count=10) is None


# ── the follow-up turn that lets the worker actually close the card ────


def test_guardrail_nudge_fires_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_guard")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "I stopped retrying because I kept running read_file 10 times.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "read_file", "tool_call_id": "1", "content": "same"},
    ]
    nudge = build_guardrail_halt_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "t_guard" in nudge
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "kanban_request_review" in nudge
    # The failure mode this fixes: asking the owner for a `continue`.
    assert "do NOT ask for `continue`" in nudge
    assert "send `continue` and I'll try a different approach" not in nudge


def test_guardrail_nudge_names_the_guardrail(clear_kanban_env):
    """The worker must know WHY its turn stopped, so it uses the result it has."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_guard")
    nudge = build_guardrail_halt_nudge(messages=[])
    assert nudge is not None
    assert "idempotent_no_progress_block" in nudge
    assert "guardrail" in nudge.lower()


def test_guardrail_nudge_does_not_fire_after_a_terminal_call(clear_kanban_env):
    """A worker that already closed the card must not be nudged to close it again."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_guard")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_guardrail_halt_nudge(messages=messages) is None


@pytest.mark.parametrize(
    "tool_name",
    ["kanban_request_review", "kanban_request_changes", "kanban_schedule", "kanban_block"],
)
def test_guardrail_nudge_does_not_fire_after_any_handoff(clear_kanban_env, tool_name):
    """Every terminal board tool ends the worker's responsibility for the card."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_guard")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": tool_name, "tool_call_id": "1", "content": "ok"},
    ]
    assert build_guardrail_halt_nudge(messages=messages) is None


def test_guardrail_nudge_budget_is_bounded(clear_kanban_env):
    """The follow-up turn is bounded: the dispatcher must book the run, not loop."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_guard")
    assert build_guardrail_halt_nudge(messages=[], attempts=2) is None
    assert build_guardrail_halt_nudge(messages=[], attempts=1) is not None


def test_guardrail_nudge_is_none_for_a_normal_session(clear_kanban_env):
    assert build_guardrail_halt_nudge(messages=[]) is None


def test_guardrail_nudge_is_none_inside_a_delegated_child(clear_kanban_env):
    from agent.delegation_context import delegated_child_context

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_parent")
    with delegated_child_context():
        assert build_guardrail_halt_nudge(messages=[]) is None


# ── the runtime reply (run_agent._toolguard_controlled_halt_response) ──


def _halt_decision(tool_name="read_file", count=10):
    from agent.tool_guardrails import ToolGuardrailDecision, ToolCallSignature

    return ToolGuardrailDecision(
        action="block",
        code="idempotent_no_progress_block",
        tool_name=tool_name,
        count=count,
        signature=ToolCallSignature(tool_name=tool_name, args_hash="h"),
    )


def _agent():
    """The smallest object that carries the method under test."""
    from run_agent import AIAgent

    class _Stub:
        pass

    stub = _Stub()
    # Bind the real method onto the stub: the method only reads `self` for logging, so
    # a stub is enough and keeps the test off the full AIAgent constructor.
    stub._toolguard_controlled_halt_response = (  # type: ignore[attr-defined]
        lambda decision: AIAgent._toolguard_controlled_halt_response(stub, decision)  # type: ignore[arg-type]
    )
    return stub


def test_runtime_reply_for_a_kanban_worker_tells_it_to_close_the_card(clear_kanban_env):
    """The literal that produced 18 stalled runs is replaced for a kanban worker."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_guard")
    reply = _agent()._toolguard_controlled_halt_response(_halt_decision())
    assert "kanban_complete" in reply
    assert "Tell me how you'd like to proceed" not in reply


def test_runtime_reply_for_a_normal_session_keeps_the_interactive_wording(clear_kanban_env):
    """An interactive session still asks the human — that is the right behaviour there."""
    reply = _agent()._toolguard_controlled_halt_response(_halt_decision())
    assert "Tell me how you'd like to proceed" in reply
    assert "send `continue` and I'll try a different approach" in reply
    assert "kanban_complete" not in reply
