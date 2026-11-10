"""The dispatcher must not DISPATCH a card that carries no execution contract.

Incident class (measured 2026-11-10, board ``strategy-processor-sprint``):
a probe card was created with an empty ``body`` and an empty ``created_by``,
the script that created it died, and the card was left ``running`` with a
dead claim lock. 900 s later the TTL reclaim put it back in ``ready`` — and
the very next dispatcher tick CLAIMED it and SPAWNED a worker on it. Three
workers of the ``default`` profile (its whole 3/3 lane) burned on cards
whose empty body gave the worker nothing to execute.

The fix is NOT "stop reclaiming" — the reclaim is what stops a card from
staying stuck forever when its worker dies. The fix is:

    reclaim, but do not DISPATCH a card with no contract.

These three tests measure exactly that, on an isolated board:

1. a REAL card whose worker dies is still reclaimed and returns to the queue
   with its body intact;
2. a PROBE card (body empty AND created_by empty) is reclaimed — its lock
   does not dangle — but never gains a worker;
3. no LEGACY card is affected: the 74 no-body cards on the ``default`` board
   all carry a ``created_by``, and they must keep spawning.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """An isolated board — never the live board (the incident came from
    creating probe cards on the live board).

    The isolation has to be explicit: a worker process inherits
    ``HERMES_KANBAN_DB`` (which pins the LIVE board) and
    ``HERMES_DELEGATED_CHILD_CONTEXT`` (which makes the kanban layer refuse
    every mutation). Both are unset here, so ``kb.init_db()`` creates a fresh
    board under the temporary ``HERMES_HOME`` and the test can never touch
    production.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_probe_card(conn: sqlite3.Connection, *, title: str = "probe") -> str:
    """Create a card with NO execution contract — body empty AND created_by empty.

    Inserted with raw SQL on purpose: that is how the incident card was made
    (a script writing straight into the board), and it is the case the
    dispatcher-side guard has to cover — a creation-time guard alone would not
    see it.
    """
    now = int(time.time())
    task_id = f"t_probe_{now}_{id(conn) % 100000}"
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, "
        "                    created_at, workspace_kind) "
        "VALUES (?, ?, '', 'default', 'ready', 0, '', ?, 'scratch')",
        (task_id, title, now),
    )
    conn.commit()
    return task_id


def _expire_claim(conn: sqlite3.Connection, task_id: str) -> None:
    """Age the card's claim lock past the TTL, exactly as a dead worker would."""
    conn.execute(
        "UPDATE tasks SET claim_expires = ?, worker_pid = NULL WHERE id = ?",
        (int(time.time()) - 100, task_id),
    )
    conn.commit()


def _row(conn: sqlite3.Connection, task_id: str):
    return conn.execute(
        "SELECT status, claim_lock, claim_expires, body, created_by, worker_pid "
        "FROM tasks WHERE id = ?", (task_id,)).fetchone()


# ---------------------------------------------------------------- STEP 2.1
def test_real_card_with_dead_worker_is_still_reclaimed_and_requeued(
    kanban_home, all_assignees_spawnable,
):
    """(a) The reclaim machinery is NOT disabled by the no-contract guard.

    A REAL card (body present) whose worker dies is reclaimed by the TTL and
    goes back to ``ready`` with its body intact — and the next tick spawns it
    again. This is the behaviour the incident's reclaim saved, and the guard
    must not cost it.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="real work", body="do the actual thing",
            assignee="alice", created_by="operator",
        )
        assert kb.claim_task(conn, tid) is not None
        _expire_claim(conn, tid)

        # The reclaim: the dead lock is released, the card is back in the queue.
        assert kb.release_stale_claims(conn) == 1
        row = _row(conn, tid)
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
        assert row["claim_expires"] is None
        # The contract survived the reclaim untouched.
        assert row["body"] == "do the actual thing"

        # And the card still gets a worker on the next tick.
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        assert any(r[0] == tid for r in result.spawned)
    finally:
        conn.close()


# ---------------------------------------------------------------- STEP 2.2
def test_probe_card_is_reclaimed_but_never_gains_a_worker(
    kanban_home, all_assignees_spawnable,
):
    """(b) A PROBE card is reclaimed (no dangling lock) but gains NO worker.

    This is the incident, reproduced end to end: create the card, leave it
    ``running`` with a dead lock, let the TTL expire, and let the dispatcher
    tick. The reclaim must happen — otherwise the card is a ghost that stays
    ``running`` forever — but the tick must not spawn a worker for it.
    """
    conn = kbc.connect()
    try:
        tid = _make_probe_card(conn)

        # --- the incident's own shape: the card is RUNNING with a dead lock ---
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = 'probehost:1', "
            "                   claim_expires = ?, worker_pid = NULL WHERE id = ?",
            (int(time.time()) + 900, tid),
        )
        conn.commit()

        # The lock ages out.
        _expire_claim(conn, tid)

        spawned: list = []
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda task, *a, **k: (spawned.append(task.id), 4242)[1],
        )

        # (b1) NO worker was spawned for the probe card.
        assert spawned == []
        assert not any(r[0] == tid for r in result.spawned)

        # (b2) The reclaim DID happen: the lock is gone, so the card is not a
        # ghost stuck in 'running' forever.
        row = _row(conn, tid)
        assert row["claim_lock"] is None
        assert row["claim_expires"] is None
        assert row["status"] == "ready"

        # (b3) The dispatcher reports it in its own bucket and left an event.
        assert tid in result.skipped_no_contract
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind = 'skipped_no_contract'",
            (tid,),
        ).fetchall()
        assert len(events) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------- STEP 2.2b
def test_probe_card_is_never_claimed_even_on_the_first_tick(
    kanban_home, all_assignees_spawnable,
):
    """(b') The guard fires BEFORE the claim, so a probe card never even
    becomes ``running``.

    The barrier sits in front of the claim on purpose: an unclaimed card has
    no lock to dangle, so there is nothing for the reclaim to clean up and
    nothing for the kernel to block afterwards.
    """
    conn = kbc.connect()
    try:
        tid = _make_probe_card(conn)
        spawned: list = []
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda task, *a, **k: (spawned.append(task.id), 4242)[1],
        )
        assert spawned == []
        assert tid in result.skipped_no_contract
        row = _row(conn, tid)
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
        # No run was ever opened for it.
        runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]
        assert runs == 0
    finally:
        conn.close()


# ---------------------------------------------------------------- STEP 2.3
def test_legacy_no_body_cards_still_spawn(
    kanban_home, all_assignees_spawnable,
):
    """(c) ZERO legacy cards are affected.

    The 74 no-body cards on the ``default`` board all carry a ``created_by``:
    they are legitimate cards whose body was never filled in. The guard must
    not touch them, or it would stop 74 real cards from ever running.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="legacy no-body card", body=None,
            assignee="alice", created_by="operator",
        )
        assert _row(conn, tid)["body"] is None
        assert _row(conn, tid)["created_by"] == "operator"

        spawned: list = []
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda task, *a, **k: (spawned.append(task.id), 4242)[1],
        )
        assert spawned == [tid]
        assert tid not in result.skipped_no_contract
    finally:
        conn.close()


def test_card_with_body_but_no_creator_still_spawns(
    kanban_home, all_assignees_spawnable,
):
    """A card with a body but no ``created_by`` still spawns.

    ``body`` IS the contract; ``created_by`` only adds provenance. Refusing
    that card would break cards created by tools that never set a creator.
    """
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="has a body", body="the spec", assignee="alice",
        )
        assert _row(conn, tid)["created_by"] is None

        spawned: list = []
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda task, *a, **k: (spawned.append(task.id), 4242)[1],
        )
        assert spawned == [tid]
        assert tid not in result.skipped_no_contract
    finally:
        conn.close()


# ---------------------------------------------------------------- telemetry
def test_suppression_line_names_the_no_contract_hold(
    kanban_home, all_assignees_spawnable,
):
    """The dispatcher telemetry names the new hold, so a board that is stuck
    on probe cards says WHY instead of showing a bare zero-spawn tick."""
    conn = kbc.connect()
    try:
        tid = _make_probe_card(conn)
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        assert tid in result.skipped_no_contract
        assert "no_contract=1" in kbd.describe_suppression([result])
    finally:
        conn.close()


def test_the_event_is_written_once_not_once_per_tick(
    kanban_home, all_assignees_spawnable,
):
    """The condition never resolves on its own, so the event must not spam the
    board: one event, then silence until something else happens on the card."""
    conn = kbc.connect()
    try:
        tid = _make_probe_card(conn)
        for _ in range(3):
            kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'skipped_no_contract'",
            (tid,),
        ).fetchone()[0]
        assert events == 1
    finally:
        conn.close()
