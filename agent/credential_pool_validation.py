"""Shape checks for a credential-pool row, applied when the row is written and when it is selected.

A pool row is the LAST credential source the runtime consults before it talks to a provider, so a
malformed row there is not a cosmetic defect: it is the credential the request carries. On
2026-11-10 two ``manual`` rows of 20 characters sat in ``credential_pool.minimax`` of one profile,
ahead of the 125-character ``env:MINIMAX_API_KEY`` row, and the pool rung of the resolution ladder
(``hermes_cli.runtime_provider._resolve_from_pool``) selected them first — the provider answered
401 and the worker died on its first call.

The judgement is EVIDENCE-BASED, not a fixed length rule: a ``manual`` row is refused only when
the same pool already holds an owner-provided credential (``env:``/``config:``/``model_config``)
for that provider, and the new row's secret is far shorter than it. A fixed floor would reject the
short keys some providers legitimately issue, and would break every fixture that uses a short
token; comparing against the credential the profile's own ``.env`` holds is both safe and precise:
a real key for a provider whose configured key is 125 characters is not 20 characters.

Two rules keep the incident from repeating:

* **A ``manual`` row is rejected when it is written** (``CredentialPool.add_entry``) if it cannot
  be a credential for that provider. The cost of a wrong key is the whole card; the cost of
  refusing one at write time is one line of output.
* **A row already on disk is skipped when it is selected** (``CredentialPool._available_entries``),
  so a store written before the check existed (or by a tool that never had it) cannot poison the
  pool either.

Both are fail-open: a provider whose credentials the pool cannot judge is accepted, because
refusing a legitimate credential is worse than letting a questionable one through to the
provider's own 401.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

__all__ = [
    "FAILURE_CAUSE_INVALID_API_KEY",
    "FAILURE_CAUSE_MISSING_API_KEY",
    "FAILURE_CAUSE_POOL_CREDENTIAL_INVALID",
    "POOL_CREDENTIAL_DEFECT_INVALID",
    "POOL_CREDENTIAL_DEFECT_EMPTY",
    "manual_pool_credential_defect",
    "pool_credential_defect",
    "pool_credential_defect_reason",
    "reference_credential_length",
]

# Cause codes carried by the row (and by the AuthError the runtime raises for it). The dispatcher
# classifies on the text of ``last_failure_error``; a stable token in that text is what lets it
# tell "the credential was refused" from "no credential was sent" and from "the quota is spent".
POOL_CREDENTIAL_DEFECT_INVALID = "pool_credential_invalid"
POOL_CREDENTIAL_DEFECT_EMPTY = "missing_api_key"

# The same three causes, named for the turn/board layer. ``missing_api_key`` — the runtime
# refused before the request, so NO key was sent. ``invalid_api_key`` — a key was sent and the
# provider rejected it. ``pool_credential_invalid`` — the key that was sent came from a
# credential-pool row that cannot be a key for this provider.
FAILURE_CAUSE_MISSING_API_KEY = "missing_api_key"
FAILURE_CAUSE_INVALID_API_KEY = "invalid_api_key"
FAILURE_CAUSE_POOL_CREDENTIAL_INVALID = POOL_CREDENTIAL_DEFECT_INVALID

# A ``manual`` row is compared against the pool's own owner-provided credential for the same
# provider. The ratio is how much SHORTER the suspicious row is than the credential the profile
# really holds; the floor keeps the comparison from firing on a truncation or a rounding
# difference (a 100-character real key against a 99-character copy is not a malformed key).
_DEFECT_LENGTH_RATIO = 0.5

# The comparison needs a reference long enough to be a real key. Below this the pool has no
# usable evidence about the provider's key shape, so the check stays out of the way.
_MIN_REFERENCE_LENGTH = 16

# Source names that carry a credential the OWNER configured (the profile's ``.env``, a custom
# endpoint's ``config.yaml``, the model block's inline key). Their length is the evidence the
# judgement uses; a ``manual`` row is measured against them.
_OWNER_SOURCE_PREFIXES = ("env:", "config:", "model_config")

# Source names that carry a credential Hermes minted or adopted itself (OAuth grants, device-code
# logins, tokens read from another tool). Their shape is not the owner's to judge, and a row
# from one of them is never the suspect.
_MANUAL_SOURCE_PREFIX = "manual"


def _is_manual_source(source: Any) -> bool:
    normalized = str(source or "").strip().lower()
    return normalized == _MANUAL_SOURCE_PREFIX or normalized.startswith(f"{_MANUAL_SOURCE_PREFIX}:")


def _is_owner_source(source: Any) -> bool:
    normalized = str(source or "").strip().lower()
    return any(normalized.startswith(prefix) for prefix in _OWNER_SOURCE_PREFIXES)


def _source_of(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("source") or "")
    return str(getattr(entry, "source", "") or "")


def _auth_type_of(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("auth_type") or "api_key")
    return str(getattr(entry, "auth_type", "") or "api_key")


def _secret_of(entry: Any) -> str:
    """The runtime secret of a pool row, whatever shape the caller holds."""
    if isinstance(entry, dict):
        for key in ("access_token", "api_key", "token", "agent_key"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    for key in ("runtime_api_key", "access_token"):
        value = getattr(entry, key, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def reference_credential_length(entries: Iterable[Any]) -> int:
    """The length of the longest owner-provided credential in *entries*; 0 when there is none.

    The owner-provided rows (``env:``/``config:``/``model_config``) are the evidence: they come
    from the profile's own configuration, so their secret is a real key for that provider.
    """
    longest = 0
    for entry in entries or ():
        if not _is_owner_source(_source_of(entry)):
            continue
        if _auth_type_of(entry) not in ("", "api_key"):
            continue
        secret = _secret_of(entry)
        if len(secret) > longest:
            longest = len(secret)
    return longest


def manual_pool_credential_defect(
    provider: str,
    entry: Any,
    *,
    reference_length: int = 0,
) -> Optional[str]:
    """``None`` when *entry* may serve *provider*; else the defect code.

    *reference_length* is the length of the pool's owner-provided credential for the same
    provider (see :func:`reference_credential_length`). Without it there is no evidence and the
    row is accepted.
    """
    if not _is_manual_source(_source_of(entry)):
        return None
    if _auth_type_of(entry) not in ("", "api_key"):
        return None
    secret = _secret_of(entry)
    if not secret:
        return POOL_CREDENTIAL_DEFECT_EMPTY
    reference = int(reference_length or 0)
    if reference < _MIN_REFERENCE_LENGTH:
        return None
    if len(secret) >= reference * _DEFECT_LENGTH_RATIO:
        return None
    return POOL_CREDENTIAL_DEFECT_INVALID


def pool_credential_defect(
    provider: str,
    entry: Any,
    *,
    reference_length: int = 0,
) -> Optional[str]:
    """``None`` when *entry* may serve *provider*; else the defect code.

    Alias of :func:`manual_pool_credential_defect` kept as the generic name so callers that hold
    an arbitrary pool row do not have to know that the judgement is scoped to ``manual`` rows.
    """
    return manual_pool_credential_defect(provider, entry, reference_length=reference_length)


def pool_credential_defect_reason(provider: str, entry: Any, *, reference_length: int = 0) -> str:
    """A human sentence naming the defect, for the write-time refusal and the log line."""
    defect = manual_pool_credential_defect(provider, entry, reference_length=reference_length)
    if defect is None:
        return ""
    if defect == POOL_CREDENTIAL_DEFECT_EMPTY:
        return (f"The {provider} credential has no secret, so it cannot serve a request. "
                "Add a real key before it is used.")
    return (
        f"The {provider} credential is too short to be the key this profile uses "
        f"(the configured key is {reference_length} characters, this one is "
        f"{len(_secret_of(entry))}), so the provider would refuse it with a 401 on the first "
        f"request. It was not added to the pool."
    )
