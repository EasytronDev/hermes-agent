"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": [],
             # "status" overrides the HTTP status of a given endpoint family; "body" the
             # JSON payload GitHub would return with it.
             "status": {}, "body": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            # A scripted refusal for this endpoint family (e.g. the private-repo
            # free-plan 403 on /rules/branches) short-circuits the normal body.
            family = ("rules" if "/rules/branches/" in self.path else
                      "check-runs" if "/check-runs" in self.path else
                      "statuses" if "/statuses" in self.path else
                      "pulls" if "/pulls/" in self.path else
                      "graphql" if self.path == "/graphql" else "other")
            refusal = state["status"].get(family)
            if refusal:
                code = refusal
                message = state["body"].get(family, {"rules": "Upgrade to GitHub Pro or make this repository public to enable this feature."}.get(family, "Not Found"))
                payload = json.dumps({"message": message, "status": str(code)}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": state.get("pr_state", "OPEN"),
                    "baseRef": {"branchProtectionRule": state.get("protection", {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": 1}}]})}}}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"},
                         "state": state.get("pull_state", "open"),
                         "merged": state.get("pull_merged", False)}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real `gh api` exits non-zero and prints "gh: <message> (HTTP <code>)" on stderr
    # for a refused read; the gate parses that status code out of stderr.
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request,urllib.error,json\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "try:\n"
                  "    print(urllib.request.urlopen(u).read().decode())\n"
                  "except urllib.error.HTTPError as e:\n"
                  "    detail=json.loads(e.read().decode())\n"
                  "    print('gh: '+detail.get('message','error')+' (HTTP %d)'%e.code, file=sys.stderr)\n"
                  "    sys.exit(1)\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the repository — not a retryable `infra` failure."""
    from pathlib import Path

    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert receipts[-1]["classification"] == "auth"
        assert "acme/repo" in receipts[-1]["detail"]
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "b-token"
    assert captured.get("GH_CONFIG_DIR") != "/nonexistent/launch/gh"
    assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                  "if 'GH_CONFIG_DIR' in os.environ and not os.path.exists(os.environ['GH_CONFIG_DIR']):\n"
                  "    sys.exit(4)\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "no login" in receipt["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GH_CONFIG_DIR"] == str(assignee_home / "gh")
    assert "GH_TOKEN" not in captured and "GITHUB_TOKEN" not in captured


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]


# --- private repository on the free plan: the rulesets read is plan-gated ---

PLAN_403 = "Upgrade to GitHub Pro or make this repository public to enable this feature."


def _receipt(conn, tid):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance' ORDER BY id DESC",
        (tid,)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.platforms("linux")
def test_free_plan_private_repo_is_not_an_auth_failure(github):
    """The private-repo free-plan 403 on /rules/branches is a PLAN limitation, not
    an identity failure: the gate must read the branch-protection rule GraphQL
    already returned instead of classifying the card `auth` (unfixable by the
    operator, retry-hostile, and blocking every code card with a PR contract).

    The login demonstrably read the repository — the PR came back over GraphQL —
    and GitHub answers 404 (not 403) for a repository the login cannot see, so a
    403 here can only be the paid-plan gate."""
    github["status"]["rules"] = 403
    github["body"]["rules"] = PLAN_403
    # The real repository carries no branch protection rule at all.
    github["protection"] = None
    with connect() as conn:
        tid = kb.create_task(conn, title="free-plan-private", completion_contract="acme/repo")
        kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = _receipt(conn, tid)
    assert receipt["classification"] != "auth", receipt["detail"]
    # Zero required checks: the gate reports "missing", NOT "auth" — the card is
    # no longer stuck on an unfixable credential error.
    assert receipt["classification"] == "missing"
    assert receipt["ok"] is False
    assert "rulesets_unavailable" in receipt
    assert "403" in receipt["rulesets_unavailable"]
    # The refusal is recorded as a plan limitation, never as a credential hint.
    assert "credential" not in receipt["detail"].lower()


@pytest.mark.platforms("linux")
def test_free_plan_repo_still_enforces_the_branch_protection_required_checks(github):
    """Degrading the rulesets read must NOT make the gate permissive: a required
    check from the branch protection rule that did not pass still refuses the
    completion, and a passing one still accepts it."""
    github["status"]["rules"] = 403
    github["body"]["rules"] = PLAN_403
    with connect() as conn:
        for conclusion, accepted in (("success", True), ("failure", False), ("pending", False)):
            github["conclusion"] = conclusion
            tid = kb.create_task(conn, title=f"free-plan-{conclusion}", completion_contract="acme/repo")
            assert kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"}) is accepted
            receipt = _receipt(conn, tid)
            assert receipt["classification"] == ("success" if accepted else conclusion)
            assert receipt["required"] == [{"context": "required", "app_id": 1}]
            assert receipt["checks"][0]["name"] == "required"


@pytest.mark.platforms("linux")
def test_rulesets_read_401_and_404_still_raise_auth(github):
    """Only the paid-plan 403 degrades. A 401/404 on the rulesets read is a real
    access failure and must keep raising `auth` — the gate may not swallow it."""
    for code in (401, 404):
        github["status"]["rules"] = code
        github["body"]["rules"] = "Not Found" if code == 404 else "Bad credentials"
        with connect() as conn:
            tid = kb.create_task(conn, title=f"denied-{code}", completion_contract="acme/repo")
            kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            receipt = _receipt(conn, tid)
        assert receipt["classification"] == "auth", (code, receipt)
        assert "rulesets_unavailable" not in receipt


@pytest.mark.platforms("linux")
def test_a_403_outside_the_rulesets_read_still_raises_auth(github):
    """The degradation is scoped to the rulesets endpoint: a 403 from any other
    read (here the check-runs read) is still an access failure and must keep
    raising `auth`. The gate may not generalise "403 means plan limit"."""
    github["status"]["rules"] = 403
    github["body"]["rules"] = PLAN_403
    github["status"]["check-runs"] = 403
    github["body"]["check-runs"] = "Resource not accessible by personal access token"
    with connect() as conn:
        tid = kb.create_task(conn, title="check-runs-denied", completion_contract="acme/repo")
        kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = _receipt(conn, tid)
    # The rulesets 403 was degraded (and recorded), but the check-runs 403 was
    # NOT: the card still fails as an identity problem.
    assert receipt["classification"] == "auth", receipt
    assert "rulesets_unavailable" in receipt


@pytest.mark.platforms("linux")
def test_merged_pr_is_accepted_under_the_free_plan(github):
    """A MERGED PR is accepted: the gate accepts `pr.state in {OPEN, MERGED}`, and
    the free-plan 403 on the rulesets read must not disturb that."""
    github["status"]["rules"] = 403
    github["body"]["rules"] = PLAN_403
    github["pr_state"] = "MERGED"
    github["pull_state"] = "closed"
    github["pull_merged"] = True
    with connect() as conn:
        tid = kb.create_task(conn, title="merged-free-plan", completion_contract="acme/repo")
        assert kb.complete_task(conn, tid, result="done",
                                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = _receipt(conn, tid)
    assert receipt["classification"] == "success", receipt
    assert receipt["ok"] is True
    assert kb.get_task(conn, tid).status == "done"


@pytest.mark.platforms("linux")
def test_free_plan_repo_with_no_required_checks_is_reported_not_accepted(github):
    """The real repository has no branch protection rule and no rulesets, so there
    is genuinely nothing to require. The gate must say so plainly instead of
    raising an auth error — a card that declares a PR contract on a repository
    with zero required checks is reported as `missing` with the reason."""
    github["status"]["rules"] = 403
    github["body"]["rules"] = PLAN_403
    github["protection"] = None
    with connect() as conn:
        tid = kb.create_task(conn, title="no-required", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = _receipt(conn, tid)
        assert kb.get_task(conn, tid).status != "done"
    assert receipt["classification"] == "missing"
    assert receipt["required"] == []
    assert "No repository-required checks" in receipt["detail"]
