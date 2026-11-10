"""Rate-limit cooldown: the config key and the consecutive-run backoff.

Context (measured on a live board, 2026-11-04/05): a provider quota wall that
states NO reset window (MiniMax's "Token Plan usage limit reached" carries no
``reset_at`` / ``resets_in`` / ``retry_after``) left the dispatcher probing the
same wall every 300 s for the whole wall — 576 runs across 36 cards, 10.84
worker-hours burned, and one card retried 35 times over 586 minutes.

Two things are covered here:

* ``kanban.rate_limit_cooldown_seconds`` — the cooldown was env-only, so a
  ``config.yaml`` operator had no way to set it without a container-level env
  var. The config key now wins over the env var.
* the exponential backoff on CONSECUTIVE rate-limited runs of the same card —
  the flat cooldown is the only spacing available when the provider states no
  reset moment, so the wait now doubles per consecutive quota death up to a
  cap.
"""

import json
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb
import hermes_cli.kanban_db_connect as kbc
import hermes_cli.kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (same fixture the other
    kanban suites define locally)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _seed_rate_limited(conn, tid, *, ended_at, error="pid 1 exited rate-limited (quota wall)"):
    """Book a finished ``rate_limited`` run for ``tid`` and free the card."""
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, error) "
        "VALUES (?, 'a', 'rate_limited', 'rate_limited', ?, ?, ?)",
        (tid, ended_at - 60, ended_at, error),
    )
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
        (error, tid),
    )
    conn.commit()


def _write_config(home: Path, kanban_section: str) -> None:
    (home / "config.yaml").write_text(f"kanban:\n{kanban_section}", encoding="utf-8")


def test_config_key_overrides_env_var(kanban_home, monkeypatch):
    """``kanban.rate_limit_cooldown_seconds`` wins over the env var."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    _write_config(kanban_home, "  rate_limit_cooldown_seconds: 900\n")

    assert kb._resolve_rate_limit_cooldown_seconds() == 900


def test_env_var_still_honoured_without_config_key(kanban_home, monkeypatch):
    """No config key -> the env var decides (unchanged behaviour)."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "600")
    assert kb._resolve_rate_limit_cooldown_seconds() == 600


def test_backoff_doubles_on_consecutive_rate_limited_runs(kanban_home, monkeypatch):
    """Consecutive quota deaths stretch the wait: base, 2x, 4x, ... up to the cap."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_BACKOFF_CAP_SECONDS", "3600")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-backoff", assignee="a")

        # One rate-limited run -> the base cooldown, unchanged.
        _seed_rate_limited(conn, tid, ended_at=now)
        assert kbd._effective_rate_limit_cooldown(conn, tid, 300) == 300

        # Two consecutive -> doubled.
        _seed_rate_limited(conn, tid, ended_at=now - 300)
        assert kbd._effective_rate_limit_cooldown(conn, tid, 300) == 600

        # Three consecutive -> quadrupled.
        _seed_rate_limited(conn, tid, ended_at=now - 600)
        assert kbd._effective_rate_limit_cooldown(conn, tid, 300) == 1200

        # Saturated at the cap.
        for i in range(1, 8):
            _seed_rate_limited(conn, tid, ended_at=now - 600 * i)
        assert kbd._effective_rate_limit_cooldown(conn, tid, 300) == 3600


def test_backoff_resets_after_a_non_rate_limited_run(kanban_home, monkeypatch):
    """A completion or crash supersedes the quota streak: the next quota death
    starts a fresh backoff at the base cooldown."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-reset", assignee="a")
        _seed_rate_limited(conn, tid, ended_at=now - 600)
        _seed_rate_limited(conn, tid, ended_at=now - 300)
        assert kbd._effective_rate_limit_cooldown(conn, tid, 300) == 600

        # A completed run supersedes the streak.
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'a', 'completed', 'completed', ?, ?)",
            (tid, now - 100, now - 50),
        )
        conn.commit()
        assert kbd._effective_rate_limit_cooldown(conn, tid, 300) == 300


def test_backoff_cap_zero_disables_the_backoff(kanban_home, monkeypatch):
    """``rate_limit_backoff_cap_seconds = 0`` -> every retry waits the base."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_BACKOFF_CAP_SECONDS", "0")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-nobackoff", assignee="a")
        _seed_rate_limited(conn, tid, ended_at=now - 600)
        _seed_rate_limited(conn, tid, ended_at=now - 300)
        assert kbd._effective_rate_limit_cooldown(conn, tid, 300) == 300


def test_guard_holds_the_card_for_the_stretched_window(kanban_home, monkeypatch):
    """The guard actually defers for the stretched window, and releases after it."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_BACKOFF_CAP_SECONDS", "3600")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-guard-backoff", assignee="a")
        # Two consecutive quota deaths -> the effective cooldown is 600 s.
        _seed_rate_limited(conn, tid, ended_at=now - 600)
        _seed_rate_limited(conn, tid, ended_at=now - 300)

        # 400 s after the last death: inside the base cooldown (300 s) but also
        # inside the stretched one (600 s) -> held.
        monkeypatch.setattr(kb.time, "time", lambda: now + 100)
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # 700 s after the last death -> past the stretched cooldown -> allowed
        # (and NOT trapped by blocker_auth).
        monkeypatch.setattr(kb.time, "time", lambda: now + 400)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_rate_limited_still_does_not_count_a_failure(kanban_home, monkeypatch):
    """The backoff must not turn a quota wall into a breaker trip."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-nofail", assignee="a")
        _seed_rate_limited(conn, tid, ended_at=now)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.consecutive_failures == 0
        assert task.status == "ready"


def test_streak_helper_counts_only_consecutive(kanban_home):
    """The streak helper stops at the first different outcome."""
    now = 5_000_000
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="streak", assignee="a")
        for i in range(3):
            _seed_rate_limited(conn, tid, ended_at=now - 300 * i)
        assert kbd._consecutive_rate_limited_runs(conn, tid) == 3

        # A NEWER run with a different outcome ends the streak.
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'a', 'crashed', 'crashed', ?, ?)",
            (tid, now + 100, now + 200),
        )
        conn.commit()
        assert kbd._consecutive_rate_limited_runs(conn, tid) == 0
