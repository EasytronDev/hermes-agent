"""The wake self-post must be ACKed AT ADMISSION, not at the end of the agent turn.

Regression for #146361 (second half). The first half (the idempotency key, PR #7)
stopped ONE event from waking the session N times. It did not stop the DELAY:
the self-post posted to ``/v1/chat/completions``, which holds the request open
for the WHOLE agent turn, and the Kanban notifier delivers ONE card at a time
(``gateway/kanban_watchers.py``). A self-post that waited out its 600 s budget
therefore held the WHOLE SESSION QUEUE, and every other card's legitimate
notification waited behind it.

Measured on a live gateway (2026-11-04): 8 consecutive 600 s self-post timeouts
held the queue for 70.8 min (20:41:04 -> 21:51:50), and a legitimate
``completed`` notification waited **82.2 min** for its agent to wake.

The fix posts the wake to ``/v1/runs``, which ADMITS the turn and answers 202 at
once: the wake counts as delivered when the admission exists, so the delivery no
longer rides the agent turn's duration. The ``Idempotency-Key`` stays OPTIONAL --
it buys replay protection for a caller with a stable batch identity, but ACK-on-
admission must not depend on it (the watch-pattern notification path has no key
and was therefore stuck on the blocking route).

Run: python scripts/run_tests.sh tests/gateway/test_wake_ack_on_admission.py
"""

import asyncio
import time

import pytest

from gateway import wake as wake_mod


class _Adapter:
    """The bits of the api_server adapter the wake path reads."""

    def __init__(self, host="127.0.0.1", port=1, key="k"):
        self._host = host
        self._port = port
        self._api_key = key
        self._model_name = "hermes-agent"


def _fake_client_session(monkeypatch, seen, status=202):
    """Patch aiohttp.ClientSession with a recorder that answers ``status``."""
    import aiohttp

    class _FakeResp:
        def __init__(self):
            self.status = status

        async def read(self):
            return b"{}"

        async def text(self):
            return "{}"

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

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))


# --- the transport choice -------------------------------------------------


def test_unkeyed_wake_acks_at_admission(monkeypatch):
    """A wake WITHOUT an idempotency key must still use the admitting route.

    This is the coverage gap: the watch-pattern notification path
    (``gateway/run_notifications.py``) has no batch identity, so it could not
    supply a key -- and therefore stayed on the route that waits for the turn.
    """
    seen = {}
    _fake_client_session(monkeypatch, seen)

    asyncio.run(wake_mod._self_post_chat_completion(
        _Adapter(host="127.0.0.1", port=8642, key="secret"),
        text="[watch] background process finished", session_id="api_server:webui"))

    assert seen["url"].endswith("/v1/runs")
    assert seen["json"]["input"] == "[watch] background process finished"
    assert seen["json"]["session_id"] == "api_server:webui"
    assert "Idempotency-Key" not in seen["headers"]


def test_keyed_wake_still_carries_the_key(monkeypatch):
    """A keyed wake keeps its Idempotency-Key (replay protection is unchanged)."""
    seen = {}
    _fake_client_session(monkeypatch, seen)

    asyncio.run(wake_mod._self_post_chat_completion(
        _Adapter(host="127.0.0.1", port=8642, key="secret"),
        text="[kanban] Task t_x completed.", session_id="api_server:webui",
        idempotency_key="kanban-wake:abc123"))

    assert seen["url"].endswith("/v1/runs")
    assert seen["headers"]["Idempotency-Key"] == "kanban-wake:abc123"


def test_the_blocking_route_is_gone(monkeypatch):
    """No self-post may target the route that waits for the whole turn.

    ``/v1/chat/completions`` holds the request open for the whole turn, which is
    what put the session queue behind the agent turn.
    """
    seen = {}
    _fake_client_session(monkeypatch, seen)

    asyncio.run(wake_mod._self_post_chat_completion(
        _Adapter(host="127.0.0.1", port=8642, key="secret"),
        text="wake", session_id="api_server:webui"))

    assert "/v1/chat/completions" not in seen["url"]


# --- the delay itself -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_slow_card_does_not_delay_another_cards_notification():
    """THE REGRESSION: a slow card must not hold up a different card's wake.

    A card whose agent turn is slow runs FIRST in the notifier's serial delivery
    loop. The card that finishes right after it must still be delivered promptly.

    Before the fix the second delivery waited for the first turn to finish: the
    self-post blocked the notifier's ``for d in deliveries: await ...`` loop for
    the whole turn (up to ``WAKE_TURN_TIMEOUT_SECONDS`` = 600 s), and the second
    card's notification waited behind it -- 82.2 min on the live gateway.

    Both wake shapes are exercised, because they took different routes before the
    fix: the KEYED one (``/v1/runs``) and the UNKEYED one
    (``/v1/chat/completions``, which holds the request open for the whole turn).
    The unkeyed shape is the one the watch-pattern notification path used.
    """
    from aiohttp import web

    state = {"slow_turn_finished": False, "admissions": [], "routes": []}
    admitted = {}
    gate = asyncio.Event()

    async def _runs(request):
        """Mimic /v1/runs: admit the turn, answer 202, run the turn in background."""
        key = request.headers.get("Idempotency-Key")
        body = await request.json()
        state["routes"].append("runs")
        if key and key in admitted:
            return web.json_response({"run_id": admitted[key], "status": "queued",
                                      "replayed": True},
                                     headers={"Idempotency-Replayed": "true"})
        run_id = f"run_{len(state['admissions'])}"
        if key:
            admitted[key] = run_id
        state["admissions"].append({"run_id": run_id, "input": body.get("input")})

        async def _slow_turn():
            # The turn is slow: it would hold a /v1/chat/completions request open
            # for its whole duration.
            await gate.wait()
            state["slow_turn_finished"] = True

        asyncio.create_task(_slow_turn())
        return web.json_response({"run_id": run_id, "status": "started",
                                  "replayed": False}, status=202)

    async def _chat_completions(request):
        """Mimic /v1/chat/completions: hold the request open for the whole turn."""
        state["routes"].append("chat_completions")
        state["admissions"].append({"input": (await request.json())["messages"][0]["content"]})
        await gate.wait()
        state["slow_turn_finished"] = True
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/v1/runs", _runs)
    app.router.add_post("/v1/chat/completions", _chat_completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    old_timeout = wake_mod.WAKE_TURN_TIMEOUT_SECONDS
    old_delays = wake_mod._RETRY_DELAYS_SECONDS
    wake_mod.WAKE_TURN_TIMEOUT_SECONDS = 30.0
    wake_mod._RETRY_DELAYS_SECONDS = (0.01, 0.01, 0.01)
    adapter = _Adapter(host="127.0.0.1", port=port, key="secret")

    # The SLOW card is delivered first (the notifier's loop is serial), for each
    # wake shape. The UNKEYED shape is the one that blocked in production.
    for label, key in (("keyed", "kanban-wake:slow"), ("unkeyed", None)):
        state["slow_turn_finished"] = False
        state["admissions"].clear()
        state["routes"].clear()
        gate = asyncio.Event()
        # Set by the CLIENT when the slow POST returns (at admission, while the agent
        # turn it started keeps running): the test waits for that condition instead
        # of a fixed delay (HX013). It is per-iteration: the second shape must wait
        # for ITS OWN slow POST, not the first shape's.
        slow_posted = asyncio.Event()

        slow = None
        try:
            started = time.monotonic()
            async def _slow_post():
                await wake_mod._self_post_chat_completion(
                    adapter, text=f"[wake] slow card ({label})", session_id="api_server:webui",
                    **({"idempotency_key": f"{key}:slow"} if key else {}))
                # The POST returned: the wake was ADMITTED (202) while the agent turn
                # it started keeps running. That return IS the condition to wait for.
                slow_posted.set()

            slow = asyncio.create_task(_slow_post())

            # A DIFFERENT card finishes while the slow one is still in flight.
            # It is a DIFFERENT event, so it gets its own key: a replayed key
            # would (correctly) resolve to the earlier run instead of a new one.
            await asyncio.wait_for(slow_posted.wait(), timeout=10.0)
            delivered = await wake_mod._self_post_chat_completion(
                adapter, text=f"[wake] fast card ({label})", session_id="api_server:webui",
                **({"idempotency_key": f"{key}:fast"} if key else {}))
            elapsed = time.monotonic() - started
            # Snapshot WHETHER the slow turn was still running AT DELIVERY TIME:
            # that is the whole point, so it must be read before the gate opens.
            slow_still_running = not state["slow_turn_finished"]
        finally:
            gate.set()
            if slow is not None:
                await slow
            await runner.cleanup()
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            adapter = _Adapter(host="127.0.0.1", port=port, key="secret")

        # The fast card's delivery must NOT wait for the slow turn to finish.
        assert slow_still_running, (
            f"the fast card's delivery ({label}) waited for the slow turn -- the "
            "session queue was held behind the agent turn"
        )
        # The budget is the whole-turn one: the delay must not approach it.
        assert elapsed < 5.0, (
            f"the second card's delivery ({label}) took {elapsed:.1f}s -- it is "
            "waiting on the agent turn instead of being ACKed at admission"
        )
        # And the delivery really happened, exactly once each.
        assert delivered is None
        assert [a["input"] for a in state["admissions"]] == [
            f"[wake] slow card ({label})", f"[wake] fast card ({label})",
        ], f"delivery lost or duplicated for the {label} wake: {state['admissions']}"
        assert "chat_completions" not in state["routes"], (
            f"the {label} wake used the route that waits for the whole turn: "
            f"{state['routes']}"
        )

    wake_mod.WAKE_TURN_TIMEOUT_SECONDS = old_timeout
    wake_mod._RETRY_DELAYS_SECONDS = old_delays


@pytest.mark.asyncio
async def test_the_delivery_is_not_lost_when_the_turn_is_still_running():
    """ACK at admission must still DELIVER: one admission per delivered event.

    The fast card's wake is ACKed while its turn is still running, so the
    admission exists and the event is delivered exactly once -- it is not
    swallowed by the "the turn has not finished yet" check.
    """
    from aiohttp import web

    state = {"admissions": [], "turns_started": 0}
    gate = asyncio.Event()

    async def _runs(request):
        body = await request.json()
        state["admissions"].append(body.get("input"))
        state["turns_started"] += 1

        async def _turn():
            await gate.wait()

        asyncio.create_task(_turn())
        return web.json_response({"run_id": "run_1", "status": "started",
                                  "replayed": False}, status=202)

    app = web.Application()
    app.router.add_post("/v1/runs", _runs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    try:
        await wake_mod._self_post_chat_completion(
            _Adapter(host="127.0.0.1", port=port, key="secret"),
            text="[kanban] the event", session_id="api_server:webui",
            idempotency_key="kanban-wake:one")
    finally:
        gate.set()
        await runner.cleanup()

    assert state["admissions"] == ["[kanban] the event"]
    assert state["turns_started"] == 1
