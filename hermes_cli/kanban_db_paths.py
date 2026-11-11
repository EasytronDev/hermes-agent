"""Board PATH resolution — the ``board=`` / env-pin priority rules.

Extracted from :mod:`hermes_cli.kanban_db` (a facade that had grown past the
repository's per-file line cap) into this topical sibling. The rules encoded
here are unchanged; only their home moved.

The resolver answers one question: given an env pin (``HERMES_KANBAN_DB`` and
friends), a requested board, and the caller's own intent, which FILE is this
board's state in?

Priority, in order:

1. an explicit ``board=`` argument — or the scoped ``--board`` context — wins
   over the pin, UNLESS a fence applies (a dispatched worker, a delegated
   child, or a machine flow enumerating boards). The pins ARE the "workers
   physically cannot see other boards" isolation, so an explicit board that
   resolved elsewhere would also escape that fence;
2. otherwise the env pin wins;
3. otherwise the board's own directory (legacy ``<root>/<default_parts>`` for
   the ``default`` board, else ``board_dir(slug)/leaf``).

In a TEST context a pin is honored only when it points inside a disposable
sandbox (:mod:`hermes_cli.kanban_test_guard`): a worker-inherited pin at a
live board is refused and the resolution falls back to a per-board sandbox
directory. On 2026-11-10 that refusal is what kept a test fixture from
replacing a production board (694 tasks became 1).

The facade imports this module LAST; this module late-imports the facade
inside its functions, so neither import is a cycle.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_test_guard


def _explicit_board_slug(board: Optional[str]) -> Optional[str]:
    """Explicit caller intent: a direct ``board=`` argument, else the scoped
    ``--board`` context (CLI ``hermes kanban --board``, dashboard plugin_api);
    ``None`` when the caller expressed neither."""
    from hermes_cli import kanban_db as kb

    if board is not None:
        return kb._normalize_board_slug(board)
    # A caller-scoped board (CLI `hermes kanban --board B ...`, dashboard
    # plugin_api) is explicit intent just like a direct board= argument —
    # without this a worker-pinned HERMES_KANBAN_DB silently outranks --board
    # (os-reviewer P1 on PR#107195 / t_11c4afd8).
    ctx = (kb._CURRENT_BOARD_OVERRIDE.get() or "").strip()
    if ctx:
        try:
            return kb._normalize_board_slug(ctx)
        except ValueError:
            return None
    return None


def _explicit_board_intent_pinned() -> bool:
    """Whether an explicit ``board=`` (or scoped ``--board``) must still resolve
    through the ``HERMES_KANBAN_DB``-style env pins instead of its own board dir.

    True for machine flows wrapped in :func:`pin_first_board_resolution` and
    for every execution the dispatcher fences: its own workers (they carry
    ``HERMES_KANBAN_TASK``) and delegated children / descendants (the
    ``HERMES_DELEGATED_CHILD_CONTEXT`` marker). The pins ARE the "workers
    physically cannot see other boards" isolation (5ec6baa), and
    ``agent.delegation_context.kanban_path_is_fenced`` checks the pinned path /
    fenced root — an explicit board that resolved elsewhere would also escape
    that fence."""
    from hermes_cli import kanban_db as kb

    if kb._PIN_FIRST_BOARD_RESOLUTION.get():
        return True
    from agent.delegation_context import explicit_board_intent_is_pinned
    return explicit_board_intent_is_pinned()


def board_path(
    env_var: Optional[str], board: Optional[str], default_parts: tuple[str, ...], leaf: str,
) -> Path:
    """Shared resolver. An explicit ``board=`` argument — or the scoped
    ``--board`` context (:func:`hermes_cli.kanban_db.scoped_current_board`) —
    outranks the ``env_var`` pin ONLY where no fence applies (see
    :func:`_explicit_board_intent_pinned`): user-facing cross-board intent
    (CLI ``--board``, a model tool's ``board=``) is honored, but machine flows
    that enumerate boards (gateway notifier / watcher / dispatcher ticks) and
    dispatched or delegated workers keep resolving through the pin. Without
    explicit intent the ``env_var`` override pins the file, else legacy
    ``<root>/<default_parts>`` for the ``default`` board, else
    ``board_dir(slug)/leaf``.

    In a TEST context a pin is honored only inside a disposable sandbox
    (:mod:`hermes_cli.kanban_test_guard`).
    """
    from hermes_cli import kanban_db as kb

    pin = os.environ.get(env_var, "").strip() if env_var else ""
    slug = _explicit_board_slug(board)
    if pin:
        pinned = kanban_test_guard.resolve_pinned_path(
            pin, slug or kb.get_current_board(), leaf, env_var or "<pin>",
            bool(slug is None or _explicit_board_intent_pinned()))
        if pinned is not None:
            return pinned
    if pin and (slug is None or _explicit_board_intent_pinned()):
        return Path(pin).expanduser()
    if slug is None:
        slug = kb.get_current_board()
    if slug == kb.DEFAULT_BOARD:
        path = kb.kanban_home().joinpath(*default_parts)
    else:
        path = kb.board_dir(slug) / leaf
    # No pin won, so the board's OWN directory was resolved. In a test run
    # that directory belongs to the deployment (``HERMES_KANBAN_HOME`` or the
    # platform root), NOT to the test — and the 2026-11-10 incident is
    # exactly a test writing there. Fail closed: a test resolves into the
    # sandbox instead.
    sandboxed = kanban_test_guard.sandbox_path_for(path, slug, leaf)
    if sandboxed is not None:
        return sandboxed
    return path
