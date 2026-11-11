"""A 401 on a malformed pool row must be named ``pool_credential_invalid`` in PRODUCTION order.

The store on disk cannot carry the owner credential: an ``env:`` row is a BORROWED source, so
the disk boundary strips its secret and leaves only ``secret_fingerprint`` (measured: 11 owner
rows across 9 profiles, every one of them without ``access_token``). Judging a malformed row
against a 0-length reference cannot happen (``_MIN_REFERENCE_LENGTH`` is 16), so the defect
was invisible and the board saw ``invalid_api_key`` — "rotate the key" — for a pool that had
to be fixed.

These tests pin the WARM case, which the store-only test could not reach: the pool has been
loaded, the ``env:`` row is hydrated in memory from the profile's ``.env``, and the 401
carries the malformed row's key.

The fail-open half is pinned too: a provider the profile holds no owner credential for stays
``invalid_api_key``, because a board must never be told "the pool is broken" without evidence.
"""

from __future__ import annotations

import hashlib
import json

import pytest


# The shape of the incident: a 20-character secret (the bad row) and a 125-character one (the
# good row the profile's .env holds). Only the LENGTHS matter here; no request is made.
BAD_SECRET = "g" * 20
GOOD_SECRET = "e" * 125


def _fingerprint(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _warm_pool() -> dict:
    """The pool exactly as it sits on disk in production: the owner row has NO secret.

    The borrow sanitizer (``agent.credential_persistence.sanitize_borrowed_credential_payload``)
    removes ``access_token`` from a borrowed row and leaves the fingerprint, so this is what a
    real profile's ``auth.json`` holds — not the test that writes the secret to disk.
    """
    return {
        "version": 1,
        "providers": {},
        "credential_pool": {
            "minimax": [
                {"id": "38a4aa22", "label": "38a4aa22", "auth_type": "api_key", "priority": 0,
                 "source": "manual", "access_token": BAD_SECRET},
                {"id": "38a4aa", "label": "MINIMAX_API_KEY", "auth_type": "api_key", "priority": 0,
                 "source": "env:MINIMAX_API_KEY", "secret_fingerprint": _fingerprint(GOOD_SECRET)},
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


class _FakeResponse:
    status_code = 401

    def json(self):
        return {"error": {"message": "login fail: Please carry the API secret key"}}


class _FakeError(Exception):
    status_code = 401
    response = _FakeResponse()


def test_a_401_on_a_defective_pool_row_is_named_pool_credential_invalid_when_the_pool_is_warm(
    tmp_path, monkeypatch
):
    """THE PRODUCTION ORDER: the store has no owner secret, the profile's .env has it.

    The 401 carries the malformed ``manual`` row's key. The pool's owner-provided credential
    — the 125-character key the profile really holds — is what the malformed row is measured
    against, and it is read from the source the pool itself hydrates it from.
    """
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _warm_pool())
    _seed_env_credential(tmp_path, monkeypatch, "MINIMAX_API_KEY", GOOD_SECRET)
    from agent.error_classifier import classify_api_error

    classified = classify_api_error(_FakeError("401 unauthorized"), provider="minimax",
                                    api_key=BAD_SECRET)
    assert classified.is_auth
    assert classified.failure_cause == "pool_credential_invalid"


def test_the_store_alone_carries_no_owner_secret(tmp_path, monkeypatch):
    """Why the store-only measurement could not judge: the owner row has no secret on disk.

    This is the premise the warm case rests on, and it holds in every real profile.
    """
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _warm_pool())
    from agent.credential_pool_validation import reference_credential_length
    from hermes_cli.auth import read_credential_pool

    rows = read_credential_pool("minimax") or []
    owner_rows = [r for r in rows if isinstance(r, dict)
                  and str(r.get("source") or "").startswith("env:")]
    assert owner_rows, "the store must hold the owner row"
    assert not any(r.get("access_token") for r in owner_rows)
    assert reference_credential_length(rows) == 0


def test_a_401_without_an_owner_credential_stays_invalid_api_key(tmp_path, monkeypatch):
    """Fail-open: no owner credential means no evidence, so the generic cause holds."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _warm_pool())
    from agent.error_classifier import classify_api_error

    classified = classify_api_error(_FakeError("401 unauthorized"), provider="minimax",
                                    api_key=BAD_SECRET)
    assert classified.failure_cause == "invalid_api_key"


def test_a_401_on_the_owner_key_stays_invalid_api_key(tmp_path, monkeypatch):
    """The key that was sent IS the profile's own: the remedy is to rotate it, not to fix a pool."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _warm_pool())
    _seed_env_credential(tmp_path, monkeypatch, "MINIMAX_API_KEY", GOOD_SECRET)
    from agent.error_classifier import classify_api_error

    classified = classify_api_error(_FakeError("401 unauthorized"), provider="minimax",
                                    api_key=GOOD_SECRET)
    assert classified.failure_cause == "invalid_api_key"


def test_the_cause_is_unchanged_for_a_provider_the_profile_holds_no_key_for(tmp_path, monkeypatch):
    """A provider with no owner credential at all: nothing to compare against, nothing to say."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, {"version": 1, "credential_pool": {"openrouter": [
        {"id": "row1", "label": "row1", "auth_type": "api_key", "priority": 0,
         "source": "manual", "access_token": BAD_SECRET}]}})
    from agent.error_classifier import classify_api_error

    classified = classify_api_error(_FakeError("401 unauthorized"), provider="openrouter",
                                    api_key=BAD_SECRET)
    assert classified.failure_cause == "invalid_api_key"


def test_classifying_a_401_does_not_touch_the_store(tmp_path, monkeypatch):
    """The classifier runs inside a 401: it may not write to the profile's credential store."""
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _warm_pool())
    _seed_env_credential(tmp_path, monkeypatch, "MINIMAX_API_KEY", GOOD_SECRET)
    auth_path = tmp_path / "hermes" / "auth.json"
    before = auth_path.read_bytes()
    from agent.error_classifier import classify_api_error

    classify_api_error(_FakeError("401 unauthorized"), provider="minimax", api_key=BAD_SECRET)
    assert auth_path.read_bytes() == before


def test_the_longest_owner_credential_is_the_reference(tmp_path, monkeypatch):
    """Two owner credentials: the longer one is the reference, as the pool measures it.

    The pool's own rule is the LONGEST owner-provided credential (``reference_credential_length``),
    so the classifier must use the same one and not the first env var it happens to find.
    """
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, {"version": 1, "credential_pool": {"minimax": [
        {"id": "m1", "label": "m1", "auth_type": "api_key", "priority": 0,
         "source": "manual", "access_token": BAD_SECRET},
        {"id": "e1", "label": "MINIMAX_API_KEY", "auth_type": "api_key", "priority": 0,
         "source": "env:MINIMAX_API_KEY", "secret_fingerprint": _fingerprint(GOOD_SECRET)},
        {"id": "e2", "label": "OTHER_KEY", "auth_type": "api_key", "priority": 0,
         "source": "env:OTHER_KEY", "secret_fingerprint": _fingerprint("h" * 200)},
    ]}})
    _seed_env_credential(tmp_path, monkeypatch, "MINIMAX_API_KEY", GOOD_SECRET)
    (tmp_path / "hermes" / ".env").write_text(
        f"MINIMAX_API_KEY={GOOD_SECRET}\nOTHER_KEY={'h' * 200}\n", encoding="utf-8")
    from agent.error_classifier import _owner_credential_reference_length
    from hermes_cli.auth import read_credential_pool

    reference = _owner_credential_reference_length(read_credential_pool("minimax") or [])
    assert reference == 200


def test_the_reference_matches_the_pool_itself(tmp_path, monkeypatch):
    """The number the classifier uses is the number the pool would use.

    This is the property that keeps the two in step: a third criterion of "long" would drift
    from the pool and start refusing legitimate credentials.
    """
    _hermetic_profile(tmp_path, monkeypatch)
    _write_auth_store(tmp_path, _warm_pool())
    _seed_env_credential(tmp_path, monkeypatch, "MINIMAX_API_KEY", GOOD_SECRET)
    from agent.credential_pool import load_pool
    from agent.credential_pool_validation import reference_credential_length
    from agent.error_classifier import _owner_credential_reference_length
    from hermes_cli.auth import read_credential_pool

    pool_reference = reference_credential_length(load_pool("minimax").entries())
    classifier_reference = _owner_credential_reference_length(read_credential_pool("minimax") or [])
    assert classifier_reference == pool_reference == len(GOOD_SECRET)
