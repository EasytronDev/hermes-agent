"""Board metadata (``board.json``) and board lifecycle management for the Kanban DB:
read/write of per-board display metadata, board creation/discovery/archival, and the
``board.json``-as-identity-marker rule.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from typing import Any
from typing import Optional
from pathlib import Path


def _dir_holds_board(d: Path) -> bool:
    # ``board.json`` is the identity marker: archive/hard-delete both leave the
    # directory without it, and a stale ``connect(board=slug)`` used to leave a
    # ``kanban.db``-only stub that resurfaced in the board list as an empty
    # active board (#43243). Discovery must therefore require the metadata
    # file; a DB-only directory is a stub to ignore, never a board.
    return (d / "board.json").exists()


def board_metadata_path(board: Optional[str] = None) -> Path:
    """``board.json`` path — display metadata only; the directory slug is the identity."""
    return _kb.board_dir(_kb._slug_or_default(board)) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """``atm10-server`` -> ``Atm10 Server``."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


def read_board_metadata(board: Optional[str] = None) -> dict:
    """``board.json`` merged over defaults, plus ``slug`` and ``db_path``. Never
    raises — a missing/malformed file yields the synthesized entry."""
    slug = _kb._slug_or_default(board)
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "default_workdir": None,
        # Project scope: new tasks inherit it (deterministic worktree + branch).
        "project_id": None,
        "created_at": None,
        "archived": False,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                meta.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    meta["db_path"] = str(_kb.kanban_db_path(slug))
    return meta


def write_board_metadata(
    board: Optional[str], *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, archived: Optional[bool] = None,
    default_workdir: Optional[str] = None, project_id: Optional[str] = None,
) -> dict:
    """Create/update ``board.json``; unmentioned fields are preserved, ``created_at``
    set on first write. ``project_id``/``default_workdir``: ``None`` = unchanged,
    "" = clear (``project_id`` is not validated here)."""
    _kb._assert_not_delegated_child_mutation()
    slug = _kb._slug_or_default(board)
    meta = read_board_metadata(slug)
    # db_path is derived on every read; never persist it into board.json.
    meta.pop("db_path", None)
    if name is not None:
        meta["name"] = str(name).strip() or _default_board_display_name(slug)
    for key, value in (("description", description), ("icon", icon), ("color", color)):
        if value is not None:
            meta[key] = str(value)
    if archived is not None:
        meta["archived"] = bool(archived)
    for key, value in (("default_workdir", default_workdir), ("project_id", project_id)):
        if value is not None:
            meta[key] = str(value) if value else None
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    meta["db_path"] = str(_kb.kanban_db_path(slug))
    return meta


def create_board(
    slug: str, *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, default_workdir: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict:
    """Create board dir + DB + metadata (``mkdir -p`` semantics: existing board returns its metadata)."""
    normed = _kb._require_slug(slug)
    # Explicit creation clears any archived tombstone at this slug (left by
    # remove_board(archive=True)) — otherwise _kb.init_db() below would rightly
    # refuse to recreate the archived board's DB (#43243).
    meta = write_board_metadata(
        normed, name=name, description=description, icon=icon, color=color,
        default_workdir=default_workdir, project_id=project_id, archived=False,
    )
    # Touch the DB so list_boards() sees it immediately.
    _kb.init_db(board=normed)
    return meta


def list_boards(*, include_archived: bool = True) -> list[dict]:
    """Metadata for every board: ``default`` first (always present), then
    ``boards/<slug>/`` dirs holding a ``kanban.db`` or ``board.json``, sorted."""
    entries = [read_board_metadata(_kb.DEFAULT_BOARD)]
    seen = {_kb.DEFAULT_BOARD}
    root = _kb.boards_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            try:
                normed = _kb._normalize_board_slug(child.name)  # skip junk dirs, don't raise
            except ValueError:
                continue
            if not normed or normed in seen or not _dir_holds_board(child):
                continue
            meta = read_board_metadata(normed)
            if meta.get("archived") and not include_archived:
                continue
            entries.append(meta)
            seen.add(normed)
    return entries


class LiveBoardTaskError(ValueError):
    """``remove_board`` refused: the board still holds a card in flight — a task
    ``running`` under a live worker claim. Removing the board would destroy
    that card while its worker keeps executing against it. A ``ValueError`` so
    the CLI renders it as a clean operator message (and exits non-zero) instead
    of a traceback, the same way ``kanban_db.LiveClaimError`` fences
    ``complete_task``."""

    def __init__(self, slug: str, task_ids: list[str], action: str, remedy: str):
        self.slug = slug
        self.task_ids = list(task_ids)
        self.action = action
        super().__init__(
            f"board {slug!r} has {len(task_ids)} task(s) running "
            f"({', '.join(task_ids)}) — refusing to {action}. {remedy}"
        )


def _board_live_task_ids(db_path: Path) -> list[str]:
    """Task ids on the board that a live worker is still running.

    A board whose card is in flight must not be destroyed: the dispatcher has
    already spawned a worker against it, and the worker keeps executing against
    a card that no longer exists in any database. Measured incidents
    (``t_ec405bea``, ``t_65f193a1``, ``t_d74a3b5e``, ``t_f180ff45`` across
    ``e2e-ciclo-test`` and ``e2e-fixa2-test``) destroyed in-flight cards this
    way.

    Liveness mirrors ``kanban_db._claim_is_live`` — a ``running`` task whose
    ``worker_pid`` still resolves to the fingerprinted process. A claim whose
    worker is gone protects no run and does NOT block the removal; otherwise a
    crashed worker would wedge its board forever.

    Both the task row and its current run row are consulted:

    * ``tasks.status = 'running'`` with a live worker — the normal in-flight card.
    * the task's CURRENT run (``tasks.current_run_id``) still ``running`` with a
      live worker — the case where the task row was archived (or flipped)
      while its run stayed open. The worker is still executing, so the board
      still holds live work.

    A run row that is no longer the task's ``current_run_id`` was superseded
    (reclaim / re-dispatch): no worker is executing it, so it must NOT block
    the removal.

    Read strictly read-only and defensively: the board's DB may be missing,
    empty, mid-WAL or locked. A board we cannot inspect is NOT assumed safe to
    destroy — an unreadable DB yields an empty list only when the file truly
    does not exist; any other failure raises, so the caller refuses instead of
    destroying a board it could not verify.
    """
    try:
        if not db_path.exists() or db_path.stat().st_size == 0:
            return []
    except OSError:
        return []

    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        # Could not open read-only (locked / corrupt / permissions). Refuse.
        raise LiveBoardTaskError(
            slug=db_path.parent.name,
            task_ids=[],
            action="inspect",
            remedy=(
                f"the board database could not be opened read-only ({exc}); "
                "refusing to remove a board whose live tasks could not be verified."
            ),
        )
    try:
        conn.row_factory = sqlite3.Row
        live: dict[str, str] = {}

        def _collect(sql: str, id_col: str) -> None:
            try:
                rows = conn.execute(sql).fetchall()
            except sqlite3.DatabaseError:
                # Table/column missing (legacy board) — that source has no
                # live work to protect.
                return
            for row in rows:
                task_id = row[id_col]
                if not task_id:
                    continue
                pid = row["worker_pid"]
                started_at = row["worker_started_at"]
                if not pid:
                    continue
                try:
                    alive = _kb._worker_alive(pid, started_at)
                except Exception:
                    alive = False
                if alive:
                    live.setdefault(task_id, str(pid))

        # The task row: the normal in-flight card (``running`` + live worker).
        _collect(
            "SELECT id AS task_id, worker_pid, worker_started_at FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL",
            "task_id",
        )
        # The run row, but ONLY the run the task still points at. A run left
        # ``running`` that is no longer the task's ``current_run_id`` was
        # superseded (reclaim / re-dispatch): no worker is executing it, and
        # honouring it would wedge the board forever behind a dead run.
        _collect(
            "SELECT r.task_id AS task_id, r.worker_pid, r.worker_started_at "
            "FROM task_runs r JOIN tasks t ON t.current_run_id = r.id "
            "WHERE r.status = 'running' AND r.worker_pid IS NOT NULL",
            "task_id",
        )
        return sorted(live)
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(
            f"cannot inspect board database {db_path}: refusing to remove a "
            f"board whose live tasks could not be verified ({exc})"
        )
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def remove_board(slug: str, *, archive: bool = True) -> dict:
    """Archive (to ``boards/_archived/<slug>-<ts>/``) or delete a board;
    ``default`` cannot be removed. Returns ``{"slug", "action", "new_path"}``.

    A board with a card in flight — a task still ``running`` under a live
    worker claim — is REFUSED, for both archive and hard delete. Removing it
    would destroy the card the dispatcher just spawned a worker for, while that
    worker keeps running against a task that no longer exists. The refusal
    names the offending task ids so the operator can wait for them to finish
    (or archive the board afterwards).
    """
    _kb._assert_not_delegated_child_mutation()
    normed = _kb._require_slug(slug)
    if normed == _kb.DEFAULT_BOARD:
        raise ValueError("the 'default' board cannot be removed")
    d = _kb.board_dir(normed)
    if not d.exists():
        raise ValueError(f"board {normed!r} does not exist")

    # Refuse BEFORE any destructive step: archiving also takes the whole board
    # (and its DB) out of the dispatcher's reach, so it needs the same fence.
    live_ids = _board_live_task_ids(d / "kanban.db")
    if live_ids:
        action = "archive" if archive else "delete"
        # Message text matches the reference guard (keyhc-e2e/fake_kanban.py):
        # same semantics, same wording. The archive path cannot advise
        # archiving (that IS the operation being refused), so it points at
        # "try again" instead.
        remedy = (
            "Wait for them to reach a terminal state and try again."
            if archive
            else "Wait for them to reach a terminal state or archive the board instead."
        )
        raise LiveBoardTaskError(normed, live_ids, action, remedy)

    # If the user removed the currently-active board, revert to default.
    if _kb.get_current_board() == normed:
        _kb.clear_current_board()

    # A concurrent connect() after the rename recreates an empty DB file; drop
    # the init cache first so the schema pass re-runs on it.
    _kb._INITIALIZED_PATHS.discard(str((d / "kanban.db").resolve()))

    if archive:
        # Capture display metadata before the move so the tombstone below keeps
        # the user's board name instead of falling back to a title-cased slug.
        prior_meta = read_board_metadata(normed)
        archive_root = _kb.boards_root() / "_archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        target = archive_root / f"{normed}-{ts}"
        suffix = 1
        while target.exists():  # rapid double-archive
            target = archive_root / f"{normed}-{ts}-{suffix}"
            suffix += 1
        d.rename(target)
        # Leave an ``archived`` tombstone at the original slug. Stale dashboard
        # tabs / gateway pollers can keep calling connect(board=slug) after the
        # archive; without a marker the resurrect-guard cannot tell an archived
        # slug from a brand-new one and an empty board would reappear (#43243).
        write_board_metadata(
            normed,
            name=prior_meta.get("name"),
            description=prior_meta.get("description"),
            icon=prior_meta.get("icon"),
            color=prior_meta.get("color"),
            default_workdir=prior_meta.get("default_workdir"),
            project_id=prior_meta.get("project_id"),
            archived=True,
        )
        return {"slug": normed, "action": "archived", "new_path": str(target)}
    import shutil
    shutil.rmtree(d)
    return {"slug": normed, "action": "deleted", "new_path": ""}


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
