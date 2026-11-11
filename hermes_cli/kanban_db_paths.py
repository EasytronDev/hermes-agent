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


def explicit_board_slug(
    board: Optional[str], scoped: Optional[str], normalize=None,
) -> Optional[str]:
    """Explicit caller intent: a direct ``board=`` argument, else the scoped
    ``--board`` context (CLI ``hermes kanban --board``, dashboard plugin_api);
    ``None`` when the caller expressed neither.

    *scoped* is the caller's scoped-board context value and *normalize* the
    caller's slug normalizer, both passed IN rather than read from here: the
    ContextVar lives in the facade, and a sibling that read it through a late
    import would see a DIFFERENT object after a test purges
    ``hermes_cli`` from ``sys.modules`` (the repo's
    ``test_kanban_per_profile_cap.py`` does exactly that — measured: the
    scoped ``--board`` override became invisible and the pinned path won).
    """
    if board is not None:
        return normalize(board)
    # A caller-scoped board (CLI `hermes kanban --board B ...`, dashboard
    # plugin_api) is explicit intent just like a direct board= argument —
    # without this a worker-pinned HERMES_KANBAN_DB silently outranks --board
    # (os-reviewer P1 on PR#107195 / t_11c4afd8).
    ctx = (scoped or "").strip()
    if ctx:
        try:
            return normalize(ctx)
        except ValueError:
            return None
    return None


def explicit_board_intent_is_pinned(pin_first: bool) -> bool:
    """Whether an explicit ``board=`` (or scoped ``--board``) must still resolve
    through the ``HERMES_KANBAN_DB``-style env pins instead of its own board dir.

    True for machine flows wrapped in :func:`pin_first_board_resolution` and
    for every execution the dispatcher fences: its own workers (they carry
    ``HERMES_KANBAN_TASK``) and delegated children / descendants (the
    ``HERMES_DELEGATED_CHILD_CONTEXT`` marker). The pins ARE the "workers
    physically cannot see other boards" isolation (5ec6baa), and
    ``agent.delegation_context.kanban_path_is_fenced`` checks the pinned path /
    fenced root — an explicit board that resolved elsewhere would also escape
    that fence.

    *pin_first* is the caller's machine-flow flag, passed IN for the same
    reason as *scoped* above.
    """
    if pin_first:
        return True
    from agent.delegation_context import explicit_board_intent_is_pinned
    return explicit_board_intent_is_pinned()


def board_path(
    env_var: Optional[str], board: Optional[str], default_parts: tuple[str, ...], leaf: str,
    scoped: Optional[str] = None, pin_first: bool = False,
    current_board_fn=None, default_board: str = "default",
    home: Optional[Path] = None, board_dir_fn=None, normalize=None,
) -> Path:
    """Shared resolver. An explicit ``board=`` argument — or the scoped
    ``--board`` context (:func:`hermes_cli.kanban_db.scoped_current_board`) —
    outranks the ``env_var`` pin ONLY where no fence applies (see
    :func:`explicit_board_intent_is_pinned`): user-facing cross-board intent
    (CLI ``--board``, a model tool's ``board=``) is honored, but machine flows
    that enumerate boards (gateway notifier / watcher / dispatcher ticks) and
    dispatched or delegated workers keep resolving through the pin. Without
    explicit intent the ``env_var`` override pins the file, else legacy
    ``<root>/<default_parts>`` for the ``default`` board, else
    ``board_dir(slug)/leaf``.

    In a TEST context a pin is honored only inside a disposable sandbox
    (:mod:`hermes_cli.kanban_test_guard`).

    EVERY piece of caller state arrives as an argument — the scoped-board
    context, the machine-flow flag, the active and default board slugs, and
    the roots. The sibling must not reach back into the facade for them: a
    test that purges ``hermes_cli`` from ``sys.modules`` (the repo's
    ``test_kanban_per_profile_cap.py`` does) leaves the caller's module and
    the sibling as DIFFERENT objects, and a late import would then read a
    different ContextVar — measured: the notifier seam lost the pinned DB.
    """
    pin = os.environ.get(env_var, "").strip() if env_var else ""
    slug = explicit_board_slug(board, scoped, normalize)
    pin_wins = bool(slug is None or explicit_board_intent_is_pinned(pin_first))
    # The active board is resolved LAZILY: the caller's resolver reads board
    # metadata, which resolves a board path in turn — resolving it eagerly
    # here recursed (measured: RecursionError in
    # ``test_none_board_falls_back_to_kanban_board_env``).
    active = slug or (current_board_fn() if current_board_fn else default_board)
    if pin:
        pinned = kanban_test_guard.resolve_pinned_path(
            pin, active, leaf, env_var or "<pin>", pin_wins)
        if pinned is not None:
            return pinned
    if pin and pin_wins:
        return Path(pin).expanduser()
    if slug is None:
        slug = active
    if slug == default_board:
        path = Path(home).joinpath(*default_parts)
    else:
        path = Path(board_dir_fn(slug)) / leaf
    # No pin won, so the board's OWN directory was resolved. In a test run
    # that directory belongs to the deployment (``HERMES_KANBAN_HOME`` or the
    # platform root), NOT to the test — and the 2026-11-10 incident is
    # exactly a test writing there. Fail closed: a test resolves into the
    # sandbox instead.
    sandboxed = kanban_test_guard.sandbox_path_for(path, slug, leaf)
    if sandboxed is not None:
        return sandboxed
    return path
