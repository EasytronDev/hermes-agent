"""The ``tracked_dirty`` completion warning: the dirt a task left uncommitted.

A task whose workspace is a git checkout with uncommitted work completes with
a VISIBLE warning (``metadata['tracked_dirty']``) instead of a hard block: the
operator sees it via ``kanban show`` and asks before merging. The key is
written ONLY when there is real dirt — a clean checkout gets no key at all.

Everything here is fail-open: git missing, a vanished repo, refused filter
discovery or a timeout yields ``None`` (no key, no exception), so a completion
can never break because of the check.

This lives in its own module (not ``kanban_db.py``, which is already past the
repo's file-size cap): new behaviour never goes into a facade.
"""
from __future__ import annotations

import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Workspace kinds whose ``workspace_path`` may point at a git checkout whose
# dirt the operator must see at completion time. ``scratch`` is excluded: it is
# not a repo and already has its own artifact-preservation gate.
_TRACKED_DIRTY_KINDS = ("worktree", "dir")

# Cap on the porcelain lines we parse and report. A worktree with thousands of
# untracked files must not blow up the run metadata (and the DB row).
_TRACKED_DIRTY_MAX_FILES = 200

_TRACKED_DIRTY_TIMEOUT = 10

_WORKSPACE_ROW_SQL = "SELECT workspace_kind, workspace_path, branch_name FROM tasks WHERE id = ?"


# Workspace kinds whose ``workspace_path`` may point at a git checkout whose
# dirt the operator must see at completion time. ``scratch`` is excluded: it is
# not a repo and already has its own artifact-preservation gate.
_TRACKED_DIRTY_KINDS = ("worktree", "dir")

# Cap on the porcelain lines we parse and report. A worktree with thousands of
# untracked files must not blow up the run metadata (and the DB row).
_TRACKED_DIRTY_MAX_FILES = 200

_TRACKED_DIRTY_TIMEOUT = 10


def _tracked_dirty_files(workspace: Path, repo_root: Path) -> Optional[list[str]]:
    """``<relpath>:<git status>`` entries for the dirty files in *workspace*.

    Returns ``None`` when the state is UNKNOWN (git missing, repo gone,
    discovery refused, timeout) so the caller can skip the warning entirely —
    a completion must never fail because the check could not run.
    """
    from hermes_cli._subprocess_compat import noninteractive_repo_git_env
    from hermes_cli.worktree_ops import _include_symlink_paths

    try:
        env = noninteractive_repo_git_env(workspace)
        if env is None:
            return None
        result = subprocess.run(
            [
                "git", "-C", str(workspace),
                # NUL-separated: a path with a newline in it is quoted in the
                # line-based porcelain v1 output, so splitting on lines would
                # report the QUOTED, escaped name instead of the real path.
                "-c", "status.showUntrackedFiles=no",
                "status", "--porcelain", "-z", "--untracked-files=normal",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TRACKED_DIRTY_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    # ``.worktreeinclude`` directory symlinks point back to the main checkout.
    # A trailing-slash gitignore pattern never matches a symlink, so git reports
    # each one as untracked — they are our own scaffolding, not the operator's
    # work, and reporting them would bury the real files under ``node_modules``.
    include_links = _include_symlink_paths(workspace, repo_root)

    entries: list[str] = []
    for record in result.stdout.split("\0"):
        if not record:
            continue
        # Porcelain v1: "XY path" (X=index, Y=worktree). Renames print as
        # "R  old -> new"; keep the destination so the warning names a real path.
        code, _, path = record[:2], record[2:3], record[3:].strip()
        if not path:
            continue
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1].strip()
        if path.rstrip("/") in include_links:
            continue
        entries.append(f"{path}:{code.strip() or '?'}")
        if len(entries) >= _TRACKED_DIRTY_MAX_FILES:
            break

    # ``git status`` trusts skip-worktree / assume-unchanged flags and reports
    # nothing for them; those edits are still real work.
    try:
        from hermes_cli.worktree_retention import hidden_index_edits

        for rel in hidden_index_edits(workspace):
            entry = f"{rel}:S"
            if entry not in entries:
                entries.append(entry)
                if len(entries) >= _TRACKED_DIRTY_MAX_FILES:
                    break
    except Exception:
        # Unknown hidden-edit state: the porcelain list above is still valid.
        logger.exception("hidden_index_edits unavailable for %s", workspace)

    return entries


def _tracked_dirty_for_workspace(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[dict]:
    """The ``tracked_dirty`` warning payload for *task_id*, or ``None``.

    A task whose ``workspace_path`` is a git checkout with uncommitted work
    completes with a visible warning (``metadata['tracked_dirty']``) instead of
    a hard block: the operator sees it via ``kanban show`` and asks before
    merging. The key is written ONLY when there is real dirt — a clean
    checkout gets no key at all.

    Everything here is fail-open: git missing, a vanished repo, refused filter
    discovery or a timeout yields ``None`` (no key, no exception), so a
    completion can never break because of the check.
    """
    try:
        row = conn.execute(_WORKSPACE_ROW_SQL, (task_id,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    kind = row["workspace_kind"]
    raw_path = row["workspace_path"]
    if kind not in _TRACKED_DIRTY_KINDS or not raw_path:
        return None
    try:
        workspace = Path(raw_path).expanduser()
    except (OSError, RuntimeError):
        return None
    if not workspace.is_dir():
        return None

    # The MAIN repo root: for a linked worktree the ``--show-toplevel`` is the
    # checkout itself, but ``.worktreeinclude`` lives in the main checkout (the
    # parent of the common git dir), and its symlinks point there.
    repo_root = workspace
    try:
        probe = subprocess.run(
            [
                "git", "-C", str(workspace), "rev-parse",
                "--path-format=absolute", "--show-toplevel", "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TRACKED_DIRTY_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if probe.returncode != 0:
            return None
        lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        top = lines[0]
        common_dir = Path(lines[1])
        if not top:
            return None
        repo_root = Path(top)
        if common_dir.name == ".git":
            main_root = common_dir.parent
            if (main_root / ".worktreeinclude").is_file():
                repo_root = main_root
    except (OSError, subprocess.SubprocessError):
        return None

    files = _tracked_dirty_files(workspace, repo_root)
    if not files:
        return None
    return {
        "worktree": str(workspace),
        "arquivos": files,
    }



def attach_tracked_dirty_warning(
    conn: sqlite3.Connection, task_id: str, metadata: Optional[dict],
) -> Optional[dict]:
    """Add ``metadata['tracked_dirty']`` when the task's workspace has dirt.

    A completion is never blocked by uncommitted work; instead the closing run
    carries a visible warning so ``kanban show`` points the operator at the
    files before a merge. The key is written ONLY when there is real dirt — a
    clean workspace gets no key at all.

    Fail-open: the check never raises and never changes the metadata the
    caller passed in (a missing/None metadata stays None).
    """
    try:
        dirty = _tracked_dirty_for_workspace(conn, task_id)
    except Exception:
        logger.exception("the tracked_dirty check failed for task %s", task_id)
        return metadata
    if not dirty:
        return metadata
    if not isinstance(metadata, dict):
        metadata = {}
    else:
        metadata = dict(metadata)
    metadata.setdefault("tracked_dirty", dirty)
    return metadata


def merge_completion_prose_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: Optional[dict], *, summary: Optional[str],
    result: Optional[str],
) -> Optional[dict]:
    """Legacy workers named deliverables only by absolute path in prose; add
    those that exist under the scratch workspace to ``metadata["artifacts"]``
    before cleanup can erase them."""
    import re
    from pathlib import Path

    from hermes_cli.kanban_db_workspace import _is_managed_scratch_path
    from hermes_cli.kanban_db_workspace import _scratch_workspace

    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return metadata
    if not _is_managed_scratch_path(workspace):
        return metadata
    text = "\n".join(part for part in (summary, result) if part)
    if not text:
        return metadata
    prefix = re.escape(str(workspace))
    discovered: list[str] = []
    for match in re.finditer(prefix + r"(?:[/\\][^\s`\"'<>]+)", text):
        raw = match.group(0).rstrip(".,;:!?)]}")
        candidate = Path(raw)
        if candidate.is_file():
            discovered.append(str(candidate))
    if not discovered:
        return metadata
    updated = dict(metadata) if isinstance(metadata, dict) else {}
    existing = updated.get("artifacts")
    merged = list(existing) if isinstance(existing, (list, tuple)) else []
    seen = {str(path) for path in merged}
    for path in discovered:
        if path not in seen:
            merged.append(path)
            seen.add(path)
    updated["artifacts"] = merged
    return updated
