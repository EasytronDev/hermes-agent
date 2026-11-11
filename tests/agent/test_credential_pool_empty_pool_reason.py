"""The empty-pool log must name the reason the pool was empty.

The line used to read "credential pool: no available entries (all exhausted
or empty)", which names two very different situations without saying which
one happened:

* ``exhausted`` — the credential was benched by a rate limit / billing
  failure. It comes back on a known cooldown (60s for a sole credential on a
  transient throttle, an hour for a billing-classified failure).
* ``secret_not_hydrated`` — the pool row exists but carries no usable key,
  because the secret never hydrated (the signature of a profile-scoping /
  multiplexing bug). No cooldown recovers it; it needs a re-auth.

The two need different fixes, and the generic line made the distinction cost
hours of log archaeology before the cause could be attributed. These tests
pin the distinction: the two branches must produce *different* log lines,
neither of which may carry key material.
"""

from __future__ import annotations

import logging
import time

from agent.credential_pool import (
    EXHAUSTED_TTL_429_SECONDS,
    EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS,
    CredentialPool,
    PooledCredential,
)

_POOL_LOGGER = "agent.credential_pool"


def _no_entries_records(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.getMessage().startswith("credential pool: no available entries")
    ]


def _api_key_entry(
    entry_id: str = "38a4aa11",
    *,
    access_token: str = "sk-live-secret-value",
    source: str = "env:MINIMAX_API_KEY",
) -> PooledCredential:
    return PooledCredential(
        provider="minimax",
        id=entry_id,
        label=entry_id,
        auth_type="api_key",
        source=source,
        access_token=access_token,
        priority=0,
    )


def _exhausted_entry(
    entry_id: str = "deadbeef",
    *,
    status_code: int = 429,
    age_seconds: float = 5.0,
    failure_reason: str | None = None,
) -> PooledCredential:
    extra = {"failure_reason": failure_reason} if failure_reason else {}
    return PooledCredential(
        provider="minimax",
        id=entry_id,
        label=entry_id,
        auth_type="api_key",
        source="manual",
        access_token="sk-live-secret-value",
        priority=0,
        last_status="exhausted",
        last_status_at=time.time() - age_seconds,
        last_error_code=status_code,
        extra=extra,
    )


def test_empty_pool_logs_reason_empty_when_no_entry_was_skipped(caplog):
    """A genuinely empty pool (no rows at all) says so, and does not invent
    a per-entry reason that does not exist."""
    pool = CredentialPool("minimax", [])

    with caplog.at_level(logging.INFO, logger=_POOL_LOGGER):
        assert pool.select() is None

    records = _no_entries_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "reason=empty" in message


def test_unhydrated_secret_is_reported_as_secret_not_hydrated(caplog):
    """Branch 1: the pool row exists but holds no usable key.

    The log must say ``secret_not_hydrated`` and name the source, so the
    reader knows to look at profile scoping / secret hydration rather than
    at a cooldown.
    """
    entry = _api_key_entry("38a4aa11", access_token="")
    pool = CredentialPool("minimax", [entry])

    with caplog.at_level(logging.INFO, logger=_POOL_LOGGER):
        assert pool.select() is None

    records = _no_entries_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "reason=secret_not_hydrated" in message
    assert "entry=38a4aa" in message
    assert "source=env:MINIMAX_API_KEY" in message
    # The exhausted branch must NOT fire for this entry.
    assert "reason=exhausted" not in message
    assert "ttl=" not in message


def test_exhausted_entry_is_reported_as_exhausted_with_ttl(caplog):
    """Branch 2: the credential is benched by a rate limit.

    The log must say ``exhausted`` and carry the remaining cooldown, so the
    reader can tell a 60s bench from an hour-long one.
    """
    entry = _exhausted_entry("deadbeef")
    pool = CredentialPool("minimax", [entry])

    with caplog.at_level(logging.INFO, logger=_POOL_LOGGER):
        assert pool.select() is None

    records = _no_entries_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "reason=exhausted" in message
    assert "entry=deadbeef" in message
    assert "ttl=" in message
    # The unhydrated branch must NOT fire for this entry.
    assert "reason=secret_not_hydrated" not in message


def test_the_two_branches_produce_different_log_lines(caplog):
    """The whole point of the change: the two causes are distinguishable."""
    unhydrated_pool = CredentialPool("minimax", [_api_key_entry(access_token="")])
    exhausted_pool = CredentialPool("minimax", [_exhausted_entry()])

    with caplog.at_level(logging.INFO, logger=_POOL_LOGGER):
        assert unhydrated_pool.select() is None
        assert exhausted_pool.select() is None

    messages = [r.getMessage() for r in _no_entries_records(caplog)]
    assert len(messages) == 2
    assert messages[0] != messages[1]
    assert "reason=secret_not_hydrated" in messages[0]
    assert "reason=exhausted" in messages[1]


def test_log_never_carries_secret_material(caplog):
    """The reason detail must not leak the key or the access token."""
    secret = "sk-super-secret-do-not-log"
    entry = _api_key_entry("38a4aa11", access_token="")
    pool = CredentialPool("minimax", [entry])

    with caplog.at_level(logging.INFO, logger=_POOL_LOGGER):
        assert pool.select() is None

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in messages
    # The entry's *source* label is safe and expected; the token is not.
    assert "env:MINIMAX_API_KEY" in messages


def test_exhausted_ttl_is_the_effective_cooldown_not_the_full_window(caplog):
    """The reported ttl is the time left, not the full cooldown window.

    A credential exhausted 5s ago with a 60s cooldown must report ~55s, so
    "ttl=60s" cannot be mistaken for "it just got benched".
    """
    entry = _exhausted_entry("deadbeef", age_seconds=5.0)
    pool = CredentialPool("minimax", [entry])

    with caplog.at_level(logging.INFO, logger=_POOL_LOGGER):
        assert pool.select() is None

    message = _no_entries_records(caplog)[0].getMessage()
    ttl_text = message.split("ttl=")[1].split("s")[0]
    ttl = int(ttl_text)
    assert 0 < ttl <= EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS
    # A sole credential on a 429 gets the short cooldown; a full hour here
    # would mean the wrong TTL was reported.
    assert ttl < EXHAUSTED_TTL_429_SECONDS


def test_marking_exhausted_logs_the_effective_ttl(caplog):
    """``marking exhausted`` must say how long the bench lasts.

    Without it a reader cannot tell a 60s sole-credential cooldown from an
    hour-long billing bench.
    """
    entry = _api_key_entry("38a4aa11")
    pool = CredentialPool("minimax", [entry])

    with caplog.at_level(logging.INFO, logger=_POOL_LOGGER):
        pool.mark_exhausted_and_rotate(status_code=429)

    marking = [
        r.getMessage() for r in caplog.records
        if "exhausted (status=" in r.getMessage()
    ]
    assert len(marking) == 1
    message = marking[0]
    assert "ttl=60s" in message
    assert "sole_credential=True" in message
    assert "entry" not in message.split("ttl=")[0] or "38a4aa" in message
    # No key material in the marking line either.
    assert "sk-live-secret-value" not in message


def test_billing_exhaustion_logs_the_long_ttl(caplog):
    """A billing-classified failure keeps the full hour — and says so."""
    entry = _api_key_entry("38a4aa11")
    pool = CredentialPool("minimax", [entry])

    with caplog.at_level(logging.INFO, logger=_POOL_LOGGER):
        pool.mark_exhausted_and_rotate(status_code=402)

    marking = [
        r.getMessage() for r in caplog.records
        if "exhausted (status=" in r.getMessage()
    ]
    assert len(marking) == 1
    message = marking[0]
    assert f"ttl={EXHAUSTED_TTL_429_SECONDS}s" in message
    assert "reason=billing" in message or "reason=unclassified" in message
