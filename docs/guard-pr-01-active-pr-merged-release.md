# GUARD-PR-01 — the `active_pr` respawn guard must not hold a card whose PR is already MERGED

## The defect (measured)

`hermes_cli.kanban_db_dispatch.py::check_respawn_guard` holds a ready card
when a comment from the last 24h carries a GitHub PR URL. The intent is the
duplicate-PR protection of #111910: a prior worker already opened a PR, so
re-spawning it risks a second PR for the same work.

The implementation could not tell an OPEN PR from a MERGED one — it only saw
"a PR URL was mentioned recently". A PR that had already been merged
therefore parked the next lane for the whole window.

Measured on the `claro-qrf` board (`hermes kanban diagnostics`, 2026-11-06
~03:00 UTC):

```
t_18be6156  ready  @qa  [critical] stranded_in_ready: Ready for 12.8h with no worker
t_a81ce43d  ready  @qa  [critical] stranded_in_ready: Ready for 9.2h with no worker
```

Both were held by `check_respawn_guard` → `active_pr`, triggered by the PR
URL in the frontend's comment:

| card | comment | PR | GitHub's actual state |
|---|---|---|---|
| `t_18be6156` | #1812 | https://github.com/EasytronDev/vale-telco-monorepo/pull/646 | `merged: true` |
| `t_a81ce43d` | #1856 | https://github.com/EasytronDev/vale-telco-monorepo/pull/660 | `merged: true` |

The QA lane was verifying work that had already landed. The card the QA was
meant to check had been waiting 12.8 hours because of a PR that GitHub had
already merged.

## The fix

GitHub is the authority on PR state; the board is not (a merge happens on
GitHub). The guard now asks GitHub whether the PR it found is still open.

**New module: `hermes_cli/kanban_pr_state.py`**

* `parse_pr_url()` — owner/repo/number out of a PR URL (accepts a trailing
  `/files`, `?w=1` or a `#fragment`, which is how a URL copied from the
  browser looks).
* `pull_request_state()` — one API call to `api.github.com/repos/{owner}/{repo}/pulls/{n}`,
  answered as `PRState.OPEN` / `CLOSED` / `UNKNOWN`.
* The credential comes from the existing ladder in
  `hermes_cli/github_api.py::github_token()` (`GITHUB_TOKEN` → `GH_TOKEN` →
  `gh auth token` → anonymous).
* Results are cached in-process for `CACHE_TTL_SECONDS = 300` (5 minutes).
  The dispatcher ticks every 60s, so an uncached lookup would spend the API
  budget on every tick of every card.

**Guard change: `hermes_cli/kanban_db_dispatch.py`**

`check_respawn_guard` gains an optional `release_detail` out-parameter. When
the guard would return `active_pr`, it first consults GitHub for the newest
PR comment's PR:

* `CLOSED` (merged or closed) → the guard **releases** the card.
* `OPEN` → the guard **holds** it (unchanged — the duplicate-PR protection).
* `UNKNOWN` → the guard **holds** it (unchanged).

The dispatcher's spawn path writes a `respawn_guard_released` task event with
`{"reason": "pr_finished", "pr": "<owner/repo#number>"}` so a spawn that
follows a released guard is auditable instead of looking like an unexplained
duplicate.

### Fail-closed, by construction

The guard may only be LIFTED on positive evidence from GitHub. Every failure
mode resolves to `UNKNOWN` and keeps the current behaviour:

| situation | answer | guard |
|---|---|---|
| no credential | `UNKNOWN` | holds |
| private repo seen anonymously (HTTP 404) | `UNKNOWN` | holds |
| rate limit (HTTP 403/429) | `UNKNOWN` | holds |
| network error / timeout | `UNKNOWN` | holds |
| malformed payload (no `state`, no `merged`) | `UNKNOWN` | holds |
| `state: "open"` | `OPEN` | holds |
| `state: "closed"` or `merged: true` | `CLOSED` | **releases** |

The asymmetry is deliberate: a wrong "finished" costs a duplicate worker; a
wrong "not finished" costs a card parked for 24 hours. The guard errs toward
holding.

An `UNKNOWN` answer is **not** cached — the next tick must be free to retry
(the token may have appeared, the rate limit may have reset).

## The proof

### TDD: RED → GREEN

Against the pristine `origin/main` of the fork (the guard untouched):

```
RED   (pristine upstream):  5 failed, 14 passed
GREEN (with the patch):    19 passed
```

The 5 that fail in RED are exactly the "a MERGED PR must release the card"
cases. The 14 that pass in RED are the "no regression" cases (an OPEN PR
still holds) and the lookup unit tests — so the suite is not vacuously green.

### Regression: the kanban suite, baseline vs patched

Same command on both trees, `tests/hermes_cli/ -k kanban`:

| | baseline (pristine upstream) | patched |
|---|---|---|
| failed | 76 | 76 |
| passed | 577 | 577 |

Set comparison of the failing test ids:

```
comm -13 BASELINE PATCHED   ->  (empty)   zero new regressions
comm -23 BASELINE PATCHED   ->  (empty)   zero fixed-by-accident
```

The 76 failures are pre-existing environment failures in this sandbox
(worker spawn, `terminal.cwd` pinning, "real root" path assertions) and are
identical on both trees.

Focused suites, patched:

```
tests/hermes_cli/test_kanban_db.py
tests/hermes_cli/test_kanban_review_lifecycle.py
tests/hermes_cli/test_kanban_review_lifecycle_complete.py
tests/hermes_cli/test_kanban_active_pr_merged_release.py
tests/hermes_cli/test_kanban_host_cap.py
tests/hermes_cli/test_kanban_list_undecodable_text.py
    -> 139 passed, 1 skipped

tests/hermes_cli/test_kanban_dispatch_tick_hook.py
tests/hermes_cli/test_kanban_dispatch_unknown_assignee_event.py
tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py
tests/hermes_cli/test_kanban_dispatch_claim_allowlist.py
    -> 16 passed
```

### Live proof on the production board

The real `claro-qrf` board DB, the real GitHub API, the real PRs of the
incident:

```
t_18be6156: status=archived assignee=qa
  comment 1812 @frontend: https://github.com/EasytronDev/vale-telco-monorepo/pull/646
    GitHub says: closed  (ref=EasytronDev/vale-telco-monorepo#646)
    PATCHED  guard verdict: None   detail={'reason': 'pr_finished',
                                            'pr': 'EasytronDev/vale-telco-monorepo#646'}
    PRISTINE guard verdict: 'active_pr'   (the defect)
```

The card that was stranded for 12.8 hours is released by the patched guard,
and the release names the PR that was already merged.

## Operational note: the token

The lookup is only as good as the credential. On this host the dispatcher
runs with `HERMES_HOME=/opt/data` (the root home), whose `.env` has **no**
`GITHUB_TOKEN`; the token lives in the `orchestrator` profile's `.env`. With
no token the API returns 404 for this private repo and the guard correctly
keeps holding (the fail-safe) — the fix is then inert.

To make the fix effective on this deployment, `GITHUB_TOKEN` (or `GH_TOKEN`)
must be present in the environment of the process that runs the dispatcher
(gateway pid 316, `HERMES_HOME=/opt/data`), or `gh auth token` must work for
that user. The card's own recommendation already states this contract: the
token is optional and its absence keeps the current behaviour.

## Scope

* `hermes_cli/kanban_pr_state.py` (new) — the PR-state lookup.
* `hermes_cli/kanban_db_dispatch.py` — the guard consults it; the release is
  audited on the board.
* `tests/hermes_cli/test_kanban_active_pr_merged_release.py` (new) — 19
  tests, no network, no token.

Nothing in the installed `/opt/hermes` was touched: this fix lives in the
upstream fork and needs a human gate before it reaches the running core.
