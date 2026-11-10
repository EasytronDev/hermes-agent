"""The no-contract barrier: a card with nothing to execute never becomes a worker.

Incident class (measured 2026-11-10, board ``strategy-processor-sprint``):
a probe card was created with an empty ``body`` and an empty ``created_by``;
the script that created it died, leaving the card ``running`` with a dead
claim lock. 900 s later the TTL reclaim put it back in ``ready`` — and the very
next dispatcher tick CLAIMED it and SPAWNED a worker on it. Three workers of
the ``default`` profile (its whole 3/3 lane) burned on cards whose empty body
gave the worker nothing to execute. The kernel blocked those cards only AFTER
the worker slots were spent.

The fix is NOT "stop reclaiming" — the reclaim is what stops a card from
staying stuck forever when its worker dies. The fix is:

    reclaim, but do not DISPATCH a card with no contract.

This module holds the barrier. It lives next to the dispatcher rather than in
it so the dispatcher's own size does not grow for a guard that has its own
topic — and so the guard can be tested and reasoned about on its own.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def is_no_contract(row: sqlite3.Row) -> bool:
    """True when a card carries NO execution contract.

    A card's contract is its ``body`` (what the worker is asked to do) together
    with ``created_by`` (who asked for it). When BOTH are empty there is
    nothing for a worker to execute: it gets no spec and no provenance, so the
    only thing a spawned worker can do is burn its slot and return an empty
    result — the kernel then blocks the card *after* the worker has already
    consumed the slot.

    The discriminator is deliberately the CONJUNCTION of both empties, not
    either one alone:

    * the 74 legacy no-body cards on the ``default`` board all carry a
      ``created_by`` — they are legitimate cards whose body was never filled
      in, and the dispatcher must keep spawning them;
    * cards with a body but no ``created_by`` exist too (``body`` is the
      contract; ``created_by`` only adds provenance).

    So only a card that is empty on BOTH axes is refused. Measured on this
    host: 5 such cards, all of them probe cards, and zero legitimate cards.
    """
    body = row["body"]
    created_by = row["created_by"]
    return not (body or "").strip() and not (created_by or "").strip()


def event_payload(row: sqlite3.Row) -> dict:
    """Payload for the ``skipped_no_contract`` event — names what was empty."""
    return {
        "reason": "no_execution_contract",
        "body_empty": not (row["body"] or "").strip(),
        "created_by_empty": not (row["created_by"] or "").strip(),
    }


def record_skip_event_once(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: dict,
    *,
    dry_run: bool,
) -> None:
    """Write a ``skipped_*`` event ONCE per card, not once per dispatcher tick.

    The condition behind these events never resolves on its own, so a repeat
    would only spam the board. The event is rewritten only when something else
    happened on the card since (an edit that filled the body, a reassign, a
    comment) — the payload then differs.
    """
    if dry_run:
        return
    from hermes_cli import kanban_db as _kb

    with _kb.write_txn(conn):
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
        if (last is None or last["kind"] != kind
                or last["payload"] != _kb._json_or_null(payload)):
            _kb._append_event(conn, task_id, kind, payload)


def refuse_no_contract_card(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    result: Any,
    *,
    dry_run: bool,
) -> bool:
    """Refuse to dispatch a card that carries NO execution contract.

    A card with an empty ``body`` AND an empty ``created_by`` gives a worker
    nothing to execute: it burns a worker slot, returns an empty result, and
    the kernel blocks the card only AFTER the slot was spent. That is how three
    workers of the ``default`` profile (its whole 3/3 lane) were lost to probe
    cards on the live board.

    This barrier sits BEFORE the claim, so the card is left in ``ready`` with
    no claim lock: there is no lock to dangle and the reclaim machinery is
    untouched. A card WITH a contract whose worker dies is still reclaimed by
    ``release_stale_claims`` exactly as before — the reclaim is what stops a
    card from staying stuck forever, and this never disables it. The guard only
    refuses to DISPATCH a card that could not be executed.

    It lives in the dispatcher, not only at creation time, so it also covers
    cards written straight into the board by SQL.

    ``result`` is the dispatcher's ``DispatchResult``; the refusal is recorded
    in its ``skipped_no_contract`` bucket so telemetry can name the hold.

    Returns True when the card was refused (no spawn slot consumed).
    """
    if not is_no_contract(row):
        return False
    task_id = row["id"]
    result.skipped_no_contract.append(task_id)
    record_skip_event_once(
        conn, task_id, "skipped_no_contract", event_payload(row), dry_run=dry_run)
    return True
