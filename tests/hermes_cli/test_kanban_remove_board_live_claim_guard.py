"""Invariant: ``remove_board`` never destroys a board that holds a card in flight.

A board removal takes the whole board directory — DB, workspaces, logs — with
it. When the dispatcher had just spawned a worker against a card on that
board, the card vanished from every database while the worker kept running
against it. Measured four times in one evening across two different boards
(``t_ec405bea``, ``t_65f193a1``, ``t_d74a3b5e``, ``t_f180ff45``): the
gateway logged "kanban DB … lost its schema after this process initialized it
(deleted or replaced externally); re-initializing", and the board came back
with 0 tasks.

The core already knows what "a card in flight" means — ``complete_task``
refuses a live claim, ``assign_task`` refuses a running task, ``LiveClaimError``
documents the fence. ``remove_board`` was the one path left out, so this card
adds the same fence there: a board with a task still ``running`` under a live
worker claim is REFUSED (archive and hard delete alike) instead of destroyed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _live_running_task(board: str, *, live_worker: bool = True) -> str:
    """Create a task on ``board``, claim it and (optionally) mark it as having a
    live worker process. This process stands in for the spawned worker: alive
    and fingerprinted, so the guard treats the claim as live."""
    with kbc.connect_closing(board=board) as conn:
        tid = kb.create_task(conn, title="in flight", assignee="devops")
        assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
        if live_worker:
            kbd._set_worker_pid(conn, tid, os.getpid())
    return tid


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

class TestRemoveBoardLiveClaimGuard:

    @pytest.mark.parametrize("archive", [True, False])
    def test_remove_board_refuses_board_with_live_task(self, fresh_home, archive):
        """Both archive and hard delete are refused while a card is in flight."""
        kb.create_board("inflight")
        tid = _live_running_task("inflight")

        db_path = kb.board_dir("inflight") / "kanban.db"
        before = db_path.stat().st_mtime_ns

        with pytest.raises(ValueError) as excinfo:
            kb.remove_board("inflight", archive=archive)
        msg = str(excinfo.value)
        assert tid in msg
        assert "refusing to" in msg

        # The card survived and the board was NOT touched.
        with kbc.connect_closing(board="inflight") as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row is not None
        assert row["status"] == "running"
        assert kb.board_exists("inflight")
        assert db_path.exists()
        assert db_path.stat().st_mtime_ns == before

    def test_refusal_raises_the_dedicated_error_type(self, fresh_home):
        """The refusal must be a ``LiveBoardTaskError`` — a ``ValueError``
        subclass, so the CLI's existing ``except ValueError`` renders it as a
        clean operator message instead of a traceback, while callers can still
        catch the specific type to tell "board has live work" apart from every
        other removal failure."""
        kb.create_board("typed")
        tid = _live_running_task("typed")

        with pytest.raises(kb.LiveBoardTaskError) as excinfo:
            kb.remove_board("typed", archive=False)
        exc: kb.LiveBoardTaskError = excinfo.value
        assert isinstance(exc, ValueError)
        assert exc.slug == "typed"
        assert exc.task_ids == [tid]
        assert exc.action == "delete"
        assert tid in str(exc)

    def test_refusal_does_not_recreate_the_db(self, fresh_home):
        """The board's DB must not be re-created (the "lost its schema"
        re-initialisation that marked the incident in the gateway log)."""
        kb.create_board("inflight")
        tid = _live_running_task("inflight")
        db_path = kb.board_dir("inflight") / "kanban.db"
        inode_before = db_path.stat().st_ino

        with pytest.raises(ValueError):
            kb.remove_board("inflight", archive=False)

        assert db_path.exists()
        assert db_path.stat().st_ino == inode_before
        with kbc.connect_closing(board="inflight") as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id = ?", (tid,)
            ).fetchone()[0] == 1

    def test_guard_also_covers_an_archived_task_with_an_open_run(self, fresh_home):
        """A task row flipped to ``archived`` while its RUN stayed open still
        has a live worker executing — the board must not be destroyed. This is
        the exact state measured on ``e2e-fixa2-test``: ``tasks.status =
        'archived'`` with ``task_runs.status = 'running'``."""
        kb.create_board("archived-run")
        tid = _live_running_task("archived-run")

        # Flip only the TASK row to archived, leaving the run row running —
        # mirrors the harness's "archive the stale card to free the
        # idempotency_key" path.
        with kbc.connect_closing(board="archived-run") as conn:
            conn.execute(
                "UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,)
            )
            run = conn.execute(
                "SELECT status FROM task_runs WHERE task_id = ?", (tid,)
            ).fetchone()
        assert run["status"] == "running"

        with pytest.raises(ValueError) as excinfo:
            kb.remove_board("archived-run", archive=False)
        assert tid in str(excinfo.value)
        assert kb.board_exists("archived-run")

    def test_dead_worker_does_not_block_removal(self, fresh_home):
        """A ``running`` card whose worker process is gone protects no live
        run — the board must still be removable, or a crashed worker would
        wedge its board forever."""
        kb.create_board("dead-worker")
        tid = _live_running_task("dead-worker", live_worker=False)
        # Point the claim at a PID that is not alive.
        with kbc.connect_closing(board="dead-worker") as conn:
            conn.execute(
                "UPDATE tasks SET worker_pid = 999999, worker_started_at = NULL "
                "WHERE id = ?", (tid,)
            )
        res = kb.remove_board("dead-worker", archive=False)
        assert res["action"] == "deleted"
        assert not kb.board_exists("dead-worker")

    def test_dead_worker_does_not_block_removal_even_with_a_live_run_row(self, fresh_home):
        """The run row keeps the worker's pid after the task row's pid was
        patched. A crashed worker therefore leaves BOTH rows pointing at a
        dead pid — the board must still be removable. This is the exact
        wedge the first implementation shipped: it honoured the run row
        unconditionally and a dead worker locked its board forever."""
        kb.create_board("dead-worker-run")
        tid = _live_running_task("dead-worker-run")
        with kbc.connect_closing(board="dead-worker-run") as conn:
            conn.execute(
                "UPDATE tasks SET worker_pid = 999999, worker_started_at = NULL "
                "WHERE id = ?", (tid,)
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = 999999, worker_started_at = NULL "
                "WHERE task_id = ?", (tid,)
            )
        res = kb.remove_board("dead-worker-run", archive=False)
        assert res["action"] == "deleted"
        assert not kb.board_exists("dead-worker-run")

    def test_superseded_run_row_does_not_block_removal(self, fresh_home):
        """A run row left ``running`` that is NO LONGER the task's
        ``current_run_id`` was superseded by a reclaim: no worker executes it.
        Honouring it would wedge the board behind a run nobody is running.

        The task row itself must be terminal here — otherwise the block comes
        from the task query and the test would not exercise the run path."""
        kb.create_board("superseded")
        tid = _live_running_task("superseded")
        with kbc.connect_closing(board="superseded") as conn:
            # A second, older run: still 'running', with a live pid, but the
            # task no longer points at it.
            conn.execute(
                "INSERT INTO task_runs (task_id, status, worker_pid, started_at) "
                "VALUES (?, 'running', ?, ?)",
                (tid, os.getpid(), 1),
            )
            # The task row is terminal and points at the ORIGINAL run.
            conn.execute(
                "UPDATE tasks SET status = 'done', current_run_id = NULL, "
                "worker_pid = NULL, worker_started_at = NULL WHERE id = ?",
                (tid,),
            )
        res = kb.remove_board("superseded", archive=False)
        assert res["action"] == "deleted"
        assert not kb.board_exists("superseded")


# ---------------------------------------------------------------------------
# Legitimate removal must keep working (no false positives)
# ---------------------------------------------------------------------------

class TestRemoveBoardStillWorks:

    def test_remove_empty_board(self, fresh_home):
        kb.create_board("empty")
        res = kb.remove_board("empty", archive=False)
        assert res["action"] == "deleted"
        assert not kb.board_exists("empty")

    def test_remove_board_with_done_tasks(self, fresh_home):
        kb.create_board("finished")
        with kbc.connect_closing(board="finished") as conn:
            tid = kb.create_task(conn, title="done", assignee="devops")
        with kbc.connect_closing(board="finished") as conn:
            assert kb.complete_task(conn, tid, result="done") is True
        res = kb.remove_board("finished", archive=False)
        assert res["action"] == "deleted"
        assert not kb.board_exists("finished")

    def test_remove_board_with_archived_tasks(self, fresh_home):
        kb.create_board("arch")
        with kbc.connect_closing(board="arch") as conn:
            tid = kb.create_task(conn, title="arch", assignee="devops")
        with kbc.connect_closing(board="arch") as conn:
            assert kb.archive_task(conn, tid) is True
        res = kb.remove_board("arch", archive=False)
        assert res["action"] == "deleted"

    def test_archive_board_still_works(self, fresh_home):
        kb.create_board("archivable")
        res = kb.remove_board("archivable", archive=True)
        assert res["action"] == "archived"
        # Upstream leaves an ``archived`` tombstone at the original slug (the
        # #43243 resurrect-guard: a stale dashboard tab keeps calling
        # connect(board=slug) and must not resurrect an empty board). So the
        # slug still "exists" as a tombstone — what matters here is that the
        # board's live content moved into ``_archived/`` and the tombstone is
        # flagged archived.
        assert kb.board_exists("archivable")
        meta = kb.read_board_metadata("archivable")
        assert meta.get("archived") is True
        # The archived copy holds the board directory.
        assert Path(res["new_path"]).exists()

    def test_default_board_cannot_be_removed(self, fresh_home):
        with pytest.raises(ValueError):
            kb.remove_board("default", archive=False)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Keep the inherited PYTHONPATH (the runtime's site-packages) so the
    # subprocess resolves the same third-party deps as this process.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_WORKTREE), os.environ.get("PYTHONPATH", "")) if p
    )
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=60,
    )


class TestCLIGuard:

    def test_cli_boards_rm_delete_refuses_live_task(self, fresh_home):
        env = {"HERMES_HOME": str(fresh_home)}
        assert _cli(["boards", "create", "cliinflight"], env_extra=env).returncode == 0

        # Create + claim + mark a live worker through the library (the CLI has
        # no "claim with a live worker" verb).
        tid = _live_running_task("cliinflight")

        r = _cli(["boards", "rm", "cliinflight", "--delete"], env_extra=env)
        assert r.returncode != 0
        assert tid in (r.stderr + r.stdout)
        assert "refusing to delete" in (r.stderr + r.stdout)

        # The board and the card are still there.
        assert kb.board_exists("cliinflight")
        with kbc.connect_closing(board="cliinflight") as conn:
            assert conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (tid,)
            ).fetchone()["status"] == "running"

    def test_cli_boards_rm_archive_refuses_live_task(self, fresh_home):
        env = {"HERMES_HOME": str(fresh_home)}
        assert _cli(["boards", "create", "cliarch"], env_extra=env).returncode == 0
        tid = _live_running_task("cliarch")

        r = _cli(["boards", "rm", "cliarch"], env_extra=env)
        assert r.returncode != 0
        assert tid in (r.stderr + r.stdout)
        assert kb.board_exists("cliarch")

    def test_archive_refusal_does_not_tell_the_operator_to_archive(self, fresh_home):
        """The archive refusal must not end with "or archive the board instead"
        — the action it just refused IS the archive. A self-contradictory
        message sends the operator down the very path that was fenced."""
        kb.create_board("arch-msg")
        tid = _live_running_task("arch-msg")

        with pytest.raises(ValueError) as excinfo:
            kb.remove_board("arch-msg", archive=True)
        msg = str(excinfo.value)
        assert "refusing to archive" in msg
        assert "or archive the board instead" not in msg
        assert tid in msg
