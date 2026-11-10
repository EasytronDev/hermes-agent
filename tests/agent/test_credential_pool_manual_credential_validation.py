"""A credential-pool row that cannot be a key must not be able to kill a turn.

Incident of 2026-11-10: two 20-character ``manual`` rows sat in one profile's
``credential_pool.minimax`` ahead of the real 125-character ``env:MINIMAX_API_KEY`` row. The
pool rung of the resolution ladder runs BEFORE the ``.env`` rung and picks the first row in file
order, so the worker sent the malformed key, the provider answered 401, and ten cards of that
profile died on their first call.

These tests pin the three defences:

1. the write-time refusal (``add_entry`` never stores such a row),
2. the selection-time skip (a store that already holds one cannot poison the pool), and
3. the cause code that tells the board WHICH credential failure happened.

The judgement is evidence-based: a ``manual`` row is measured against the pool's own
owner-provided credential (``env:``/``config:``/``model_config``). Without that reference there
is nothing to compare against and the row is accepted — so a provider the profile has no
configured key for is never refused.
"""

from __future__ import annotations

import json

import pytest


# The shape of the incident: a 20-character secret (the bad row) and a 125-character one (the
# good row the profile's .env holds). Only the LENGTHS matter here; no request is made.
BAD_SECRET = "g" * 20
GOOD_SECRET = "e" * 125


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _incident_pool() -> dict:
    """The pool exactly as it was on disk on 2026-11-10: two bad rows first, then the good one."""
    return {
        "version": 1,
        "providers": {},
        "credential_pool": {
            "minimax": [
                {"id": "38a4aa22", "label": "38a4aa22", "auth_type": "api_key", "priority": 0,
                 "source": "manual", "access_token": BAD_SECRET},
                {"id": "38a4aa11", "label": "38a4aa11", "auth_type": "api_key", "priority": 0,
                 "source": "manual", "access_token": "h" * 20},
                {"id": "38a4aa", "label": "MINIMAX_API_KEY", "auth_type": "api_key", "priority": 0,
                 "source": "env:MINIMAX_API_KEY", "access_token": GOOD_SECRET},
            ]
        },
    }


def _seed_env_credential(tmp_path, monkeypatch, name: str, value: str) -> None:
    """The profile's .env holds the good key (the owner-controlled source)."""
    (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "hermes" / ".env").write_text(f"{name}={value}\n", encoding="utf-8")
    monkeypatch.setenv(name, value)


def _hermetic_profile(tmp_path, monkeypatch) -> None:
    """Point the profile at *tmp_path* and strip the provider's ambient credentials.

    The pool seeds itself from the environment: ``get_env_prefer_dotenv`` reads the
    profile's ``.env`` and falls back to the process environment (the secret scope
    reads ``os.environ`` when no scope is bound). A key left in ``os.environ`` by
    another module — the runtime's own env loader installs the profile's ``.env`` into
    ``os.environ`` process-wide — would therefore seed a row into this test's store and
    make the test depend on whatever ran before it. The provider's key vars are removed
    so each test starts from the store it wrote and nothing else.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    for name in ("MINIMAX_API_KEY", "MINIMAX_TOKEN", "MINIMAX_API_HOST"):
        monkeypatch.delenv(name, raising=False)


# ── defence 1: the write-time refusal ──────────────────────────────────────────────────────────────

def test_add_entry_refuses_a_manual_credential_that_cannot_be_the_configured_key(tmp_path, monkeypatch):
    """A 20-character ``manual`` row never reaches the store: the cost of a 401 is the card."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _incident_pool())
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool
    from agent.credential_pool_admin import CredentialRejectedError

    pool = load_pool("minimax")
    with pytest.raises(CredentialRejectedError) as excinfo:
        pool.add_entry(PooledCredential(
            provider="minimax", id="badbad", label="badbad", auth_type=AUTH_TYPE_API_KEY,
            priority=0, source=SOURCE_MANUAL, access_token=BAD_SECRET))
    assert "minimax" in str(excinfo.value)
    # Nothing was written: the store does not hold the refused row.
    assert [e.id for e in pool.entries()] == ["38a4aa22", "38a4aa11", "38a4aa"]


def test_add_entry_accepts_a_credential_the_same_length_as_the_configured_key(tmp_path, monkeypatch):
    """The refusal judges the SHAPE against the configured key, not an absolute length."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _incident_pool())
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool

    pool = load_pool("minimax")
    entry = pool.add_entry(PooledCredential(
        provider="minimax", id="good123", label="good123", auth_type=AUTH_TYPE_API_KEY,
        priority=0, source=SOURCE_MANUAL, access_token=GOOD_SECRET))
    assert entry.id == "good123"


def test_add_entry_accepts_a_manual_credential_when_the_pool_has_no_configured_key(tmp_path, monkeypatch):
    """No evidence, no judgement: a provider the profile holds no key for is never refused."""
    _hermetic_profile(tmp_path, monkeypatch)
    from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool

    pool = load_pool("minimax")
    entry = pool.add_entry(PooledCredential(
        provider="minimax", id="shortok", label="shortok", auth_type=AUTH_TYPE_API_KEY,
        priority=0, source=SOURCE_MANUAL, access_token="short"))
    assert entry.id == "shortok"


def test_add_entry_accepts_a_non_manual_source_of_any_length(tmp_path, monkeypatch):
    """A credential Hermes obtained from the provider itself is not the owner's to judge."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _incident_pool())
    from agent.credential_pool import AUTH_TYPE_API_KEY, PooledCredential, load_pool

    pool = load_pool("minimax")
    entry = pool.add_entry(PooledCredential(
        provider="minimax", id="okrow1", label="okrow1", auth_type=AUTH_TYPE_API_KEY,
        priority=0, source="env:MINIMAX_API_KEY", access_token="short"))
    assert entry.id == "okrow1"


# ── defence 2: the selection-time skip ────────────────────────────────────────────────────────────

def test_pool_never_selects_a_malformed_manual_row(tmp_path, monkeypatch):
    """A store that ALREADY holds a malformed row cannot put it in front of a healthy sibling."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _incident_pool())
    from agent.credential_pool import load_pool

    pool = load_pool("minimax")
    selected = pool.select()
    assert selected is not None
    # The good row (the .env one), not the malformed one that sits first in the file.
    assert selected.id == "38a4aa"
    assert selected.runtime_api_key == GOOD_SECRET


def test_pool_peek_skips_a_malformed_manual_row(tmp_path, monkeypatch):
    """``peek`` is what the credential resolver reads: it must not hand back the bad row."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _incident_pool())
    from agent.credential_pool import load_pool

    peeked = load_pool("minimax").peek()
    assert peeked is not None
    assert peeked.id == "38a4aa"


def test_runtime_resolution_sends_the_env_credential_not_the_malformed_pool_row(tmp_path, monkeypatch):
    """THE REGRESSION TEST: the request carries the good key, as it did before the incident.

    The pool rung of the ladder runs before the ``.env`` rung, so a malformed row that reached
    the store used to be the credential every request carried.
    """
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _incident_pool())
    _seed_env_credential(tmp_path, monkeypatch, "MINIMAX_API_KEY", GOOD_SECRET)
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested="minimax", target_model="minimax-m3")
    assert runtime["api_key"] == GOOD_SECRET
    assert runtime.get("source") != "manual"


def test_a_manual_row_is_still_selected_when_the_pool_holds_no_configured_key(tmp_path, monkeypatch):
    """Fail-open: without a configured key to compare against, the pool serves what it holds."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, {"version": 1, "credential_pool": {"openrouter": [
        {"id": "row1", "label": "row1", "auth_type": "api_key", "priority": 0,
         "source": "manual", "access_token": "tok-1"}]}})
    from agent.credential_pool import load_pool

    selected = load_pool("openrouter").select()
    assert selected is not None
    assert selected.id == "row1"


# ── defence 3: the cause code ───────────────────────────────────────────────────────────────────

def test_a_401_is_classified_as_invalid_api_key():
    """A key WAS sent and the provider refused it: the cause says so."""
    from agent.error_classifier import classify_api_error

    class _FakeResponse:
        status_code = 401

        def json(self):
            return {"error": {"message": "login fail: Please carry the API secret key"}}

    class _FakeError(Exception):
        status_code = 401
        response = _FakeResponse()

    classified = classify_api_error(_FakeError("401 unauthorized"), provider="minimax")
    assert classified.is_auth
    assert classified.failure_cause == "invalid_api_key"


def test_the_auth_copy_carries_the_cause_code():
    """The rendered text reaches ``last_failure_error``: it must name the cause."""
    from agent.error_classifier import FailoverReason, classify_api_error
    from agent.turn_failure_copy import nonretryable_copy

    class _FakeResponse:
        status_code = 401

        def json(self):
            return {"error": {"message": "login fail"}}

    class _FakeError(Exception):
        status_code = 401
        response = _FakeResponse()

    classified = classify_api_error(_FakeError("401 unauthorized"), provider="minimax")
    assert classified.reason is FailoverReason.auth
    copy = nonretryable_copy(classified, provider="minimax", model="minimax-m3",
                             summary="login fail: Please carry the API secret key")
    assert "invalid_api_key" in copy
    # The old sentence said the same thing for every credential failure; the cause names which.
    assert "rejected your API key, so the model can't be reached" not in copy


def test_a_missing_key_is_reported_as_missing_api_key():
    """No key was sent: the cause says THAT, not "the key was rejected"."""
    from hermes_cli.auth import AuthError, format_auth_error

    error = AuthError("No usable credentials found for provider 'minimax'. Set MINIMAX_API_KEY.",
                      provider="minimax", code="missing_api_key")
    rendered = format_auth_error(error)
    assert "missing_api_key" in rendered
    assert "rejected your API key" not in rendered


def test_the_cause_codes_are_stable_tokens():
    """The cause codes are tokens, not prose: a classifier matches on them."""
    from agent.credential_pool_validation import (
        FAILURE_CAUSE_INVALID_API_KEY, FAILURE_CAUSE_MISSING_API_KEY,
        FAILURE_CAUSE_POOL_CREDENTIAL_INVALID,
    )

    assert FAILURE_CAUSE_MISSING_API_KEY == "missing_api_key"
    assert FAILURE_CAUSE_INVALID_API_KEY == "invalid_api_key"
    assert FAILURE_CAUSE_POOL_CREDENTIAL_INVALID == "pool_credential_invalid"


def test_a_malformed_manual_row_reports_the_pool_cause():
    """The pool row's own defect carries the ``pool_credential_invalid`` code."""
    from agent.credential_pool_validation import manual_pool_credential_defect

    row = {"id": "38a4aa22", "label": "38a4aa22", "auth_type": "api_key", "priority": 0,
           "source": "manual", "access_token": BAD_SECRET}
    assert manual_pool_credential_defect("minimax", row, reference_length=len(GOOD_SECRET)) == \
        "pool_credential_invalid"
    # Without the reference (no configured key to compare against) there is no defect.
    assert manual_pool_credential_defect("minimax", row) is None


def test_a_stored_malformed_row_is_not_deleted_but_not_selected(tmp_path, monkeypatch):
    """The skip is a SELECTION rule, not a delete: the row stays for the operator to inspect."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _incident_pool())
    from agent.credential_pool import load_pool
    from hermes_cli.auth import read_credential_pool

    pool = load_pool("minimax")
    assert len(pool.entries()) == 3  # all three rows are still there
    assert pool.select().id == "38a4aa"
    # And the store still holds it (nothing was silently destroyed).
    rows = read_credential_pool("minimax")
    ids = {row.get("id") for row in (rows or []) if isinstance(row, dict)}
    assert {"38a4aa22", "38a4aa11", "38a4aa"} <= ids
