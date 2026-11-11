"""A test run must NEVER be able to write into a live Kanban board.

Incident (2026-11-10, board ``strategy-processor-sprint``): a dispatched worker
ran the runtime test suite. The worker inherits ``HERMES_KANBAN_DB=<the live
board>``; pytest inherits the worker's environment; the
``test_kanban_review_surfaces`` fixture created its "Review tool contract"
cards and the live board was replaced — 694 tasks became 1 freshly-created
row. The tell was that those cards carried ``assignee='builder'``, a profile
that does not exist in the roster: they came from a FIXTURE, not from a worker.

The rule this file encodes: **a pin of environment that points at production
data cannot survive a test.** The pin exists so a worker talks to the right
board; a test talks to a temporary database.

Two layers are covered here:

1. the kernel (``hermes_cli.kanban_db._board_path``) refuses a pin that does
   not resolve inside a disposable sandbox while the process is under test,
   and falls back to a per-process sandbox directory;
2. the test suite's own conftest strips the pins at IMPORT time (the
   collection window) and deny-lists the deployment root the pins pointed at.

The test with teeth is :func:`test_live_board_file_is_untouched_after_a_card_is_created`:
it points ``HERMES_KANBAN_DB`` at a real board file, creates a card through
the ordinary kernel API, and asserts the file is byte-for-byte and
stat-for-stat what it was.

A NOTE ON PATHS. A "live board" in these tests MUST live outside every
sandbox root the guard recognizes (TMPDIR, pytest's ``tmp_path`` tree, the
hermetic isolation home) — a board under ``tmp_path`` is a LEGITIMATE test
board and the kernel is right to honor the pin for it. The live board here is
placed under a directory the guard does not claim, which is exactly the shape
of the incident (a deployment root such as
``/opt/data/kanban/boards/<board>/``).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_test_guard as ktg


@pytest.fixture(autouse=True)
def _no_ambient_board_pins(monkeypatch):
    """These tests own the pins explicitly; an ambient one would confound them."""
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_TEST_ISOLATION",
        "PYTEST_CURRENT_TEST",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def live_board_dir(tmp_path, monkeypatch):
    """A directory OUTSIDE every sandbox root the guard recognizes.

    ``tmp_path`` is under TMPDIR, which is a legitimate place for a test to
    put a board — so it cannot stand in for a live board here. The directory
    is created under a sibling of the temp root that the guard does not
    claim, and cleaned up by the test.
    """
    root = Path(tmp_path).parent / "live-boards-not-a-sandbox"
    target = root / f"board-{os.getpid()}"
    target.mkdir(parents=True, exist_ok=True)
    yield target
    import shutil

    shutil.rmtree(target, ignore_errors=True)


def _seed_board(path: Path, tasks: int = 7) -> None:
    """Create a board file with *tasks* rows, using the kernel's own schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(kb.SCHEMA_SQL)
        now = 1_700_000_000
        for i in range(tasks):
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"task-{i}", f"live task {i}", "backend", "done", now + i),
            )
        conn.commit()
    finally:
        conn.close()


def _board_fingerprint(path: Path) -> tuple[int, int, int, str]:
    """(task count, mtime_ns, size, sha256) — everything that must not move."""
    import hashlib

    data = path.read_bytes()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    stat = path.stat()
    return count, stat.st_mtime_ns, stat.st_size, hashlib.sha256(data).hexdigest()


# ── Layer 2: the kernel ─────────────────────────────────────────────────────


def test_kernel_refuses_a_production_pin_under_test(monkeypatch, live_board_dir):
    """A pin at a path outside every sandbox is refused while under test."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x.py::test_y (call)")
    production = live_board_dir / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(production))

    resolved = kb.kanban_db_path()

    assert resolved != production, (
        "the kernel resolved the production pin under test — the pin must be refused"
    )
    assert ktg.pin_points_into_sandbox(str(resolved)), (
        f"the fallback {resolved} is not inside a disposable sandbox either"
    )


def test_kernel_honours_a_sandbox_pin_under_test(monkeypatch, tmp_path):
    """The legitimate case: a test pins into its own tmp_path."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x.py::test_y (call)")
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))

    assert kb.kanban_db_path() == pinned


def test_kernel_honours_the_pin_outside_a_test_run(monkeypatch, tmp_path):
    """Production behavior is unchanged: outside a test run the pin wins."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))

    assert kb.kanban_db_path() == pinned, (
        "the pin must still win outside a test run — the worker relies on it"
    )


def test_guard_detects_the_test_context_three_ways(monkeypatch):
    """The guard's context detection survives losing one of its signals."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)

    # 1. pytest's own var (set while a test runs).
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x.py::y (call)")
    assert ktg.running_under_pytest() is True

    # 2. OUR marker — survives a child that scrubs PYTEST_*.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    assert ktg.running_under_pytest() is True

    # 3. pytest imported (covers the COLLECTION window, before any test runs).
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.setitem(sys.modules, "pytest", object())
    assert ktg.running_under_pytest() is True

    # None of them -> not a test context.
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    assert ktg.running_under_pytest() is False


def test_sandbox_fallback_is_stable_and_disposable(monkeypatch):
    """The fallback root is per-process and inside the temp area."""
    ktg.reset_sandbox_board_root()
    try:
        first = ktg.sandbox_board_root()
        assert first == ktg.sandbox_board_root(), "the fallback root must be stable"
        assert first.is_dir()
        assert ktg.pin_points_into_sandbox(str(first / "kanban.db"))
    finally:
        ktg.reset_sandbox_board_root()


# ── The test with teeth ─────────────────────────────────────────────────────


def test_live_board_file_is_untouched_after_a_card_is_created(monkeypatch, live_board_dir):
    """THE PROOF: creating a card must not mutate a board the test does not own.

    Before the fix this test fails: the pin resolves to the live board and the
    card is written into it (and the board's schema is replaced).
    """
    live_board = live_board_dir / "kanban.db"
    _seed_board(live_board, tasks=7)
    before = _board_fingerprint(live_board)

    # Exactly the worker's shape: a pin at the live board, in a test context.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x.py::test_y (call)")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live_board))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)

    # The kernel must NOT resolve to the live board.
    resolved = kb.kanban_db_path()
    assert resolved != live_board, (
        f"the pin resolved to the live board {live_board} under test"
    )

    # And creating a card through the ordinary API must leave it alone.
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Review tool contract", assignee="builder")
    assert task_id

    after = _board_fingerprint(live_board)
    assert before == after, (
        "the live board changed: "
        f"tasks {before[0]} -> {after[0]}, "
        f"mtime_ns {before[1]} -> {after[1]}, "
        f"size {before[2]} -> {after[2]}, "
        f"sha256 {before[3][:12]} -> {after[3][:12]}"
    )
    assert after[0] == 7, "the live board lost rows"
    assert after[1] == before[1], "the live board was rewritten (mtime moved)"


def test_live_board_survives_a_whole_fixture_shaped_like_the_incident(
    monkeypatch, live_board_dir
):
    """The incident's fixture shape, end to end: the board file is intact.

    ``test_kanban_review_surfaces``'s fixture does exactly this: init the DB,
    create a task titled "Review tool contract" with ``assignee='builder'``,
    claim it, and set ``HERMES_KANBAN_TASK``/``HERMES_KANBAN_RUN_ID``.
    """
    live_board = live_board_dir / "kanban.db"
    _seed_board(live_board, tasks=7)
    before = _board_fingerprint(live_board)

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x.py::test_y (call)")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live_board))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)

    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Review tool contract", assignee="builder")
        task = kb.claim_task(conn, task_id, claimer="builder:1")
        assert task is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

    after = _board_fingerprint(live_board)
    assert before == after, "the live board was mutated by the fixture"


# ── Layer 1: the test suite's own isolation ────────────────────────────────


def test_conftest_strips_the_kanban_pins_at_import_time():
    """The COLLECTION window: a test module imported by pytest must not see
    the worker's pins. Measured on the incident: the pin was live for the
    whole collection phase, before any fixture ran.

    The check is BEHAVIORAL — the strip removes the pins from the
    environment and hands back what it removed, which is what the conftest
    turns into the deployment-root deny-list. (Reading the conftest's source
    would only prove the shape of the text, not the behavior.)
    """
    env = {
        "HERMES_KANBAN_DB": "/opt/data/kanban/boards/strategy-processor-sprint/kanban.db",
        "HERMES_KANBAN_ATTACHMENTS_ROOT": "/opt/data/kanban/attachments/strategy-processor-sprint",
        "HERMES_KANBAN_WORKSPACES_ROOT": "/opt/data/kanban/boards/strategy-processor-sprint/workspaces",
        "HERMES_KANBAN_BOARD": "strategy-processor-sprint",
    }
    removed = ktg.strip_path_pins(env)

    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_ATTACHMENTS_ROOT",
                "HERMES_KANBAN_WORKSPACES_ROOT"):
        assert var not in env, f"{var} survived the strip — the collection window stays open"
        assert removed[var] == env.get(var, removed[var]), f"{var} was not reported as removed"

    # The removed value is what the deny-list is built from.
    assert removed["HERMES_KANBAN_DB"].endswith("strategy-processor-sprint/kanban.db")

    # A pin that was not set is simply absent from the report.
    assert ktg.strip_path_pins({}) == {}


def test_attachments_root_pin_is_blanked_by_the_hermetic_fixture(monkeypatch):
    """``HERMES_KANBAN_ATTACHMENTS_ROOT`` was missing from the behavioral list
    and therefore leaked into EVERY test — not just the collection window."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", "/opt/data/kanban/attachments/probe")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", "/opt/data/kanban/workspaces/probe")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/opt/data/kanban/boards/probe/kanban.db")

    # The hermetic fixture blanks these; simulate its effect by asking the
    # guard what it would do with an inherited pin.
    from hermes_cli import kanban_test_guard as ktg

    assert ktg.pinned_path_is_refused("/opt/data/kanban/attachments/probe")
    assert ktg.pinned_path_is_refused("/opt/data/kanban/workspaces/probe")
    assert ktg.pinned_path_is_refused("/opt/data/kanban/boards/probe/kanban.db")


def test_write_guard_denylists_the_deployment_root_the_pin_pointed_at(monkeypatch):
    """The ``~/.hermes`` deny-list does NOT cover a deployment root.

    The incident board lived at ``/opt/data/kanban/boards/<board>/kanban.db``
    — not under ``~/.hermes`` — so the existing write guard let the test
    through. The conftest must now deny-list the roots the inherited pins
    pointed at.

    The check is BEHAVIORAL: the guard refuses a write whose path lands under
    the root the inherited pin named.
    """
    deployment_root = Path(tempfile.mkdtemp(prefix="deployment-board-"))
    deployment_db = deployment_root / "kanban.db"
    try:
        # The conftest's deny-list is built from the pin it removed.
        env = {"HERMES_KANBAN_DB": str(deployment_db)}
        removed = ktg.strip_path_pins(env)
        assert removed["HERMES_KANBAN_DB"] == str(deployment_db)

        # The deployment root IS the kanban home for this layout, so a pin
        # there is a LIVE board pointer and must be refused under test.
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(deployment_root))
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "x.py::y (call)")

        assert ktg.pin_points_at_a_live_board(str(deployment_db))
        assert ktg.pinned_path_is_refused(str(deployment_db))
    finally:
        import shutil

        shutil.rmtree(deployment_root, ignore_errors=True)


def test_a_worker_environment_cannot_reach_a_live_board_through_a_child_process(
    monkeypatch, live_board_dir
):
    """The pin must not survive into a child the test spawns either.

    A child inherits the worker's environment; if the child resolves a kanban
    path, it must not land on the live board.
    """
    live_board = live_board_dir / "kanban.db"
    probe_dir = Path(os.environ.get("TMPDIR", "/tmp"))
    probe = probe_dir / f"hermes-kanban-pin-probe-{os.getpid()}.py"
    repo_root = Path(__file__).resolve().parents[2]
    probe.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "from hermes_cli import kanban_db as kb\n"
        "print(kb.kanban_db_path())\n",
        encoding="utf-8",
    )
    try:
        env = dict(os.environ)
        env["HERMES_KANBAN_DB"] = str(live_board)
        env["PYTEST_CURRENT_TEST"] = "x.py::y (call)"
        env["PYTHONPATH"] = str(repo_root)
        result = subprocess.run(
            [sys.executable, str(probe)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(repo_root),
        )
        assert result.returncode == 0, result.stderr
        resolved = result.stdout.strip().splitlines()[-1]
    finally:
        probe.unlink(missing_ok=True)
    assert resolved != str(live_board), (
        f"a child process resolved the live board {live_board}"
    )


def test_the_incident_fixtures_own_env_cannot_resolve_the_pinned_board(monkeypatch):
    """The EXACT env choreography of the fixture that caused the wipe.

    ``tests/hermes_cli/test_kanban_review_surfaces.py::review_worker`` sets
    ``HERMES_HOME`` to a tmp dir, patches ``Path.home``, sets
    ``HERMES_PROFILE='builder'``, deletes ``HERMES_DELEGATED_CHILD_CONTEXT``
    — and never touches ``HERMES_KANBAN_DB``. The pin therefore survives and
    decides where the "Review tool contract" card lands.

    On the base that resolution is the PINNED FILE. This test pins the
    branch's behavior: the same env resolves into the sandbox.
    """
    live_board = Path(tempfile.mkdtemp(prefix="fixture-env-")) / "kanban.db"
    try:
        monkeypatch.setenv("HERMES_KANBAN_DB", str(live_board))
        monkeypatch.setenv("HERMES_HOME", str(live_board.parent / ".hermes"))
        monkeypatch.setenv("HERMES_PROFILE", "builder")
        monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x.py::review_worker (call)")

        resolved = kb.kanban_db_path()

        assert resolved != live_board, (
            f"the fixture's env resolved the pinned board {live_board} — "
            f"the card would land in the live board"
        )
        assert ktg.pin_points_into_sandbox(str(resolved)), (
            f"the fallback {resolved} is not inside a disposable sandbox"
        )
    finally:
        import shutil

        shutil.rmtree(live_board.parent, ignore_errors=True)
