"""Test-context guard for the Kanban path pins (``HERMES_KANBAN_DB`` and friends).

Field evidence (2026-11-10, board ``strategy-processor-sprint``): the runtime
test suite ran inside a dispatched worker. The worker inherits
``HERMES_KANBAN_DB=<production board>``; pytest inherits the worker's
environment; a fixture that creates a card therefore wrote to the PRODUCTION
board, and the board went from 694 tasks to a single freshly-created row.

Two independent layers close that, and this module is the second one (the
kernel layer). The first is the test suite's own isolation (``tests/conftest.py``
strips the pins at import time and per test). The kernel layer is the one that
matters: a test suite that forgets to strip a pin still must not be able to
write into a board it does not own.

The rule: **in a test context, a path pin is only honored when it points inside
a disposable sandbox.** A pin at a production board path is refused, and the
resolution falls back to a PER-PROCESS SANDBOX DIRECTORY
(``<temp>/hermes-kanban-test-sandbox/pid-<pid>/``) — deliberately NOT to the
board the kernel would have chosen on its own, because in a worker
environment that board IS the live deployment (``HERMES_KANBAN_BOARD`` plus
the platform default resolve straight into production). Refusing — rather
than raising — keeps every legitimate use working: the pin is a convenience
override, not a contract, so a test that pins into ``tmp_path`` keeps its
behavior and a test that inherited a production pin gets the sandbox instead.

The sandbox set is deliberately narrow, because a generous one re-opens the
hole this module closes. It is: pytest's own basetemp (published by the
hermetic conftest as ``HERMES_TEST_PYTEST_BASETEMP``, or
``PYTEST_DEBUG_TEMPOROOT``), pytest's ``pytest-of-<user>`` tree, the
hermetic isolation home, and this module's own sandbox directory.
``TMPDIR`` itself is NOT a sandbox root: on a deployment whose ``TMPDIR``
happens to be the same tree the board lives in, claiming it would make every
production path look like a test path. Anything outside those roots is
treated as a production path — and an unknown path is refused rather than
trusted, because "unknown" is how a production pin looks when the deployment
layout is unusual.

THE SANDBOX IS PER BOARD. A refused pin falls back to
``<sandbox>/<slug>/<leaf>``, not to ``<sandbox>/<leaf>``: two boards that
collapse onto one file would let a write intended for board A land in board
B, and would break the repo's own invariant that "workers physically cannot
see other boards" (a CLI ``--board B`` write must not appear in the pinned
board's database).
"""

from __future__ import annotations

import os
import sys
import tempfile
import logging
from pathlib import Path
from typing import MutableMapping, Optional

_log = logging.getLogger(__name__)

#: pytest's own temp roots. ``PYTEST_DEBUG_TEMPOROOT`` is the explicit
#: override; the ``pytest-of-<user>`` tree is where pytest's ``tmp_path``
#: lands when no override is set.
_PYTEST_TMP_ROOT_VARS = ("PYTEST_DEBUG_TEMPOROOT",)

#: Marker exported by the hermetic conftest: unlike pytest's own
#: ``PYTEST_*`` vars (which tests routinely scrub when they rebuild a child's
#: environment), this one is ours and inherits by default into subprocesses.
_TEST_ISOLATION_MARKER_ENV = "HERMES_TEST_ISOLATION"

#: The isolation marker a hermetic test run points at: the per-test sandbox
#: home. A pin under it is a test-owned path.
_TEST_ISOLATION_HOME_ENV = "HERMES_TEST_SANDBOX_HOME"

#: Published by the hermetic conftest: pytest's basetemp, which is where
#: pytest's ``tmp_path`` lands. This is the AUTHORITATIVE answer to "where
#: is the test sandbox?" — guessing from TMPDIR is unreliable because the
#: layout varies per host (this one redirects TMPDIR into the profile
#: scratch and relocates the basetemp out of the operator's home into a
#: per-run directory).
_PYTEST_BASETEMP_ENV = "HERMES_TEST_PYTEST_BASETEMP"

#: Published by the hermetic conftest: the board roots of the DEPLOYMENT, as
#: they were BEFORE the test sandbox rewired ``HERMES_HOME``. This is what
#: makes "is this pin a live board?" answerable inside a test run, where
#: every env-derived root is the sandbox itself.
_DEPLOYMENT_ROOTS_ENV = "HERMES_TEST_DEPLOYMENT_KANBAN_ROOTS"


def running_under_pytest() -> bool:
    """True when this process is running inside a test suite.

    Three independent signals, each of which survives a different way of
    losing the others:

    * ``PYTEST_CURRENT_TEST`` — set by pytest while a test runs. It is NOT set
      during collection, so it alone misses the import-time window.
    * ``HERMES_TEST_ISOLATION`` — ours; exported by the hermetic conftest and
      inherited by children by default.
    * ``pytest`` in ``sys.modules`` — covers the collection window (pytest
      itself is imported) and any harness that imports pytest without being
      pytest.

    A child process spawned with a rebuilt environment loses ``PYTEST_*`` but
    keeps our marker, so the union is strictly wider than any single signal.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if os.environ.get(_TEST_ISOLATION_MARKER_ENV):
        return True
    return "pytest" in sys.modules


def _candidate_sandbox_roots() -> list[Path]:
    """Directories a test may pin into. Never includes a live board root."""
    roots: list[Path] = []

    def add(value: Optional[str]) -> None:
        if not value:
            return
        try:
            roots.append(Path(value).expanduser())
        except (OSError, ValueError):
            return

    for var in _PYTEST_TMP_ROOT_VARS:
        add(os.environ.get(var, "").strip())
    add(os.environ.get(_TEST_ISOLATION_HOME_ENV, "").strip())
    # The authoritative sandbox root: pytest's own basetemp.
    add(os.environ.get(_PYTEST_BASETEMP_ENV, "").strip())

    # The OS temp root is NOT a sandbox root by itself, and neither is
    # TMPDIR: on a deployment whose TMPDIR happens to be the same tree the
    # board lives in, claiming it would re-open the hole this module closes.
    # What IS claimed are the test-specific trees inside it:
    #
    #   * pytest's per-user tree: <tmp>/pytest-of-<user>/pytest-<n>/<test>
    #   * the guard's own sandbox: <tmp>/hermes-kanban-test-sandbox/
    try:
        tmp_root = Path(tempfile.gettempdir())
    except (OSError, ValueError):
        tmp_root = None
    if tmp_root is not None:
        try:
            children = list(tmp_root.glob("pytest-of-*"))
        except OSError:
            children = []
        roots.extend(children)
        roots.append(tmp_root / "hermes-kanban-test-sandbox")

    # The hermetic per-test sandbox home (a tmp_path under the basetemp).
    isolation = os.environ.get(_TEST_ISOLATION_MARKER_ENV, "").strip()
    if isolation and isolation != "1":
        add(isolation)

    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except (OSError, ValueError):
            continue
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def pin_points_into_sandbox(pin: str) -> bool:
    """True when *pin* names a path inside a disposable test sandbox.

    A pin at a file (not a directory) is inside the sandbox when its PARENT
    is — a test pins ``<tmp>/kanban.db``, not ``<tmp>`` itself.
    """
    if not pin:
        return False
    try:
        target = Path(pin).expanduser().resolve()
    except (OSError, ValueError):
        return False
    for root in _candidate_sandbox_roots():
        if target == root or _is_relative_to(target, root):
            return True
        # The pin may name the file directly; the sandbox owns its parent.
        if _is_relative_to(target.parent, root):
            return True
    return False


def _deployment_board_roots() -> list[Path]:
    """The board roots of THIS deployment, independent of the test sandbox.

    The naive oracle — ``kanban_db.kanban_home()`` — cannot be used here: in a
    hermetic test run ``HERMES_HOME`` points at the per-test sandbox, so
    ``kanban_home()`` IS the sandbox. Comparing a pin against it then says
    "this is a live board" for every legitimate test pin (measured: the repo's
    own ``test_kanban_board_path_priority.py`` pins a board inside its
    ``tmp_path`` and the naive oracle called it live), and the kernel refuses
    a pin it should honor.

    The deployment's roots are therefore taken from the environment as it was
    BEFORE the hermetic sandbox rewired it. The conftest captures them at
    import time and publishes them in ``HERMES_TEST_DEPLOYMENT_KANBAN_ROOTS``
    (a ``os.pathsep``-separated list); the guard falls back to
    ``HERMES_KANBAN_HOME`` — the one board root the deployment sets
    explicitly — and, failing that, to the platform-native Hermes root.

    An empty result means "the deployment did not say", and the caller then
    fails closed: an unknown pin is refused.
    """
    roots: list[Path] = []

    def add(value: Optional[str]) -> None:
        if not value:
            return
        try:
            roots.append(Path(value).expanduser().resolve())
        except (OSError, ValueError):
            return

    published = os.environ.get(_DEPLOYMENT_ROOTS_ENV, "").strip()
    if published:
        for part in published.split(os.pathsep):
            add(part)
    add(os.environ.get("HERMES_KANBAN_HOME", "").strip())
    return roots


def pin_points_at_a_live_board(pin: str) -> bool:
    """True when *pin* names a board of THIS deployment.

    This is the decisive check, and it is not a heuristic about temp dirs: the
    kernel knows where the deployment keeps its boards. A pin at any of

      * ``<kanban home>/kanban.db``            (the default board)
      * ``<kanban home>/kanban/boards/<slug>/``  (a named board)

    is a LIVE board pointer. A test run must never write there, whatever the
    temp-dir layout happens to be. On the 2026-11-10 incident the board lived
    at ``/opt/data/kanban/boards/strategy-processor-sprint/kanban.db`` — a
    deployment root that is NOT under ``~/.hermes`` and NOT under the temp
    tree, which is exactly why a path-shape heuristic alone missed it.

    NOTE: the comparison is against the DEPLOYMENT's board roots, not against
    the ``HERMES_KANBAN_DB`` the caller passed in. The caller IS the thing
    under test — it sets that variable to whatever it wants to pin, so
    comparing against it would make every pin look live and refuse the
    legitimate sandbox pins too.
    """
    if not pin:
        return False
    try:
        target = Path(pin).expanduser().resolve()
    except (OSError, ValueError):
        return False

    for home in _deployment_board_roots():
        live_roots = (
            home,                                  # <home>/kanban.db and <home>/kanban/boards/...
            home / "kanban",
            home / "kanban" / "boards",
        )
        for root in live_roots:
            try:
                resolved_root = root.resolve()
            except (OSError, ValueError):
                continue
            if target == resolved_root or _is_relative_to(target, resolved_root):
                return True
    return False


def sandbox_board_root() -> Path:
    """The directory a test run may write Kanban state into.

    This is where a refused pin falls back to. It is NOT the kernel's own
    board root: in a worker environment that root is the LIVE deployment
    (``HERMES_KANBAN_BOARD`` + the platform default resolve straight into the
    production board), so refusing the pin without redirecting the fallback
    would still land the test on the live board.

    The root is per-process and stable for the life of the process, so every
    board/workspace/attachment path a test resolves lands in the same
    disposable directory.
    """
    global _SANDBOX_BOARD_ROOT
    if _SANDBOX_BOARD_ROOT is not None:
        return _SANDBOX_BOARD_ROOT
    base = os.environ.get("TMPDIR", "").strip() or tempfile.gettempdir()
    try:
        root = Path(base) / "hermes-kanban-test-sandbox" / f"pid-{os.getpid()}"
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        root = Path(tempfile.mkdtemp(prefix="hermes-kanban-test-sandbox-"))
    _SANDBOX_BOARD_ROOT = root
    return root


_SANDBOX_BOARD_ROOT: Optional[Path] = None


def reset_sandbox_board_root() -> None:
    """Forget the cached sandbox root (tests that fork or re-point TMPDIR)."""
    global _SANDBOX_BOARD_ROOT
    _SANDBOX_BOARD_ROOT = None


def sandbox_board_path(slug: str, leaf: str, pin_var: str = "<pin>") -> Path:
    """The sandboxed path for board *slug* — ``<sandbox>/<slug>/<leaf>``.

    PER BOARD, not per process: a single file per process would collapse every
    board onto one database, and the repo's own invariant — "workers
    physically cannot see other boards" — depends on two boards resolving to
    two different files. The repo's ``test_cli_board_flag_trumps_worker_env_pin_end_to_end``
    fails outright if they collapse: a CLI ``--board B`` write would show up
    in the worker's own pinned board.
    """
    safe_slug = _safe_slug(slug)
    path = sandbox_board_root() / safe_slug / leaf
    _log.warning(
        "kanban path pin %s refused: this process is running under a test suite "
        "and the pin does not point inside a disposable sandbox. Falling back to "
        "the per-process, per-board test sandbox (%s) so the test cannot write "
        "into a live board.",
        pin_var,
        path,
    )
    return path


def _safe_slug(slug: str) -> str:
    """A slug that is safe to use as a directory name.

    The slug normally comes from the kernel's own normalization, but the
    sandbox path must never be able to escape the sandbox root, so anything
    that is not a plain slug token is replaced.
    """
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in (slug or "").strip())
    return cleaned or "default"


#: The path pins that must never survive into a test run.
KEEP_OUT_PATH_PINS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
    "HERMES_KANBAN_WORKSPACES_ROOT",
)


def strip_path_pins(env: Optional[MutableMapping[str, str]] = None) -> dict[str, str]:
    """Remove the kanban path pins from *env* (default: ``os.environ``).

    Returns the pins that were removed, so a caller can build a deny-list out
    of them: the removed pin is, by definition, the path the environment was
    pointing at — which on a dispatched worker is the LIVE board.

    This is the test suite's first layer of defense, and it is a FUNCTION (not
    a statement in the conftest) so it can be tested directly.
    """
    target = os.environ if env is None else env
    removed: dict[str, str] = {}
    for var in KEEP_OUT_PATH_PINS:
        value = target.pop(var, None)
        if value:
            removed[var] = value
    return removed


def pinned_path_is_refused(pin: str) -> bool:
    """True when *pin* must NOT be honored because this is a test run and the
    pin points at a board the test does not own.

    In a test context a pin is honored only when it resolves inside a
    disposable sandbox; a pin at a LIVE board of this deployment is refused
    outright, whatever the temp-dir layout is, and so is a pin that names
    neither a sandbox nor a live board — an unknown path is refused rather
    than trusted, because "unknown" is how a production pin looks when the
    deployment layout is unusual.
    """
    if not pin or not running_under_pytest():
        return False
    if pin_points_at_a_live_board(pin):
        return True
    return not pin_points_into_sandbox(pin)


def sandbox_path_for(path: Path, slug: str, leaf: str) -> Optional[Path]:
    """The sandboxed twin of *path*, or ``None`` when *path* is fine.

    The other half of the fail-closed rule: a pin is not the only way into a
    live board. With NO pin set, the board's own directory resolves under
    the deployment's root (``HERMES_KANBAN_HOME`` or the platform root) —
    and inside a test run that root is the DEPLOYMENT's, not the test's.
    Measured on a fake deployment root: every resolver (``kanban_db_path``,
    ``workspaces_root``, ``attachments_root``, ``worker_logs_dir``) landed
    inside the deployment tree with no pin at all, which is the same class
    of damage as the 2026-11-10 wipe (a test writing where it must not).

    So in a test context a resolved board path that is NOT already inside a
    disposable sandbox is redirected into the per-board sandbox. A path that
    already is inside a sandbox is returned as-is (``None`` means "keep it").
    """
    if not running_under_pytest():
        return None
    if pin_points_into_sandbox(str(path)):
        return None
    return sandbox_board_path(slug, leaf, pin_var="<board path>")


def resolve_pinned_path(
    pin: str, slug: str, leaf: str, pin_var: str = "<pin>",
    pin_wins: bool = True,
) -> Optional[Path]:
    """Resolve *pin* for a test run: the path the caller must use.

    This is the kernel's fail-closed rule, in one place. In a test context a
    pin is honored ONLY when it resolves inside a disposable sandbox. A pin at
    a LIVE board of this deployment is refused outright, whatever the
    temp-dir layout is, and so is a pin that names neither a sandbox nor a
    live board — an unknown path is refused rather than trusted, because
    "unknown" is how a production pin looks when the deployment layout is
    unusual.

    Returns the SANDBOXED path when the pin is refused, and the pin itself
    when it may be honored. Returns ``None`` when there is no pin, or when
    this is not a test run at all and the caller's own ``pin_wins`` rule does
    not select the pin — the caller then resolves its board normally.

    *slug* is the board the caller asked for (or the active one), *pin_wins*
    whether the caller's own rules select the pin over that board.
    """
    if not pin:
        return None
    if not running_under_pytest():
        return Path(pin).expanduser() if pin_wins else None
    if pinned_path_is_refused(pin):
        return sandbox_board_path(slug, leaf, pin_var=pin_var)
    return Path(pin).expanduser() if pin_wins else None
