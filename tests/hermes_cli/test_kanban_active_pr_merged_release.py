"""The ``active_pr`` respawn guard must not hold a card whose PR is already
MERGED (GUARD-PR-01).

The guard exists to stop a card from being re-spawned against a PR a prior
worker already opened — the duplicate-PR protection #111910. The defect it
also had: it could not tell an OPEN PR from a MERGED one, so a PR that had
already been merged parked the next lane (QA) for the whole 24-hour window,
even though the work the PR carried was long finished. Measured on the
``claro-qrf`` board: two QA cards sat ``ready`` for 12.8h and 9.2h, each
held by a PR GitHub already reported as ``merged``.

The rules these tests pin down:

* A PR that GitHub reports as MERGED (or closed) RELEASES the card.
* A PR that GitHub reports as OPEN still HOLDS the card — the
  duplicate-PR protection must not regress.
* The answer is only ever taken from GitHub: with no credential, a private
  repo seen anonymously, a rate limit or a network error the guard HOLDS
  (fail-closed). A lookup failure must never be read as "the PR is done".
* The release is auditable: the dispatcher records why it let the card go.

Every test here runs WITHOUT the GitHub API — the lookup is stubbed at the
``kanban_pr_state.pull_request_state`` seam — so the suite is reproducible in
CI (no token, no network).
"""

from __future__ import annotations

import email.message
import json
import urllib.error
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pr_state as prs


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _clear_pr_cache():
    """The PR-state cache is process-global; every test starts empty."""
    prs.clear_cache()
    yield
    prs.clear_cache()


def _stub_pr_state(monkeypatch: pytest.MonkeyPatch, state: str, calls: list | None = None):
    """Force every PR lookup to answer ``state`` (no network)."""
    def _fake(ref, **kwargs):
        if calls is not None:
            calls.append(ref.slug if hasattr(ref, "slug") else str(ref))
        return state

    monkeypatch.setattr(prs, "pull_request_state", _fake)


PR_URL = "https://github.com/example/repo/pull/646"
PR_URL_2 = "https://github.com/example/repo/pull/660"


# ---------------------------------------------------------------------------
# 1. The defect: a MERGED PR must release the card.
# ---------------------------------------------------------------------------


def test_merged_pr_releases_the_guard(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The case of the incident: the PR is MERGED, so the card is NOT held.

    Before the fix this returned ``"active_pr"`` and the QA card stayed
    ``ready`` for the whole window — 12.8h in the measured incident.
    """
    calls: list = []
    _stub_pr_state(monkeypatch, prs.PRState.CLOSED, calls)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="qa of a merged PR", assignee="qa")
        kb.add_comment(conn, tid, author="frontend", body=f"PR aberto: {PR_URL}")

        assert kbd.check_respawn_guard(conn, tid) is None
        assert calls == ["example/repo#646"]


def test_closed_pr_releases_the_guard(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR closed WITHOUT merging also carries no pending work."""
    _stub_pr_state(monkeypatch, prs.PRState.CLOSED)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="qa of a closed PR", assignee="qa")
        kb.add_comment(conn, tid, author="frontend", body=f"PR: {PR_URL}")

        assert kbd.check_respawn_guard(conn, tid) is None


def test_merged_pr_releases_the_card_for_spawn(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through ``dispatch_once``: the card is SPAWNED, not guarded."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})
    _stub_pr_state(monkeypatch, prs.PRState.CLOSED)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="qa of a merged PR", assignee="qa")
        kb.add_comment(conn, tid, author="frontend", body=f"PR: {PR_URL}")

        res = kbd.dispatch_once(conn, dry_run=True)
        assert tid in [s[0] for s in res.spawned]
        assert dict(res.respawn_guarded).get(tid) is None


def test_release_is_auditable_on_the_board(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The release leaves a trail naming the PR that was already finished.

    A spawn that follows a released guard would otherwise look like an
    unexplained duplicate of work someone already did.
    """
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})
    _stub_pr_state(monkeypatch, prs.PRState.CLOSED)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="audited release", assignee="qa")
        kb.add_comment(conn, tid, author="frontend", body=f"PR: {PR_URL}")

        res = kbd.dispatch_once(conn, dry_run=False)
        assert tid in [s[0] for s in res.spawned]

        row = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "AND kind = 'respawn_guard_released' ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["reason"] == "pr_finished"
        assert payload["pr"] == "example/repo#646"


# ---------------------------------------------------------------------------
# 2. No regression: an OPEN PR must still hold the card.
# ---------------------------------------------------------------------------


def test_open_pr_still_holds_the_guard(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The duplicate-PR protection must survive the fix.

    An OPEN PR means a prior worker already opened it — re-spawning risks a
    second PR for the same work. This is what the guard was built for.
    """
    _stub_pr_state(monkeypatch, prs.PRState.OPEN)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="dev own pr", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR_URL} for review.")

        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_open_pr_still_defers_the_spawn(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: an OPEN PR keeps the card out of the spawn set."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})
    _stub_pr_state(monkeypatch, prs.PRState.OPEN)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="dev own pr", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR_URL} for review.")

        res = kbd.dispatch_once(conn, dry_run=True)
        assert tid not in [s[0] for s in res.spawned]
        assert dict(res.respawn_guarded).get(tid) == "active_pr"


def test_handoff_still_lifts_the_guard_regardless_of_pr_state(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #111910 handoff exception is untouched by the PR-state lookup."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})
    _stub_pr_state(monkeypatch, prs.PRState.OPEN)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="closer recovery", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR_URL}")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_comments SET created_at = created_at - 60 WHERE task_id = ?",
                (tid,),
            )
        assert kb.assign_task(conn, tid, "closer") is True

        assert kbd.check_respawn_guard(conn, tid) is None


# ---------------------------------------------------------------------------
# 3. Fail-closed: a lookup that fails must NEVER read as "the PR is done".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [prs.PRState.UNKNOWN],
)
def test_unknown_pr_state_still_holds_the_guard(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    """No trustworthy answer => the card stays held (the fail-safe)."""
    _stub_pr_state(monkeypatch, state)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unknown pr", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR_URL}")

        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_missing_credential_keeps_the_guard(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no token the lookup cannot answer — the guard must hold.

    This is the behaviour a CI run (no GitHub token) sees: the fix must not
    change the outcome for an installation that cannot reach the API.
    """
    monkeypatch.setattr(prs, "_token", lambda: None)

    def _fail(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", email.message.Message(), None,
        )

    monkeypatch.setattr(prs.urllib.request, "urlopen", _fail)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="no token", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"Opened {PR_URL}")

        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


# ---------------------------------------------------------------------------
# 4. The lookup itself (no network, no token).
# ---------------------------------------------------------------------------


def test_parse_pr_url_reads_owner_repo_number() -> None:
    ref = prs.parse_pr_url(f"see {PR_URL} for the diff")
    assert ref is not None
    assert (ref.owner, ref.repo, ref.number) == ("example", "repo", 646)
    assert ref.slug == "example/repo#646"


def test_parse_pr_url_accepts_a_trailing_path_or_query() -> None:
    """GitHub appends /files or ?w=1 when a user copies a PR URL."""
    assert prs.parse_pr_url("https://github.com/o/r/pull/12/files") is not None
    assert prs.parse_pr_url("https://github.com/o/r/pull/12?w=1") is not None
    assert prs.parse_pr_url("https://github.com/o/r/pull/12#issuecomment-1") is not None


def test_parse_pr_url_rejects_a_non_pull_url() -> None:
    assert prs.parse_pr_url("https://github.com/example/repo/issues/5") is None
    assert prs.parse_pr_url("https://github.com/example/repo") is None
    assert prs.parse_pr_url("") is None
    assert prs.parse_pr_url(None) is None


def test_payload_mapping_never_invents_a_closed_state() -> None:
    """Only an explicit ``merged``/``state`` may say CLOSED."""
    assert prs._state_from_payload({"merged": True, "state": "closed"}) == prs.PRState.CLOSED
    assert prs._state_from_payload({"merged": False, "state": "closed"}) == prs.PRState.CLOSED
    assert prs._state_from_payload({"merged": False, "state": "open"}) == prs.PRState.OPEN
    # A payload with neither field is no answer, never "closed".
    assert prs._state_from_payload({}) == prs.PRState.UNKNOWN
    assert prs._state_from_payload({"draft": True}) == prs.PRState.UNKNOWN
    assert prs._state_from_payload("not a dict") == prs.PRState.UNKNOWN
    assert prs._state_from_payload(None) == prs.PRState.UNKNOWN


def test_http_error_is_unknown_not_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 (private repo, anonymous) must NOT read as 'the PR is closed'."""
    def _fail(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(prs.urllib.request, "urlopen", _fail)
    ref = prs.parse_pr_url(PR_URL)
    assert ref is not None
    assert prs.pull_request_state(ref, token="x", use_cache=False) == prs.PRState.UNKNOWN


def test_network_error_is_unknown_not_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(prs.urllib.request, "urlopen", _fail)
    ref = prs.parse_pr_url(PR_URL)
    assert ref is not None
    assert prs.pull_request_state(ref, token="x", use_cache=False) == prs.PRState.UNKNOWN


def test_successful_lookup_caches_within_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatcher ticks every 60s — an uncached lookup would burn the API."""
    calls: list = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *_a, **_k):
            return json.dumps({"state": "closed", "merged": True}).encode()

    def _ok(request, timeout=None):
        calls.append(request.full_url)
        return _Resp()

    monkeypatch.setattr(prs.urllib.request, "urlopen", _ok)
    ref = prs.parse_pr_url(PR_URL)
    assert ref is not None

    assert prs.pull_request_state(ref, token="t") == prs.PRState.CLOSED
    assert prs.pull_request_state(ref, token="t") == prs.PRState.CLOSED
    assert len(calls) == 1  # the second read came from the cache


def test_cache_expires_after_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *_a, **_k):
            return json.dumps({"state": "open", "merged": False}).encode()

    def _ok(request, timeout=None):
        calls.append(request.full_url)
        return _Resp()

    monkeypatch.setattr(prs.urllib.request, "urlopen", _ok)
    ref = prs.parse_pr_url(PR_URL)
    assert ref is not None

    now = 1000.0
    assert prs.pull_request_state(ref, token="t", now=now) == prs.PRState.OPEN
    assert prs.pull_request_state(
        ref, token="t", now=now + prs.CACHE_TTL_SECONDS - 1,
    ) == prs.PRState.OPEN
    assert len(calls) == 1
    # Past the TTL the state is re-read — a fresh merge is noticed.
    assert prs.pull_request_state(
        ref, token="t", now=now + prs.CACHE_TTL_SECONDS + 1,
    ) == prs.PRState.OPEN
    assert len(calls) == 2


def test_an_unknown_answer_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed lookup must be retried on the next tick, not remembered."""
    attempts = {"n": 0}

    def _flaky(request, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.HTTPError(request.full_url, 403, "rate limited", {}, None)
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *_a, **_k):
                return json.dumps({"state": "closed", "merged": True}).encode()
        return _Resp()

    monkeypatch.setattr(prs.urllib.request, "urlopen", _flaky)
    ref = prs.parse_pr_url(PR_URL)
    assert ref is not None

    assert prs.pull_request_state(ref, token="t") == prs.PRState.UNKNOWN
    assert prs.pull_request_state(ref, token="t") == prs.PRState.CLOSED
    assert attempts["n"] == 2


def test_the_newest_pr_comment_decides(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the newest PR comment's PR is consulted."""
    calls: list = []
    _stub_pr_state(monkeypatch, prs.PRState.CLOSED, calls)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="two prs", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=f"older PR {PR_URL}")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_comments SET created_at = created_at - 60 WHERE task_id = ?",
                (tid,),
            )
        kb.add_comment(conn, tid, author="dev", body=f"newer PR {PR_URL_2}")

        assert kbd.check_respawn_guard(conn, tid) is None
        assert calls == ["example/repo#660"]
