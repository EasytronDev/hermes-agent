"""E2E: the worker that hits a runtime guardrail CLOSES the card.

The scenario the board owner described: the worker reads the same file (the same SQL)
10 times, the runtime's ``idempotent_no_progress`` guardrail blocks the 10th call, and
the turn ends. Before the fix, the guardrail's own reply told the worker to "send
`continue`" — a question nobody answers on a headless card — so the worker exited
rc=0 with no terminal board call, the dispatcher booked a ``protocol_violation``, and
the card sat ``running``/crash-looping for hours (18 such runs in 7 days on this host;
t_eee22555 lost 15h).

After the fix the guardrail-halted worker is told to close the card, and it gets one
bounded follow-up turn to do it. This test measures that end to end, through the real
dispatcher and a real worker process, against a real board DB.
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.e2e.core.kanban._helpers import Board, wait_until
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc worker liveness"),
    pytest.mark.live_system_guard_bypass,  # teardown SIGKILLs this board's reparented workers
]

#: The same read-only call, repeated: the guardrail blocks it once the streak reaches
#: the configured threshold. ``read_file`` is idempotent, so the no-progress detector
#: counts the identical results.
_REPEATS = 12
_SQL_NAME = "the same SQL"


def _responder(rec: dict):
    """The scripted worker: read the same file N times, then close the card.

    The point of the test is what the RUNTIME does when the guardrail blocks the
    call — the model never gets to choose to close the card on the blocked turn, so
    the closing call here is the scripted follow-up, and the verdict comes from the
    board rows, not from the model's words.
    """
    messages = rec["body"]["messages"]
    # How many times has the worker already asked for the same file?
    reads = sum(
        1 for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
        if tc.get("function", {}).get("name") == "read_file"
    )
    if reads < _REPEATS:
        return ToolCall("read_file", {"path": "/tmp/kanban-guardrail-probe.sql"})
    # The guardrail has stopped the turn; the scripted worker now closes the card.
    return ToolCall("kanban_complete", {
        "summary": f"read {_SQL_NAME} {reads} times; the guardrail blocked the repeat and the "
                   "card is closed with what was already read",
    })


def test_a_guardrail_halted_worker_closes_its_card(tmp_path) -> None:
    board_ref: dict[str, Board] = {}

    with FakeLLMServer(_responder) as srv:
        board = board_ref["b"] = Board(tmp_path, srv.base_url)
        try:
            tid = board.create("worker hits the guardrail and must close the card")
            board.dispatch()
            pid = board.task(tid)["worker_pid"]
            assert pid, board.diag(tid)
            board.wait_worker_exit(tid, int(pid))

            # The dispatcher has to book the run: reap the exited worker.
            board.dispatch("--max", "0")

            task = board.task(tid)
            runs = board.runs(tid)
            events = board.events(tid)
            log = board.worker_log(tid)

            # 1. The card reached a TERMINAL state.
            assert task["status"] == "done", (
                f"the guardrail-halted worker did not close the card\n{board.diag(tid)}")

            # 2. The terminal transition is a real board event, not a prose reply.
            kinds = [e["kind"] for e in events]
            assert "completed" in kinds, f"no completed event: {kinds}\n{board.diag(tid)}"

            # 3. The run was NOT booked as a protocol violation: the worker made a
            #    terminal board call, so the dispatcher has nothing to complain about.
            run = runs[-1]
            assert run["outcome"] == "completed", (
                f"run outcome {run['outcome']!r}, error {run['error']!r}\n{board.diag(tid)}")
            assert "protocol violation" not in (run["error"] or "").lower(), board.diag(tid)

            # 4. The worker never asked the owner for `continue`.
            assert "send `continue`" not in log, log[-3000:]
            assert "how you'd like to proceed" not in log, log[-3000:]

            # 5. The guardrail actually fired (otherwise the test proved nothing).
            assert "idempotent_no_progress" in log or "same result" in log, log[-3000:]
        finally:
            board.kill_workers()


def test_the_worker_is_told_to_close_the_card_not_to_ask_for_continue(tmp_path) -> None:
    """The guardrail's own reply must name the terminal board call.

    This is the literal that produced the 18 stalled runs; the runtime builds it in
    ``run_agent._toolguard_controlled_halt_response``.
    """
    board_ref: dict[str, Board] = {}

    def responder(rec: dict):
        messages = rec["body"]["messages"]
        reads = sum(
            1 for m in messages
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
            if tc.get("function", {}).get("name") == "read_file"
        )
        if reads < _REPEATS:
            return ToolCall("read_file", {"path": "/tmp/kanban-guardrail-probe.sql"})
        return ToolCall("kanban_complete", {"summary": "closed after the guardrail"})

    with FakeLLMServer(responder) as srv:
        board = board_ref["b"] = Board(tmp_path, srv.base_url)
        try:
            tid = board.create("the guardrail reply must say: close the card")
            board.dispatch()
            pid = board.task(tid)["worker_pid"]
            assert pid, board.diag(tid)
            board.wait_worker_exit(tid, int(pid))
            board.dispatch("--max", "0")

            # The provider's request log holds the conversation: the guardrail's reply
            # is an assistant turn inside it.
            requests = srv.main_requests()
            replies = []
            for req in requests:
                for m in req["body"]["messages"]:
                    content = str(m.get("content") or "")
                    if "without making progress" in content:
                        replies.append(content)
            assert replies, (
                "the guardrail never produced its reply; the test proved nothing\n"
                + board.diag(tid))
            reply = replies[-1]
            assert "kanban_complete" in reply, reply
            assert "Tell me how you'd like to proceed" not in reply, reply
        finally:
            board.kill_workers()
