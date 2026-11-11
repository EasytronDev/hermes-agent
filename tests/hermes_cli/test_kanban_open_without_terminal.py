"""Tests for the ``open_without_terminal`` diagnostic rule.

The indicator the board owner asked for: a card should not stay OPEN (never reaching a
terminal transition) for hours. It is the shape the guardrail class leaves behind — the
worker's turn ends on a runtime guardrail with no terminal board call, the card is
requeued, crash-loops, and then waits for a human (measured: 43 of 748 cards over 7
days on the sprint board, 5.9%).

The rule is lane-agnostic on purpose: a ``running`` card whose session died while its
process lives, a ``blocked`` card parked on a breaker trip and a ``ready`` card
requeued by the crash sweep are the same failure from the board's point of view.
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_diagnostics as kd

HOUR = 3600


def _task(**overrides):
    now = int(time.time())
    base = {
        "id": "t_open0001",
        "title": "a card nobody finished",
        "assignee": "backend",
        "status": "ready",
        "created_at": now - 10 * HOUR,
        "started_at": now - 9 * HOUR,
        "completed_at": None,
        "consecutive_failures": 0,
        "last_failure_error": None,
        "last_heartbeat_at": None,
        "current_run_id": None,
        "claim_lock": None,
        "claim_expires": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="crashed", run_id=7, error=None, ended_at=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
        "ended_at": ended_at if ended_at is not None else int(time.time()),
    }


def _kinds(diags):
    return [d.kind for d in diags]


# ---------------------------------------------------------------------------
# positive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["running", "ready", "review", "todo"])
def test_open_card_in_any_lane_without_a_terminal_transition_is_flagged(status):
    """The rule is lane-agnostic: the failure is the open card, not the lane."""
    now = int(time.time())
    diags = kd.compute_task_diagnostics(
        _task(status=status, created_at=now - 10 * HOUR),
        [_event("created", ts=now - 10 * HOUR)],
        [],
        now=now,
    )
    assert "open_without_terminal" in _kinds(diags)


def test_a_card_the_worker_explicitly_blocked_is_not_flagged():
    """``kanban_block`` IS a handoff: the worker asked the owner a question on purpose.

    It is NOT the same as the breaker trip (``gave_up``), which leaves the card
    ``blocked`` with nobody having asked anything.
    """
    now = int(time.time())
    diags = kd.compute_task_diagnostics(
        _task(status="blocked", created_at=now - 10 * HOUR),
        [_event("created", ts=now - 10 * HOUR),
         _event("blocked", ts=now - 5 * HOUR, reason="need the product decision")],
        [],
        now=now,
    )
    assert "open_without_terminal" not in _kinds(diags)


def test_a_card_parked_by_the_breaker_is_flagged():
    """``gave_up`` (the breaker trip) is NOT a handoff: nobody asked anything.

    This is the shape the guardrail class leaves behind: the worker crashed, the
    breaker tripped, and the card sat ``blocked`` for hours waiting for a manual
    promotion (measured on t_eee22555: 5.5h).
    """
    now = int(time.time())
    diags = kd.compute_task_diagnostics(
        _task(status="blocked", created_at=now - 10 * HOUR),
        [_event("created", ts=now - 10 * HOUR),
         _event("gave_up", ts=now - 5 * HOUR, failures=2, effective_limit=2)],
        [_run(outcome="crashed", error="worker exited cleanly (rc=0) without "
                "kanban_complete, kanban_block or kanban_request_review")],
        now=now,
    )
    assert "open_without_terminal" in _kinds(diags)


def test_the_severity_escalates_with_age():
    now = int(time.time())
    young = kd.compute_task_diagnostics(
        _task(created_at=now - 9 * HOUR), [], [], now=now)
    old = kd.compute_task_diagnostics(
        _task(created_at=now - 50 * HOUR), [], [], now=now)
    young_diag = next(d for d in young if d.kind == "open_without_terminal")
    old_diag = next(d for d in old if d.kind == "open_without_terminal")
    # 9h is just past the 8h threshold → warning; 50h is past 6x it → critical.
    assert young_diag.severity == "warning"
    assert old_diag.severity == "critical"


def test_the_diagnostic_carries_the_evidence():
    """The operator needs the age, the lane and the stalled run to act on it."""
    now = int(time.time())
    diags = kd.compute_task_diagnostics(
        _task(status="blocked", created_at=now - 10 * HOUR),
        [_event("gave_up", ts=now - 9 * HOUR, failures=2)],
        [_run(outcome="crashed", error="worker exited cleanly (rc=0) without "
                "kanban_complete, kanban_block or kanban_request_review")],
        now=now,
    )
    diag = next(d for d in diags if d.kind == "open_without_terminal")
    assert diag.data["status"] == "blocked"
    assert diag.data["age_seconds"] >= 10 * HOUR
    assert diag.data["stalled_run_outcome"] == "crashed"
    assert diag.run_id == 7
    assert "without a terminal transition" in diag.title
    # The detail must name the cause an operator can check.
    assert "terminal board call" in diag.detail
    # And it must offer a recovery action.
    assert any(a.kind in {"reclaim", "comment"} for a in diag.actions)


# ---------------------------------------------------------------------------
# negative: the card is fine
# ---------------------------------------------------------------------------


def test_a_fresh_card_is_not_flagged():
    now = int(time.time())
    diags = kd.compute_task_diagnostics(
        _task(created_at=now - 2 * HOUR), [], [], now=now)
    assert "open_without_terminal" not in _kinds(diags)


def test_a_card_that_completed_is_not_flagged():
    """The rule is about cards that NEVER reached a terminal transition."""
    now = int(time.time())
    diags = kd.compute_task_diagnostics(
        _task(status="done", created_at=now - 10 * HOUR),
        [_event("created", ts=now - 10 * HOUR),
         _event("completed", ts=now - 5 * HOUR)],
        [_run(outcome="completed")],
        now=now,
    )
    assert "open_without_terminal" not in _kinds(diags)


def test_a_card_that_asked_for_review_is_not_flagged():
    """``review_requested`` IS a terminal transition: the card left the worker's hands."""
    now = int(time.time())
    diags = kd.compute_task_diagnostics(
        _task(status="review", created_at=now - 10 * HOUR),
        [_event("created", ts=now - 10 * HOUR),
         _event("review_requested", ts=now - 5 * HOUR)],
        [_run(outcome="review_requested")],
        now=now,
    )
    assert "open_without_terminal" not in _kinds(diags)


def test_a_card_with_an_open_run_is_not_flagged():
    """A worker that is executing the card right now is slow, not stuck.

    The liveness signal is the RUN, not the heartbeat: the crashed worker's LAST
    heartbeat is written seconds before it dies, so "heartbeat in the last hour"
    would hide exactly the card this rule exists for.
    """
    now = int(time.time())
    diags = kd.compute_task_diagnostics(
        _task(status="running", created_at=now - 10 * HOUR),
        [_event("created", ts=now - 10 * HOUR),
         _event("heartbeat", ts=now - 60)],
        [{"id": 9, "outcome": None, "error": None, "ended_at": None}],
        now=now,
    )
    assert "open_without_terminal" not in _kinds(diags)


def test_a_recent_heartbeat_alone_does_not_hide_a_stalled_card():
    """The heartbeat a crashed worker wrote before dying must not suppress the signal."""
    now = int(time.time())
    diags = kd.compute_task_diagnostics(
        _task(status="blocked", created_at=now - 10 * HOUR),
        [_event("created", ts=now - 10 * HOUR),
         _event("heartbeat", ts=now - 60),
         _event("gave_up", ts=now - 30, failures=2)],
        [_run(outcome="crashed", error="worker exited cleanly (rc=0) without "
                "kanban_complete, kanban_block or kanban_request_review")],
        now=now,
    )
    assert "open_without_terminal" in _kinds(diags)


def test_a_closed_card_is_not_flagged():
    now = int(time.time())
    for status in ("done", "archived", "cancelled"):
        diags = kd.compute_task_diagnostics(
            _task(status=status, created_at=now - 10 * HOUR), [], [], now=now)
        assert "open_without_terminal" not in _kinds(diags), status


# ---------------------------------------------------------------------------
# the threshold is configurable
# ---------------------------------------------------------------------------


def test_the_threshold_is_configurable():
    """The owner picks the number; the rule must honour it."""
    now = int(time.time())
    diags = kd.compute_task_diagnostics(
        _task(created_at=now - 2 * HOUR), [], [], now=now,
        config={"open_without_terminal_seconds": 3600},
    )
    assert "open_without_terminal" in _kinds(diags)


def test_the_default_threshold_is_eight_hours():
    assert kd.DEFAULT_CONFIG["open_without_terminal_seconds"] == 8 * 3600
