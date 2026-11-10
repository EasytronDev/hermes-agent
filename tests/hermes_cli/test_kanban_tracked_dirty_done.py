"""The ``tracked_dirty`` warning a completion carries when the workspace has dirt.

A task whose workspace is a git checkout with uncommitted work must NOT be
blocked from completing (a hard block would cycle analysis-only cards), but the
operator has to SEE the uncommitted work before merging. So ``complete_task``
attaches ``metadata['tracked_dirty']`` to the closing run when — and only when
— the workspace really is dirty. A clean checkout gets no key at all, and the
check itself is fail-open: a missing git, a vanished repo or a timeout must
never break a completion.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_connect as kbc


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo with a remote whose history is fully pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _make_worktree(repo: Path, task_id: str, branch: str | None = None) -> Path:
    target = repo / ".worktrees" / task_id
    kbw._ensure_git_worktree(repo, target, branch or f"wt/{task_id}")
    return target


def _worktree_task(conn, repo: Path, title: str = "wt-task") -> tuple[str, Path]:
    """A ``worktree``-kind task whose workspace is a real linked worktree."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    wt = _make_worktree(repo, tid)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, "
            "branch_name=? WHERE id=?",
            (str(wt), f"wt/{tid}", tid),
        )
    return tid, wt


def _run_metadata(conn, task_id: str) -> dict:
    """The metadata of the run that closed *task_id*.

    ``metadata`` is NULL when the completion carried no payload at all — a
    clean workspace leaves the column empty (the key is written ONLY when
    there is real dirt). An empty payload reads as an empty dict.
    """
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None, "the completion did not close a run"
    if row["metadata"] is None:
        return {}
    assert isinstance(row["metadata"], str)
    import json

    return json.loads(row["metadata"])


# ---------------------------------------------------------------------------
# Criterion 1: a dirty worktree completion carries the warning
# ---------------------------------------------------------------------------


def test_dirty_worktree_completion_carries_tracked_dirty(
    kanban_home: Path, repo: Path
) -> None:
    """An untracked file left in the worktree is reported at completion time."""
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)

    dirty = metadata.get("tracked_dirty")
    assert isinstance(dirty, dict), f"no tracked_dirty warning in {metadata!r}"
    assert Path(dirty["worktree"]) == wt
    assert dirty["arquivos"], "the warning listed no files"
    assert any(entry.startswith("wip.txt:") for entry in dirty["arquivos"]), dirty


def test_modified_tracked_file_is_reported(kanban_home: Path, repo: Path) -> None:
    """An unstaged edit to a TRACKED file is reported too, not only untracked ones."""
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "README.md").write_text("changed\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)

    dirty = metadata["tracked_dirty"]
    assert any(entry.startswith("README.md:") for entry in dirty["arquivos"]), dirty


# ---------------------------------------------------------------------------
# Criterion 2: a clean worktree completion has NO warning key
# ---------------------------------------------------------------------------


def test_clean_worktree_completion_has_no_tracked_dirty_key(
    kanban_home: Path, repo: Path
) -> None:
    with kbc.connect_closing() as conn:
        tid, _wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)

    assert "tracked_dirty" not in metadata, metadata


# ---------------------------------------------------------------------------
# Criterion 3: a scratch workspace (no git) does not break the completion
# ---------------------------------------------------------------------------


def test_scratch_workspace_completion_does_not_break(
    kanban_home: Path, tmp_path: Path
) -> None:
    """A scratch workspace is not a repo: the completion still works as before."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="scratch-task", assignee="worker")
        scratch = tmp_path / "scratch-ws"
        scratch.mkdir()
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?",
                (str(scratch), tid),
            )
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)
        task = kb.get_task(conn, tid)

    assert task.status == "done"
    assert "tracked_dirty" not in metadata, metadata


def test_non_git_dir_workspace_completion_does_not_break(
    kanban_home: Path, tmp_path: Path
) -> None:
    """A ``dir`` workspace that is not a git checkout is skipped, not an error."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="plain-dir-task", assignee="worker")
        plain = tmp_path / "plain-dir"
        plain.mkdir()
        (plain / "note.txt").write_text("x\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                (str(plain), tid),
            )
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)
        task = kb.get_task(conn, tid)

    assert task.status == "done"
    assert "tracked_dirty" not in metadata, metadata


# ---------------------------------------------------------------------------
# Criterion 4: the check is fail-open (timeout / missing repo / failing git)
# ---------------------------------------------------------------------------


def test_missing_workspace_directory_does_not_break_completion(
    kanban_home: Path, repo: Path
) -> None:
    """A workspace path that no longer exists must not break the completion."""
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        import shutil

        shutil.rmtree(wt)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)
        task = kb.get_task(conn, tid)

    assert task.status == "done"
    assert "tracked_dirty" not in metadata, metadata


def test_git_failure_is_fail_open(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git invocation that fails yields no warning and no exception."""
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))

        real_run = subprocess.run

        def exploding_run(cmd, *args, **kwargs):
            if (
                isinstance(cmd, (list, tuple))
                and "status" in cmd
                and "--porcelain" in cmd
            ):
                raise OSError("git is broken")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(kbw.subprocess, "run", exploding_run)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)
        task = kb.get_task(conn, tid)

    assert task.status == "done"
    assert "tracked_dirty" not in metadata, metadata


def test_git_timeout_is_fail_open(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``git status`` that hangs past the timeout yields no warning, no hang."""
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))

        real_run = subprocess.run

        def timing_out_run(cmd, *args, **kwargs):
            if (
                isinstance(cmd, (list, tuple))
                and "status" in cmd
                and "--porcelain" in cmd
            ):
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 10))
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(kbw.subprocess, "run", timing_out_run)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)
        task = kb.get_task(conn, tid)

    assert task.status == "done"
    assert "tracked_dirty" not in metadata, metadata


def test_dirty_dir_workspace_is_reported(kanban_home: Path, repo: Path) -> None:
    """A ``dir`` workspace pointing at a git checkout is covered too."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="dir-task", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                (str(repo), tid),
            )
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        (repo / "leftover.txt").write_text("dirty\n", encoding="utf-8")
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)

    dirty = metadata.get("tracked_dirty")
    assert isinstance(dirty, dict), f"no tracked_dirty warning in {metadata!r}"
    assert any(entry.startswith("leftover.txt:") for entry in dirty["arquivos"]), dirty


def test_path_with_a_newline_is_reported_unquoted(
    kanban_home: Path, repo: Path
) -> None:
    """A path with a newline in it is reported as the REAL path, not quoted.

    The line-based porcelain v1 output quotes such a path (``"weird\\nname.txt"``),
    so splitting on lines would report the escaped name; the NUL-separated
    output (``-z``) carries the real path.
    """
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "weird\nname.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)

    dirty = metadata.get("tracked_dirty")
    assert isinstance(dirty, dict), f"no tracked_dirty warning in {metadata!r}"
    assert any(
        entry.startswith("weird\nname.txt:") for entry in dirty["arquivos"]
    ), dirty
    assert not any(
        entry.startswith('"weird') for entry in dirty["arquivos"]
    ), dirty


def test_show_untracked_files_no_does_not_hide_the_dirt(
    kanban_home: Path, repo: Path
) -> None:
    """A repo configured with ``status.showUntrackedFiles=no`` still reports dirt.

    The config would hide every new file; the check overrides it for the
    command it runs, so the warning still names the untracked file.
    """
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "hidden-by-config.txt").write_text("unsaved\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(wt), "config", "status.showUntrackedFiles", "no"],
            check=False,
            capture_output=True,
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)

    dirty = metadata.get("tracked_dirty")
    assert isinstance(dirty, dict), f"no tracked_dirty warning in {metadata!r}"
    assert any(
        entry.startswith("hidden-by-config.txt:") for entry in dirty["arquivos"]
    ), dirty


def test_worktreeinclude_symlink_is_not_reported_as_dirt(
    kanban_home: Path, repo: Path
) -> None:
    """A ``.worktreeinclude`` symlink is scaffolding, not the operator's work.

    The include symlinks a directory (e.g. ``node_modules``) back to the main
    checkout; a trailing-slash gitignore pattern never matches a symlink, so
    git reports it as untracked. Reporting it would bury the real files.
    """
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (repo / ".worktreeinclude").write_text("linked_dir\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", ".worktreeinclude"],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "include"],
            check=False,
            capture_output=True,
        )
        (repo / "linked_dir").mkdir(exist_ok=True)
        (repo / "linked_dir" / "content.txt").write_text("x\n", encoding="utf-8")
        (wt / "linked_dir").symlink_to(repo / "linked_dir")
        (wt / "real-work.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")

        metadata = _run_metadata(conn, tid)

    dirty = metadata.get("tracked_dirty")
    assert isinstance(dirty, dict), f"no tracked_dirty warning in {metadata!r}"
    assert any(
        entry.startswith("real-work.txt:") for entry in dirty["arquivos"]
    ), dirty
    assert not any(
        entry.startswith("linked_dir") for entry in dirty["arquivos"]
    ), dirty
