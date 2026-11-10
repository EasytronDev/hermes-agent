"""How a kanban worker is LAUNCHED: its command, its log file, its scope.

Split out of ``hermes_cli.kanban_db_dispatch`` — the dispatcher's own size is
already past the repo's file-size cap, and "who gets a worker" (the dispatch
decision) is a different topic from "what command runs the worker" (the
launch). ``AGENTS.md``: new behaviour never goes into a facade, and a unit
that is already over target may only go down.

The functions here are reached through the dispatch module (``kbd._worker_argv``
and friends), so a test that monkeypatches them on ``kanban_db_dispatch`` keeps
working: the sibling resolves them through that module at call time.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hermes_cli.kanban_db import Task

# Pre-tag worker rows in state.db are reclaimed so they leave the session
# lists. Best-effort: the durable gate is ``state_meta`` in
# ``retag_kanban_worker_sessions``; the in-process set avoids reopening
# state.db on every spawn. A tick must never fail because a session DB was
# busy or missing.
_retagged_workspace_roots: set[str] = set()


def _retag_legacy_worker_sessions(workspaces_root_path: str) -> None:
    """Reclaim pre-tag worker rows in state.db so they leave the session lists.

    Best-effort: the durable gate is ``state_meta`` in
    ``retag_kanban_worker_sessions``; the in-process set avoids reopening
    state.db on every spawn. A tick must never fail because a session DB was
    busy or missing.
    """
    if workspaces_root_path in _retagged_workspace_roots:
        return
    try:
        from hermes_state_registry import acquire, release_or_close

        # Inside the gateway the dispatcher shares the process's registry handle; a bare
        # SessionDB() here was one more writer connection on the same state.db (#100896).
        db = acquire()
        try:
            db.retag_kanban_worker_sessions(workspaces_root_path)
        finally:
            release_or_close(db)
        _retagged_workspace_roots.add(workspaces_root_path)
    except Exception as exc:
        from hermes_cli import kanban_db as _kb
        _kb._log.debug("kanban worker: legacy session retag skipped (%s)", exc)


def _worker_argv(task: "Task", profile_arg: str, hermes_home: Optional[str]) -> list[str]:
    """The profile owner executes; this subprocess only waits for its receipt."""
    import sys
    return [sys.executable, "-m", "hermes_cli.kanban_worker_client"]


def _open_worker_log(task: "Task", board: Optional[str]):
    """Append-mode per-task log (a re-run on unblock appends, never overwrites),
    rotated first. Anchored at the board root (not the shared kanban root) so
    `hermes kanban log` reads its own file and boards sharing task ids don't
    collide."""
    from hermes_cli import kanban_db as _kb
    log_dir = _kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    from hermes_cli.kanban_worker_log import rotate_worker_log, worker_log_rotation_config
    rotate_worker_log(log_path, *worker_log_rotation_config())
    return open(log_path, "ab")


def _restart_safe_worker_argv(task: "Task", command: list[str]) -> list[str]:
    """Wrap a systemd-hosted dispatcher's worker in the shared restart-safe scope.

    Kanban workers are long-lived agentic runs that outlive the dispatcher
    tick, so they never take cron's degraded mode under the managed gateway:
    ``require_restart_safe_scope=True`` makes the helper raise
    ``RestartSafeScopeUnavailable`` there (an infrastructure spawn failure the
    dispatcher does not charge to the card). Under any other systemd unit
    (``Type=oneshot`` dispatch timers, #113612) ``outlives_parent=True`` gets the
    worker its own scope so the unit's cgroup teardown cannot kill it.
    """
    from tools.process_registry import restart_safe_gateway_child_argv

    if task.current_run_id is None:
        # Outside managed systemd this is harmless, but a managed dispatch must
        # never mint an untraceable worker.  Check topology through the shared
        # helper first, using a placeholder suffix that cannot be launched.
        dispatch = restart_safe_gateway_child_argv(
            command,
            unit_suffix=f"kanban-{task.id}-run-missing",
            require_restart_safe_scope=True,
            outlives_parent=True,
        )
        if dispatch.mode != "in_process":
            raise RuntimeError(
                "cannot create restart-safe systemd scope for Kanban worker: "
                "the claimed task has no current run id"
            )
        return command

    return restart_safe_gateway_child_argv(
        command,
        unit_suffix=f"kanban-{task.id}-run-{task.current_run_id}",
        require_restart_safe_scope=True,
        outlives_parent=True,
    ).argv
