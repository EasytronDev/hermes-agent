"""Pull-request state for the kanban respawn guard — GitHub is the authority
on whether a PR is still open.

The respawn guard (``kanban_db_dispatch.check_respawn_guard``) holds a card
when a recent comment carries a GitHub PR URL: the intent is "a prior worker
already opened a PR, don't re-spawn and duplicate it". The original
implementation could not tell an OPEN PR from a MERGED one — it only saw "a
PR URL was mentioned recently" — so a PR that had already been merged parked
the next lane (typically QA) for the whole 24-hour window, even though the
work the PR carried was long finished.

This module answers the only question the guard needs: **is this PR still
open?** It asks api.github.com, because GitHub is the authority on PR state;
the Kanban DB is not (a merge happens on GitHub, not in the board).

Design constraints (all deliberate):

* **The token is optional.** With no credential the answer is
  :data:`PRState.UNKNOWN` and the caller keeps the current behaviour (hold
  the card). Holding is the fail-safe: the worst outcome of a wrong answer
  is a duplicate worker, not a silently skipped card.
* **A failed lookup is never "closed".** A 404 (private repo seen
  anonymously), a 403 (rate limit), a timeout or a malformed payload all
  resolve to :data:`PRState.UNKNOWN`. Only an explicit ``state``/``merged``
  from the API can release the guard — the guard may only be LIFTED on
  positive evidence, never on absence of evidence.
* **One request per PR per cache window.** The dispatcher evaluates every
  ready card on every tick (60s by default), so an uncached lookup would
  burn the API budget and rate-limit the whole fleet. Results are cached
  in-process for :data:`CACHE_TTL_SECONDS` (5 minutes) — long enough to
  cover many ticks, short enough that a fresh merge is noticed quickly.
* **The URL is the only input.** The guard already knows the PR URL; this
  module parses ``owner/repo/number`` out of it and queries that PR.

The module is import-light and side-effect-free at import time: no network
call happens until :func:`pull_request_state` is called.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# A PR URL as it appears in a Kanban comment. Deliberately permissive about
# the host's case and about a trailing path/query (GitHub appends
# ``/files`` or ``?w=1`` to the PR URL when a user copies it from the
# browser), but strict about the shape: owner, repo, ``/pull/``, number.
PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)",
    re.IGNORECASE,
)

_API_BASE = "https://api.github.com"

# How long a PR's state stays valid in-process. The dispatcher's tick is
# 60s by default, so a 5-minute window collapses ~5 ticks into 1 request.
CACHE_TTL_SECONDS = 300

# The API call is on the dispatcher's critical path (it gates a spawn), so
# it must not hang the tick. 10s is the same budget the update check uses.
_TIMEOUT_SECONDS = 10


class PRState:
    """The three answers the guard can act on."""

    OPEN = "open"
    """The PR is still open — the guard MUST hold the card."""

    CLOSED = "closed"
    """The PR is closed (merged or closed without merging) — the work it
    carried is over, so the guard MUST release the card."""

    UNKNOWN = "unknown"
    """No trustworthy answer (no token, private repo, rate limit, network
    error). The caller MUST keep holding — fail closed."""


@dataclass(frozen=True)
class PullRequestRef:
    """The identity of a PR, parsed from its URL."""

    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def api_url(self) -> str:
        return f"{_API_BASE}/repos/{self.owner}/{self.repo}/pulls/{self.number}"


def parse_pr_url(text: Optional[str]) -> Optional[PullRequestRef]:
    """The first PR reference in ``text``, or None.

    A URL that carries a query/fragment (``.../pull/646/files``,
    ``.../pull/646?w=1``) is accepted — the regex stops at the number.
    """
    if not text:
        return None
    match = PR_URL_RE.search(text)
    if not match:
        return None
    try:
        number = int(match.group("number"))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return PullRequestRef(
        owner=match.group("owner"),
        repo=match.group("repo"),
        number=number,
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
# ``slug -> (state, fetched_at)``. Entries are never evicted by age here;
# the TTL check happens on read and a stale entry is simply re-fetched.
_state_cache: dict[str, tuple[str, float]] = {}


def _cache_get(slug: str, now: float) -> Optional[str]:
    with _cache_lock:
        entry = _state_cache.get(slug)
        if entry is None:
            return None
        state, fetched_at = entry
        if (now - fetched_at) >= CACHE_TTL_SECONDS:
            return None
        return state


def _cache_put(slug: str, state: str, now: float) -> None:
    with _cache_lock:
        _state_cache[slug] = (state, now)


def clear_cache() -> None:
    """Drop every cached PR state. Test seam."""
    with _cache_lock:
        _state_cache.clear()


# ---------------------------------------------------------------------------
# The lookup
# ---------------------------------------------------------------------------


def _token() -> Optional[str]:
    """The GitHub credential, or None when there is none.

    Never raises: a multiplexed gateway can have no scope installed for a
    dispatcher tick, and the guard must degrade to "hold", not blow up the
    dispatch.
    """
    try:
        from hermes_cli.github_api import github_token

        return github_token()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("GitHub token lookup failed for the PR guard: %s", exc)
        return None


def _state_from_payload(payload: object) -> str:
    """Map a GitHub PR payload onto a :class:`PRState`.

    ``merged`` is the decisive field: GitHub keeps it True only for a PR
    that was actually merged (``closed_at`` alone also covers a PR closed
    without merging). A payload that carries neither field is not a PR
    answer — it is UNKNOWN, never "closed".
    """
    if not isinstance(payload, dict):
        return PRState.UNKNOWN
    merged = payload.get("merged")
    state = payload.get("state")
    if merged is True:
        return PRState.CLOSED
    if isinstance(state, str) and state.lower() == "closed":
        return PRState.CLOSED
    if isinstance(state, str) and state.lower() == "open":
        return PRState.OPEN
    return PRState.UNKNOWN


def _fetch_state(ref: PullRequestRef, token: Optional[str], now: float) -> str:
    """One API call. Returns a :class:`PRState` value — never raises."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "hermes-kanban-pr-guard",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(ref.api_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read(512 * 1024).decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        # 404 = the repo is private and we are anonymous (or the PR does
        # not exist). 403/429 = the rate limit. Both are "no answer", NOT
        # "the PR is closed" — the guard keeps holding.
        logger.debug(
            "PR state lookup for %s failed with HTTP %s", ref.slug, exc.code
        )
        return PRState.UNKNOWN
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.debug("PR state lookup for %s failed: %s", ref.slug, exc)
        return PRState.UNKNOWN
    state = _state_from_payload(payload)
    if state != PRState.UNKNOWN:
        _cache_put(ref.slug, state, now)
    return state


def pull_request_state(
    ref: PullRequestRef | str,
    *,
    token: Optional[str] = None,
    now: Optional[float] = None,
    use_cache: bool = True,
) -> str:
    """The state of a pull request: :data:`PRState.OPEN`, ``CLOSED`` or
    ``UNKNOWN``.

    ``ref`` is a :class:`PullRequestRef` or a PR URL string. ``token``
    overrides the credential lookup (test seam); ``None`` means "resolve it
    the normal way". ``use_cache=False`` forces a fresh call.
    """
    if isinstance(ref, str):
        parsed = parse_pr_url(ref)
        if parsed is None:
            return PRState.UNKNOWN
        ref = parsed
    now = time.time() if now is None else now
    if use_cache:
        cached = _cache_get(ref.slug, now)
        if cached is not None:
            return cached
    if token is None:
        token = _token()
    state = _fetch_state(ref, token, now)
    # An UNKNOWN answer is NOT cached: the next tick must be free to retry
    # (the token may have appeared, the rate limit may have reset).
    return state
