"""A failed delivery must not re-deliver the SAME event batch forever.

Regression for #146361 (second half): the notifier's failure path rewinds the
claim cursor, so the next tick claims the same events again and re-delivers
them. Measured on a live gateway (2026-11-04): one ``blocked`` event reached
the user 8 times over ~2 hours while the consecutive-failure counter only
reached 2/12 — the counter is per subscription, not per event, so it never
tripped.

The cursor is the only dedup mechanism the notification path has, so the rewind
has to be bounded. After ``MAX_REWIND_PER_BATCH`` rewinds of the same claimed
batch the cursor is advanced instead of rewound: the batch counts as delivered
and the next tick starts at the following event. The subscription stays alive,
so later events still notify.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers_notifier as notifier


class _FakeRunner:
    """Records the cursor operations a delivery performs.

    The real runner's ``_kanban_rewind`` / ``_kanban_advance`` / ``_kanban_unsub``
    are SYNC (they run inside a worker thread via ``_to_thread_process_service``),
    so the fakes are sync too.
    """

    def __init__(self):
        self.rewinds = []
        self.advances = []
        self.unsubs = []

    def _kanban_rewind(self, sub, cursor, old_cursor, board_slug):
        self.rewinds.append((cursor, old_cursor))

    def _kanban_advance(self, sub, cursor, board_slug):
        self.advances.append(cursor)

    def _kanban_unsub(self, sub, board_slug):
        self.unsubs.append(sub)


async def _immediate(func, *args, **kwargs):
    """Stand-in for ``_to_thread_process_service``: run the sync op inline."""
    return func(*args, **kwargs)


def _make_notification(runner, *, cursor=41204, old_cursor=41182, fail_counts=None,
                       batch_rewinds=None):
    """A _KanbanNotification wired to a fake runner, with only the cursor ops stubbed."""
    sub = {"task_id": "t_x", "platform": "api_server", "chat_id": "chat-1",
           "thread_id": "", "delivery_mode": "notify+wake", "notifier_profile": "default"}
    task = SimpleNamespace(title="a task", assignee="worker", status="done")
    d = {"sub": sub, "task": task, "cursor": cursor, "old_cursor": old_cursor,
         "board": "board-1", "events": []}
    notif = notifier._KanbanNotification.__new__(notifier._KanbanNotification)
    notif.runner = runner
    notif.d = d
    notif.platform_cls = None
    notif.sub_fail_counts = {} if fail_counts is None else fail_counts
    notif.batch_rewinds = {} if batch_rewinds is None else batch_rewinds
    notif.sub = sub
    notif.task = task
    notif.board_slug = "board-1"
    notif.platform_str = "api_server"
    notif.task_id = "t_x"
    notif.sub_profile = "default"
    notif.title = "a task"
    notif.board_tag = ""
    notif.head = ""
    notif.sub_key = ("t_x", "api_server", "chat-1", "")
    notif.wake_agent = True
    notif.send_passive = True
    notif.wake_handoff = notif.wake_review_detail = notif.session_key = notif.synth = ""
    notif.plat = None
    notif.adapter = None
    notif.is_push_adapter = False
    notif.wake_kinds = set()
    return notif


async def _fail_once(runner, *, cursor=41204, old_cursor=41182, fail_counts, batch_rewinds):
    notif = _make_notification(runner, cursor=cursor, old_cursor=old_cursor,
                               fail_counts=fail_counts, batch_rewinds=batch_rewinds)
    await notif.delivery_failed(
        "kanban notifier: delivery failed for %s (attempt %d/%d): %s",
        ("t_x",),
        "kanban notifier: dropping subscription %s on %s after %d failures",
        RuntimeError("boom"), False)
    return notif


@pytest.mark.asyncio
async def test_rewind_is_bounded_per_event_batch(monkeypatch):
    """Past the bound the cursor is advanced, so the event is not re-delivered."""
    monkeypatch.setattr(notifier, "_to_thread_process_service", _immediate)
    runner = _FakeRunner()
    fail_counts, batch_rewinds = {}, {}

    for _ in range(notifier.MAX_REWIND_PER_BATCH + 1):
        await _fail_once(runner, fail_counts=fail_counts, batch_rewinds=batch_rewinds)

    assert len(runner.rewinds) <= notifier.MAX_REWIND_PER_BATCH
    assert runner.advances, "the failure that reaches the bound advances the cursor"
    assert not runner.unsubs, "the subscription stays alive so later events still notify"
    # Exactly one cursor operation per failure.
    assert len(runner.rewinds) + len(runner.advances) == notifier.MAX_REWIND_PER_BATCH + 1


@pytest.mark.asyncio
async def test_a_new_batch_gets_a_fresh_rewind_budget(monkeypatch):
    """The bound is per claimed batch: a later event is retried normally again."""
    monkeypatch.setattr(notifier, "_to_thread_process_service", _immediate)
    runner = _FakeRunner()
    fail_counts, batch_rewinds = {}, {}

    for _ in range(notifier.MAX_REWIND_PER_BATCH + 1):
        await _fail_once(runner, cursor=41204, old_cursor=41182,
                         fail_counts=fail_counts, batch_rewinds=batch_rewinds)

    rewinds_before = len(runner.rewinds)
    advances_before = len(runner.advances)

    await _fail_once(runner, cursor=41300, old_cursor=41204,
                     fail_counts=fail_counts, batch_rewinds=batch_rewinds)

    assert len(runner.rewinds) == rewinds_before + 1, "a new batch gets its own rewind budget"
    assert len(runner.advances) == advances_before, "a new batch is not advanced early"


@pytest.mark.asyncio
async def test_retired_batch_stays_retired(monkeypatch):
    """After the bound, a later failure for the SAME cursor must not rewind.

    Regression: popping the counter when the bound is hit handed the batch a
    fresh rewind budget, so the same event could still be re-delivered — the
    very thing the bound exists to prevent.
    """
    monkeypatch.setattr(notifier, "_to_thread_process_service", _immediate)
    runner = _FakeRunner()
    fail_counts, batch_rewinds = {}, {}

    for _ in range(notifier.MAX_REWIND_PER_BATCH + 1):
        await _fail_once(runner, cursor=41204, old_cursor=41182,
                         fail_counts=fail_counts, batch_rewinds=batch_rewinds)

    rewinds_after_bound = len(runner.rewinds)
    advances_after_bound = len(runner.advances)

    for _ in range(3):
        await _fail_once(runner, cursor=41204, old_cursor=41182,
                         fail_counts=fail_counts, batch_rewinds=batch_rewinds)

    assert len(runner.rewinds) == rewinds_after_bound, "no new rewind for a retired batch"
    assert len(runner.advances) == advances_after_bound + 3, "each later failure advances"


@pytest.mark.asyncio
async def test_dead_chat_still_drops_the_subscription(monkeypatch):
    """The MAX_SEND_FAILURES drop path is unchanged: a dead chat is unsubscribed."""
    monkeypatch.setattr(notifier, "_to_thread_process_service", _immediate)
    runner = _FakeRunner()
    fail_counts, batch_rewinds = {}, {}

    for _ in range(notifier.MAX_SEND_FAILURES):
        await _fail_once(runner, fail_counts=fail_counts, batch_rewinds=batch_rewinds)

    assert len(runner.unsubs) == 1, "a persistently dead chat is dropped after MAX_SEND_FAILURES"


@pytest.mark.asyncio
async def test_the_rewind_map_cannot_grow_without_bound(monkeypatch):
    """The per-batch counter map is pruned, so it cannot leak across ticks."""
    monkeypatch.setattr(notifier, "_to_thread_process_service", _immediate)
    runner = _FakeRunner()
    fail_counts, batch_rewinds = {}, {}

    for cursor in range(50000, 50200):
        await _fail_once(runner, cursor=cursor, old_cursor=cursor - 1,
                         fail_counts=fail_counts, batch_rewinds=batch_rewinds)

    assert len(batch_rewinds) <= 64
