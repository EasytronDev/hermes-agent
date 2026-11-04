"""Cooldown sizing for pooled credentials, and why the pool was empty.

Two closely related concerns live here:

**Cooldown sizing.** A credential that failed is benched for a while, and the
length of that bench depends on what failed and on what the pool can rotate
to. A sole credential on a transient throttle gets a short cooldown; a
billing-classified failure keeps the full hour. The functions here compute
that bench, and the pool logs it so a reader can tell "retry in a minute"
from "retry in an hour".

**Why the pool was empty.** The credential pool can be empty for several very
different reasons, and the empty-pool log used to print one generic line for
all of them:

    credential pool: no available entries (all exhausted or empty)

That text names two very different situations without saying which one
happened:

* ``exhausted`` — the credential was benched by a rate limit or a billing
  failure. It comes back on a known cooldown: 60s for a sole credential on a
  transient throttle, an hour for a billing-classified failure.
* ``secret_not_hydrated`` — the pool row exists but carries no usable key,
  because the secret never hydrated. This is the signature of a
  profile-scoping / multiplexing bug. No cooldown recovers it; it needs a
  re-auth.

The two need different fixes, so a reader of the log cannot tell them apart
from the generic line. The empty-pool line is also throttled to once per
60s, so it reports a *state* that has lost the origin of the state.

Only non-sensitive facts are rendered: the skip reason, the first eight
characters of the entry id, the entry's source label and, for an exhausted
entry, the seconds left on its cooldown. Key material and access tokens are
never rendered.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.credential_pool import PooledCredential

# The pool's own logger, so the lines keep flowing to the same handlers as
# every other credential-pool message (the module cannot define it at import
# time: ``credential_pool`` imports this module).
def _pool_logger() -> logging.Logger:
    from agent.credential_pool import logger
    return logger

# The credential is benched by a rate limit / billing failure and returns on
# a known cooldown.
POOL_SKIP_REASON_EXHAUSTED = "exhausted"
# The pool row exists but carries no usable key: the secret never hydrated.
POOL_SKIP_REASON_SECRET_NOT_HYDRATED = "secret_not_hydrated"
# The credential failed permanently and will not re-enter rotation.
POOL_SKIP_REASON_DEAD = "dead"
# Only the requested model is benched; the credential serves its siblings.
POOL_SKIP_REASON_MODEL_COOLDOWN = "model_cooldown"
# A borrowed OAuth row that failed to hydrate.
POOL_SKIP_REASON_OAUTH_NOT_HYDRATED = "oauth_not_hydrated"
# The token refresh failed, so the entry could not be used this round.
POOL_SKIP_REASON_REFRESH_FAILED = "refresh_failed"
# A single-use-token provider whose refresh is deferred to the caller.
POOL_SKIP_REASON_PENDING_REFRESH = "pending_refresh"

# One recorded skip: (reason, entry id, detail). The detail carries the
# remaining cooldown for an exhausted entry and the source label for an
# unhydrated one; it is ``None`` for the reasons that need no extra detail.
PoolSkip = tuple[str, str, Optional[str]]


def describe_skips(skips: Optional[list[PoolSkip]]) -> str:
    """Summarize why the pool was empty, without leaking secret material.

    Returns the parenthesized detail for the empty-pool log line. Every
    reason is listed: a pool can be empty for more than one reason at once,
    and collapsing them hides the actionable one.
    """
    if not skips:
        # No entry was skipped (an empty pool, or every entry was pruned
        # before selection) — say so rather than inventing a reason.
        return "reason=empty"
    parts: list[str] = []
    for reason, entry_id, detail in skips:
        entry_ref = (entry_id or "")[:8] or "unknown"
        if reason == POOL_SKIP_REASON_EXHAUSTED:
            ttl = detail or "unknown"
            parts.append(f"reason={reason} entry={entry_ref} ttl={ttl}s")
        elif reason == POOL_SKIP_REASON_SECRET_NOT_HYDRATED:
            source = detail or "unknown"
            parts.append(f"reason={reason} entry={entry_ref} source={source}")
        else:
            parts.append(f"reason={reason} entry={entry_ref}")
    return ", ".join(parts)


# --- cooldown sizing -------------------------------------------------------
#
# The TTL constants and the status names live in ``agent.credential_pool``
# (they are part of that module's published surface), so they are imported
# lazily here to keep the import graph acyclic: ``credential_pool`` imports
# this module.


def exhausted_ttl(
    error_code: Optional[int],
    *,
    sole_credential: bool = False,
    failure_reason: Optional[str] = None,
) -> int:
    """Return cooldown seconds based on the HTTP status that caused exhaustion.

    *sole_credential*: the pool has nothing to rotate to, so transient
    throttles (429 and the catch-all default covering 403/5xx/unknown) are
    capped to a brief cooldown; 401 keeps its own already-short TTL.

    *failure_reason* is the classifier verdict: an OpenRouter ``key limit
    exceeded`` and an xAI spending block both arrive as 403 but are billing,
    and a 60s retry on a spent account just re-fails. Billing keeps the full
    bench regardless of status; 402 is billing by definition.
    Unverified billing (#82154) gets the short cooldown regardless of pool
    size (the credential may be healthy), unless the status is a true 402.
    """
    from agent.credential_pool import (
        EXHAUSTED_TTL_401_SECONDS,
        EXHAUSTED_TTL_429_SECONDS,
        EXHAUSTED_TTL_DEFAULT_SECONDS,
        EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS,
        FAILURE_REASON_BILLING,
        FAILURE_REASON_BILLING_UNVERIFIED,
    )

    if error_code == 401:
        return EXHAUSTED_TTL_401_SECONDS
    base = EXHAUSTED_TTL_429_SECONDS if error_code == 429 else EXHAUSTED_TTL_DEFAULT_SECONDS
    if failure_reason == FAILURE_REASON_BILLING_UNVERIFIED and error_code != 402:
        return min(base, EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS)
    is_billing = error_code == 402 or failure_reason == FAILURE_REASON_BILLING
    if sole_credential and not is_billing:
        return min(base, EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS)
    return base


def exhausted_until(entry: PooledCredential, *, sole_credential: bool = False) -> Optional[float]:
    """The epoch at which *entry*'s cooldown ends, or ``None`` if it has none."""
    from agent.credential_pool import STATUS_EXHAUSTED
    from agent.credential_pool_timestamps import _parse_absolute_timestamp

    if entry.last_status != STATUS_EXHAUSTED:
        return None
    reset_at = _parse_absolute_timestamp(entry.last_error_reset_at)
    if reset_at is not None:
        return reset_at
    if entry.last_status_at:
        return entry.last_status_at + exhausted_ttl(
            entry.last_error_code,
            sole_credential=sole_credential,
            failure_reason=entry.failure_reason,
        )
    return None


# Alias used inside the selection loop, where the local variable
# ``exhausted_until`` would otherwise shadow the module-level function.
cooldown_end_for = exhausted_until


# --- selection -------------------------------------------------------------


class CredentialPoolSelectionMixin:
    """Choosing the entries a pool may hand out, and explaining an empty pool.

    The selection loop skips entries for several distinct reasons, and the
    empty-pool log has to name them: a credential benched by a rate limit and
    a credential whose secret never hydrated need different fixes. The loop
    records a reason per skipped entry; the log renders them.

    The mixin lives beside the cooldown helpers because the two are one
    concern: "why is this credential not available, and when will it be?"
    """

    def _available_entries(
        self, *, clear_expired: bool = False, refresh: bool = False, model: Optional[str] = None,
    ) -> tuple[list[PooledCredential], list[PooledCredential]]:
        """Return (available, pending_refresh) for entries not in cooldown.

        *clear_expired* resets elapsed cooldowns to STATUS_OK and persists.
        *refresh* refreshes entries needing a token refresh (skipped on
        failure) — except single-use-token providers (openai-codex,
        xai-oauth), which are returned as *pending_refresh* so the caller
        refreshes them outside the lock instead of stalling every pool
        consumer during cross-process flock acquisition + OAuth network I/O.
        """
        from agent.credential_pool import (
            AUTH_TYPE_API_KEY,
            AUTH_TYPE_OAUTH,
            DEAD_MANUAL_PRUNE_TTL_SECONDS,
            STATUS_DEAD,
            STATUS_EXHAUSTED,
            _MARK_OK,
            _TOKENS_SINGLETON_PROVIDERS,
            _is_manual_source,
        )
        from agent.credential_pool_model_cooldowns import model_cooldown_until
        from agent.credential_pool_validation import (
            manual_pool_credential_defect,
            reference_credential_length,
        )

        now = time.time()
        cleared_any = False
        entries_to_prune: list[str] = []
        available: list[PooledCredential] = []
        pending_refresh: list[PooledCredential] = []
        sole_credential = self._is_sole_credential()
        # The pool's own owner-provided credential (``env:``/``config:``/``model_config``) is the
        # evidence a ``manual`` row is measured against: a real key for a provider whose
        # configured key is 125 characters is not 20 characters. No reference, no judgement.
        reference_length = reference_credential_length(self._entries)
        # Why each entry was skipped. The empty-pool log needs the *reason*,
        # not just the fact: "exhausted" (recoverable, known cooldown) and
        # "secret_not_hydrated" (the row holds no usable key — a
        # profile-scoping / multiplexing bug) need different fixes, and a
        # single generic line made the distinction cost hours of archaeology.
        skip_reasons: list[PoolSkip] = []
        for entry in self._entries:
            # Borrowed credentials persist as metadata-only references and are
            # hydrated from their live source on load; never lease an
            # unhydrated duplicate as an empty key.
            if entry.auth_type == AUTH_TYPE_API_KEY and not entry.runtime_api_key:
                skip_reasons.append(
                    (POOL_SKIP_REASON_SECRET_NOT_HYDRATED, entry.id, entry.source)
                )
                continue
            # A row that cannot be a credential for this provider is skipped, not selected: a
            # store written before the write-time check existed (or by a tool that never had it)
            # must not be able to put a 401 in front of a healthy sibling. The write-time refusal
            # (``add_entry``) is the first line of defence; this is the one that protects a pool
            # that was already on disk when the incident of 2026-11-10 was diagnosed.
            if manual_pool_credential_defect(self.provider, entry, reference_length=reference_length) is not None:
                continue
            synced = self._resync_stale_entry(entry)
            if synced is not entry:
                entry = synced
                cleared_any = True
            if entry.last_status == STATUS_DEAD:
                # Manual DEAD credentials are pruned after a 24h quiet window;
                # singleton-seeded ones stay (audit trail, and the seeder would
                # re-create them anyway). DEAD never re-enters via TTL — only a
                # write-side re-auth sync clears it.
                if _is_manual_source(entry.source):
                    dead_at = entry.last_status_at or 0
                    if dead_at and now - dead_at > DEAD_MANUAL_PRUNE_TTL_SECONDS:
                        _pool_logger().warning(
                            "credential pool: pruning DEAD manual entry %s "
                            "(reason=%s, age=%.1fh) — re-add via `hermes auth add %s`",
                            entry.label or entry.id[:8],
                            entry.last_error_reason or "unknown",
                            (now - dead_at) / 3600.0,
                            self.provider,
                        )
                        entries_to_prune.append(entry.id)  # can't mutate while iterating
                        cleared_any = True
                skip_reasons.append((POOL_SKIP_REASON_DEAD, entry.id, entry.source))
                continue
            if model_cooldown_until(entry, model) is not None:
                skip_reasons.append((POOL_SKIP_REASON_MODEL_COOLDOWN, entry.id, entry.source))
                continue
            if entry.last_status == STATUS_EXHAUSTED:
                exhausted_until = cooldown_end_for(entry, sole_credential=sole_credential)
                # Codex quota windows can reopen EARLY; a throttled live probe
                # lifts a stale cooldown (issue #43747).
                if (
                    exhausted_until is not None
                    and now < exhausted_until
                    and not (clear_expired and self._codex_quota_restored_upstream(entry))
                ):
                    skip_reasons.append(
                        (
                            POOL_SKIP_REASON_EXHAUSTED,
                            entry.id,
                            # The remaining cooldown, in whole seconds. This is
                            # the "when can I rotate again" number the log
                            # never carried.
                            str(max(0, round(exhausted_until - now))),
                        )
                    )
                    continue
                if clear_expired:
                    # The probe may have rotated this row's single-use token pair: clear the
                    # cooldown on the live row, not on this pre-probe copy.
                    entry = self._find(lambda e, i=entry.id: e.id == i) or entry
                    entry = self._adopt(entry, persist=False, **_MARK_OK)
                    cleared_any = True
            if refresh and self._entry_needs_refresh(entry):
                if self.provider in _TOKENS_SINGLETON_PROVIDERS:
                    pending_refresh.append(entry)
                    skip_reasons.append((POOL_SKIP_REASON_PENDING_REFRESH, entry.id, entry.source))
                    continue
                refreshed = self._refresh_entry(entry, force=False)
                if refreshed is None:
                    skip_reasons.append((POOL_SKIP_REASON_REFRESH_FAILED, entry.id, entry.source))
                    continue
                entry = refreshed
            if entry.auth_type == AUTH_TYPE_OAUTH and not (entry.access_token or "").strip():
                # A borrowed OAuth row that failed to hydrate (or a sanitized
                # row read straight off disk); leasing it would send an empty
                # bearer. The API-key guard above does not cover it.
                skip_reasons.append((POOL_SKIP_REASON_OAUTH_NOT_HYDRATED, entry.id, entry.source))
                continue
            available.append(entry)
        if entries_to_prune:
            pruned_ids = set(entries_to_prune)
            self._entries = [e for e in self._entries if e.id not in pruned_ids]
        if cleared_any:
            self._persist(removed_ids=entries_to_prune)
        # Stash the skip reasons for the empty-pool log. Best-effort: the
        # selection result is unchanged either way.
        self._last_skip_reasons = skip_reasons
        return available, pending_refresh

    def _log_no_available_entries(self) -> None:
        """Emit the empty-pool INFO line at most once per throttle window.

        The line names *why* the pool was empty. "all exhausted or empty"
        conflated two situations that need different fixes: a credential
        benched by a rate limit (recoverable on a known cooldown) and a
        credential whose secret never hydrated (the pool row exists but
        carries no usable key — the signature of a profile-scoping /
        multiplexing bug). Only the reason, the short entry id, the source
        label and the remaining TTL are logged; never key material.
        """
        from agent.credential_pool import NO_AVAILABLE_ENTRIES_LOG_THROTTLE_SECONDS

        now = time.monotonic()
        last = self._last_no_entries_log_at
        if last is not None and (now - last) < NO_AVAILABLE_ENTRIES_LOG_THROTTLE_SECONDS:
            return
        self._last_no_entries_log_at = now
        _pool_logger().info("credential pool: no available entries (%s)", self._describe_empty_pool())

    def _describe_empty_pool(self) -> str:
        """Summarize why the pool was empty, without leaking secret material.

        Only the reason, the short entry id, the source label and the
        remaining TTL are rendered.
        """
        return describe_skips(self._last_skip_reasons)
