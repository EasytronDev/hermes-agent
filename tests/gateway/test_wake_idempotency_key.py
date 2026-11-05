"""The kanban wake self-post must not start a second agent turn for one event.

Regression for #146361 (first half). The wake self-post carried no idempotency
identity, so a client-side timeout on a slow turn -- during which the server had
already admitted the POST -- was re-POSTed up to four times, and each re-POST
was a new admission that started another agent turn in the woken session.

Measured on a live gateway (2026-11-04): one ``completed`` event produced six
rows in ``session_admissions`` with a single ``payload_digest``, and the user
received the notification three times.

The fix gives the wake a per-batch idempotency key and posts it to ``/v1/runs``,
whose durable Idempotency-Key store replays the already-admitted run instead of
starting a second turn.

Run: python scripts/run_tests.sh tests/gateway/test_wake_idempotency_key.py
"""

import asyncio

import pytest

from gateway import kanban_watchers_notifier as notifier


class _Adapter:
    """The bits of the api_server adapter the wake path reads."""

    def __init__(self, host="127.0.0.1", port=1, key="k"):
        self._host = host
        self._port = port
        self._api_key = key
        self._model_name = "hermes-agent"


def _sub(**over):
    sub = {"task_id": "t_x", "platform": "api_server", "chat_id": "api_server:webui",
           "thread_id": "", "delivery_mode": "notify+wake", "notifier_profile": "default"}
    sub.update(over)
    return sub


# --- the key derivation ---------------------------------------------------


def test_key_is_stable_for_one_batch():
    """A retry of the same claimed batch must derive the same key."""
    sub = _sub()
    first = notifier._wake_idempotency_key(sub, 43293)
    again = notifier._wake_idempotency_key(dict(sub), 43293)
    assert first == again


def test_key_differs_per_cursor():
    """A later claimed batch must not reuse the earlier batch's key."""
    sub = _sub()
    assert notifier._wake_idempotency_key(sub, 43293) != notifier._wake_idempotency_key(sub, 43294)


def test_key_differs_per_task_and_destination():
    """Two subscriptions must never share a key."""
    base = _sub()
    assert notifier._wake_idempotency_key(base, 1) != notifier._wake_idempotency_key(_sub(task_id="t_y"), 1)
    assert notifier._wake_idempotency_key(base, 1) != notifier._wake_idempotency_key(_sub(chat_id="webui"), 1)
    assert notifier._wake_idempotency_key(base, 1) != notifier._wake_idempotency_key(_sub(platform="slack"), 1)


def test_key_satisfies_the_server_validation():
    """The API server rejects a key outside 1-255 visible ASCII characters."""
    key = notifier._wake_idempotency_key(_sub(), 43293)
    assert 1 <= len(key) <= 255
    assert all(33 <= ord(ch) <= 126 for ch in key)


# --- the keyed request ----------------------------------------------------


def test_keyed_wake_posts_to_runs_with_the_key(monkeypatch):
    """A keyed wake goes to /v1/runs with the Idempotency-Key header."""
    from gateway import wake as wake_mod

    seen = {}

    class _FakeResp:
        status = 202

        async def read(self):
            return b"{}"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        def post(self, url, json=None, headers=None):
            seen["url"] = url
            seen["json"] = json
            seen["headers"] = headers
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeClientSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *exc):
            return False

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))

    asyncio.run(wake_mod._self_post_chat_completion(
        _Adapter(host="127.0.0.1", port=8642, key="secret"),
        text="[kanban] Task t_x completed.", session_id="api_server:webui",
        idempotency_key="kanban-wake:abc123"))

    assert seen["url"].endswith("/v1/runs")
    assert seen["headers"]["Idempotency-Key"] == "kanban-wake:abc123"
    # /v1/runs takes the turn text as "input", not as a chat-completions message list.
    assert seen["json"]["input"] == "[kanban] Task t_x completed."
    assert seen["json"]["session_id"] == "api_server:webui"


def test_unkeyed_wake_also_acks_at_admission(monkeypatch):
    """A wake without a key must still use the admitting route.

    ACK-on-admission is the fix for the DELAY (a self-post on
    ``/v1/chat/completions`` holds the notifier's serial delivery loop for the
    whole agent turn), and it does not depend on the key: the key buys replay
    protection for a caller with a stable batch identity, but the watch-pattern
    notification path (``gateway/run_notifications.py``) has no such identity
    and was therefore stuck on the blocking route.
    """
    from gateway import wake as wake_mod

    seen = {}

    class _FakeResp:
        status = 202

        async def read(self):
            return b"{}"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        def post(self, url, json=None, headers=None):
            seen["url"] = url
            seen["json"] = json
            seen["headers"] = headers
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeClientSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *exc):
            return False

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))

    asyncio.run(wake_mod._self_post_chat_completion(
        _Adapter(host="127.0.0.1", port=8642, key="secret"),
        text="[kanban] Task t_x completed.", session_id="api_server:webui"))

    assert seen["url"].endswith("/v1/runs")
    assert "Idempotency-Key" not in seen["headers"]
    assert seen["json"]["input"] == "[kanban] Task t_x completed."
    assert seen["json"]["session_id"] == "api_server:webui"


# --- the replay -----------------------------------------------------------


@pytest.mark.asyncio
async def test_same_batch_delivered_twice_starts_one_turn():
    """Two deliveries of one batch (what the notifier's rewind does) start ONE turn.

    The server replays the durable Idempotency-Key record instead of admitting a
    second run, so the woken session runs the turn once.
    """
    import aiohttp
    from aiohttp import web

    from gateway import wake as wake_mod

    state = {"turns": 0, "replays": 0}
    admitted = {}

    async def _runs(request):
        key = request.headers.get("Idempotency-Key")
        if key in admitted:
            state["replays"] += 1
            return web.json_response({"run_id": admitted[key], "status": "queued",
                                      "replayed": True},
                                     headers={"Idempotency-Replayed": "true"})
        admitted[key] = "run_1"
        state["turns"] += 1
        return web.json_response({"run_id": admitted[key], "status": "started",
                                  "replayed": False}, status=202)

    app = web.Application()
    app.router.add_post("/v1/runs", _runs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    monkey_timeout = wake_mod.WAKE_TURN_TIMEOUT_SECONDS
    monkey_delays = wake_mod._RETRY_DELAYS_SECONDS
    wake_mod.WAKE_TURN_TIMEOUT_SECONDS = 0.5
    wake_mod._RETRY_DELAYS_SECONDS = (0.01, 0.01, 0.01)
    try:
        for _ in range(2):
            await wake_mod._self_post_chat_completion(
                _Adapter(host="127.0.0.1", port=port, key="secret"),
                text="[kanban] Task t_x completed.", session_id="api_server:webui",
                idempotency_key="kanban-wake:abc123")
    finally:
        wake_mod.WAKE_TURN_TIMEOUT_SECONDS = monkey_timeout
        wake_mod._RETRY_DELAYS_SECONDS = monkey_delays
        await runner.cleanup()

    assert state["turns"] == 1
    assert state["replays"] == 1
