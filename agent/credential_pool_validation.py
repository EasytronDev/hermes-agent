"""Shape checks for a credential-pool row, applied when the row is written and when it is selected.

A pool row is the LAST credential source the runtime consults before it talks to a provider, so a
malformed row there is not a cosmetic defect: it is the credential the request carries. On
2026-11-10 two ``manual`` rows of 20 characters sat in ``credential_pool.minimax`` of one profile,
ahead of the 125-character ``env:MINIMAX_API_KEY`` row, and the pool rung of the resolution ladder
(``hermes_cli.runtime_provider._resolve_from_pool``) selected them first — the provider answered
401 and the worker died on its first call.

Two rules keep that from repeating:

* **A ``manual`` row is rejected when it is written** (``CredentialPool.add_entry``) if its secret
  cannot be a credential for that provider. The cost of a wrong key is the whole card; the cost of
  refusing one at write time is one line of output.
* **A row already on disk is skipped when it is selected** (``CredentialPool._available_entries``),
  so a store written before the check existed (or by a tool that never had it) cannot poison the
  pool either.

The checks are deliberately conservative. A provider whose key shape this module cannot judge is
accepted (fail-open): refusing a legitimate credential is worse than letting a questionable one
through to the provider's own 401.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "POOL_CREDENTIAL_DEFECT_INVALID",
    "POOL_CREDENTIAL_DEFECT_EMPTY",
    "manual_pool_credential_defect",
    "pool_credential_defect",
    "pool_credential_defect_reason",
]

# Cause code carried by the row (and by the AuthError the runtime raises for it). The dispatcher
# classifies on the text of ``last_failure_error``; a stable token in that text is what lets it
# tell "the credential was refused" from "no credential was sent" and from "the quota is spent".
POOL_CREDENTIAL_DEFECT_INVALID = "pool_credential_invalid"
POOL_CREDENTIAL_DEFECT_EMPTY = "missing_api_key"

# A secret shorter than this cannot be a real API key for any provider Hermes talks to: the
# shortest keys in the wild (a handful of characters) are local-server placeholders, which the
# pool never carries as a ``manual`` row. 32 is far below every real key (the MiniMax key that
# failed here was 125 characters) and far above the 20-character value that poisoned the pool.
POOL_CREDENTIAL_MIN_LENGTH = 32

# Source names that carry a credential Hermes minted or adopted itself (OAuth grants, device-code
# logins, tokens read from another tool). Their shape is not the user's to judge, so the length
# floor does not apply to them; only the ``manual`` family is judged.
_MANUAL_SOURCE_PREFIX = "manual"


def _is_manual_source(source: Any) -> bool:
    normalized = str(source or "").strip().lower()
    return normalized == _MANUAL_SOURCE_PREFIX or normalized.startswith(f"{_MANUAL_SOURCE_PREFIX}:")


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


def manual_pool_credential_defect(provider: str, entry: Any) -> Optional[str]:
    """``None`` when *entry* may serve *provider*; else the defect code.

    Only ``manual`` api-key rows are judged. An OAuth row (``auth_type`` ``oauth``/``oauth_external``)
    carries a bearer grant whose shape the provider defines, and a non-manual source is a
    credential Hermes obtained from the provider itself.
    """
    if isinstance(entry, dict):
        source = entry.get("source")
        auth_type = str(entry.get("auth_type") or "api_key")
    else:
        source = getattr(entry, "source", None)
        auth_type = str(getattr(entry, "auth_type", "") or "api_key")
    if not _is_manual_source(source):
        return None
    if auth_type not in ("", "api_key"):
        return None
    secret = _secret_of(entry)
    if not secret:
        return POOL_CREDENTIAL_DEFECT_EMPTY
    if len(secret) < POOL_CREDENTIAL_MIN_LENGTH:
        return POOL_CREDENTIAL_DEFECT_INVALID
    return None


def pool_credential_defect(provider: str, entry: Any) -> Optional[str]:
    """``None`` when *entry* may serve *provider*; else the defect code.

    Alias of :func:`manual_pool_credential_defect` kept as the generic name so callers that hold
    an arbitrary pool row do not have to know that the judgement is scoped to ``manual`` rows.
    """
    return manual_pool_credential_defect(provider, entry)


def pool_credential_defect_reason(provider: str, entry: Any) -> str:
    """A human sentence naming the defect, for the write-time refusal and the log line."""
    defect = manual_pool_credential_defect(provider, entry)
    if defect is None:
        return ""
    if defect == POOL_CREDENTIAL_DEFECT_EMPTY:
        return (f"The {provider} credential has no secret, so it cannot serve a request. "
                "Add a real key before it is used.")
    return (
        f"The {provider} credential is too short to be a real API key "
        f"(fewer than {POOL_CREDENTIAL_MIN_LENGTH} characters), so it would be refused by the "
        f"provider with a 401 on the first request. It was not added to the pool."
    )
