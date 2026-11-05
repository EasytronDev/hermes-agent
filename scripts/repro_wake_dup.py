"""Repro: does the wake self-post still start N agent turns for ONE event?

Loads the REAL gateway/wake.py (patched copy) by path and drives
``_self_post_chat_completion`` against an in-process aiohttp server that
models a BUSY api_server: the /v1/chat/completions handler blocks past the
client timeout, and /v1/runs admits-and-answers-immediately while honouring
Idempotency-Key (durable replay of the SAME run).

Counts server-side admissions (turns actually started) for one delivery.

Run:  python3 repro_wake_dup.py <path-to-wake.py>
"""
import asyncio
import importlib.util
import sys
import time

WAKEPATH = sys.argv[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Adapter:
    def __init__(self, host, port, key):
        self._host = host
        self._port = port
        self._api_key = key
        self._model_name = "hermes-agent"


async def main():
    import aiohttp
    from aiohttp import web

    state = {"chat_completions": 0, "runs": 0, "keys": [], "turns_started": 0,
             "replayed": 0}
    admitted_runs = {}

    async def chat_completions(request):
        # Models a BUSY api_server: the turn outlives the client's timeout.
        state["chat_completions"] += 1
        state["turns_started"] += 1
        await asyncio.sleep(5.0)
        return web.json_response({"id": "cc", "choices": []})

    async def runs(request):
        state["runs"] += 1
        key = request.headers.get("Idempotency-Key")
        state["keys"].append(key)
        await request.json()
        if key and key in admitted_runs:
            # Durable replay: the SAME run, no second turn.
            state["replayed"] += 1
            return web.json_response(
                {"id": admitted_runs[key], "status": "admitted"},
                headers={"Idempotency-Replayed": "true"})
        if key:
            admitted_runs[key] = "run_" + str(state["runs"])
        state["turns_started"] += 1
        # /v1/runs admits and answers at once.
        return web.json_response({"id": admitted_runs.get(key), "status": "admitted"},
                                 status=202)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_post("/v1/runs", runs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    wake = load(WAKEPATH, "wake_under_test")
    # Compress the constants so the repro runs in seconds, not 40 minutes.
    wake.WAKE_TURN_TIMEOUT_SECONDS = 0.5
    wake._RETRY_DELAYS_SECONDS = (0.05, 0.05, 0.05)

    adapter = _Adapter("127.0.0.1", port, "test-key")

    # --- Scenario A: the SHIPPED behaviour (no idempotency key) ---
    state["chat_completions"] = state["runs"] = state["turns_started"] = 0
    state["keys"].clear(); state["replayed"] = 0
    outcome_a = "delivered"
    try:
        await wake._self_post_chat_completion(
            adapter, text="[kanban] Task t_x completed.", session_id="api_server:webui")
    # health: allow BLE001 -- the probe compares the SHIPPED and the PATCHED behaviour; an exception IS one of the outcomes it reports.
    except Exception as exc:
        outcome_a = f"raised {type(exc).__name__}"
    a = dict(state)

    # --- Scenario B: the PATCHED behaviour (idempotency key present) ---
    state["chat_completions"] = state["runs"] = state["turns_started"] = 0
    state["keys"].clear(); state["replayed"] = 0
    outcome_b = "delivered"
    try:
        await wake._self_post_chat_completion(
            adapter, text="[kanban] Task t_x completed.", session_id="api_server:webui",
            idempotency_key="kanban-wake:deadbeef")
    # health: allow BLE001 -- the probe compares the SHIPPED and the PATCHED behaviour; an exception IS one of the outcomes it reports.
    except Exception as exc:
        outcome_b = f"raised {type(exc).__name__}"
    b = dict(state)

    await runner.cleanup()

    print(f"A shipped (no key): chat_completions={a['chat_completions']} runs={a['runs']} turns_started={a['turns_started']} outcome={outcome_a}")
    print(f"B patched (key):    chat_completions={b['chat_completions']} runs={b['runs']} turns_started={b['turns_started']} outcome={outcome_b}")
    print(f"B idempotency keys sent: {b['keys']}")
    print()
    if b["turns_started"] > 1:
        print(f"RESULT: FAIL - the patched path still started {b['turns_started']} turns for ONE event")
        return 1
    if b["turns_started"] != 1:
        print(f"RESULT: FAIL - expected exactly 1 turn, got {b['turns_started']}")
        return 1
    if not b["keys"] or not b["keys"][0]:
        print("RESULT: FAIL - no Idempotency-Key header was sent")
        return 1
    print("RESULT: PASS - ONE event -> exactly ONE admitted turn, with an Idempotency-Key")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
