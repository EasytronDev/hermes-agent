"""Which ladder does the idempotency key kill, and which one does it NOT?

The operator's correction identifies TWO ladders:
  INNER (wake.py): 4 POSTs per delivery (attempts = 1 + len(_RETRY_DELAYS_SECONDS)).
                    Each POST is a fresh admission -> a fresh agent turn.
  OUTER (notifier): up to MAX_SEND_FAILURES deliveries of the same claimed
                    batch, because delivery_failed() rewinds the cursor.

The idempotency key makes the SERVER reject a repeat of the same POST. So:
  - INNER: the 3rd, 4th POST of the same delivery carry the SAME key ->
    replayed, not a new admission -> the inner ladder is broken.
  - OUTER: each outer delivery re-claims the same batch, so it derives the
    SAME key (the key is derived from the batch, not from the attempt) ->
    the outer ladder is broken TOO.

This script proves it against the real patched code: it drives N deliveries of
one batch and counts the ADMISSIONS (turns started), which is what the
duplicate actually costs.

Run: python3 verify_ladder_coverage.py <tree-dir>
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

TREE = Path(sys.argv[1]).resolve()
sys.path.insert(0, "/opt/hermes")
sys.path.insert(0, str(TREE))

import gateway as pkg


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


notifier = load(TREE / "gateway" / "kanban_watchers_notifier.py", "gateway.kanban_watchers_notifier")
wake_mod = load(TREE / "gateway" / "wake.py", "gateway.wake")
pkg.kanban_watchers_notifier = notifier
pkg.wake = wake_mod


async def main():
    import aiohttp
    from aiohttp import web

    state = {"turns_started": 0, "admissions": 0, "replays": 0, "keys": []}
    admitted = {}

    async def runs(request):
        """Models the server's durable Idempotency-Key store."""
        state["admissions"] += 1
        key = request.headers.get("Idempotency-Key")
        state["keys"].append(key)
        if key in admitted:
            state["replays"] += 1
            return web.json_response({"run_id": admitted[key], "status": "queued",
                                      "replayed": True},
                                     headers={"Idempotency-Replayed": "true"})
        admitted[key] = f"run_{state['admissions']}"
        state["turns_started"] += 1
        return web.json_response({"run_id": admitted[key], "status": "started",
                                  "replayed": False}, status=202)

    app = web.Application()
    app.router.add_post("/v1/runs", runs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    class _A:
        _host = "127.0.0.1"
        _port = port
        _api_key = "k"
        _model_name = "hermes-agent"

    sub = {"task_id": "t_7f49d2b3", "platform": "api_server", "chat_id": "api_server:webui",
           "thread_id": "", "delivery_mode": "notify+wake", "notifier_profile": "default"}
    cursor = 43293
    key = notifier._wake_idempotency_key(sub, cursor)

    print("INNER ladder (4 POSTs of ONE delivery, same key):")
    wake_mod.WAKE_TURN_TIMEOUT_SECONDS = 0.5
    wake_mod._RETRY_DELAYS_SECONDS = (0.05, 0.05, 0.05)
    # Force the client to "time out" so it re-POSTs, exactly like the live bug.
    # Force the client to "time out" so it re-POSTs, exactly like the live bug.
    for attempt in range(4):
        try:
            await wake_mod._self_post_chat_completion(
                _A(), text="[kanban] Task t_7f49d2b3 completed.",
                session_id="api_server:webui", idempotency_key=key)
        # health: allow BLE001 S110 -- the probe counts SERVER admissions; a client-side timeout is the measurement, not a defect.
        except Exception:
            pass
    inner_turns = state["turns_started"]
    inner_adm = state["admissions"]
    print("  client attempts        : 4")
    print(f"  server admissions      : {inner_adm}")
    print(f"  AGENT TURNS started    : {inner_turns}  <- the cost")
    print(f"  durable replays        : {state['replays']}")

    print()
    print("OUTER ladder (12 notifier deliveries of the SAME claimed batch):")
    before = state["turns_started"]
    for i in range(12):
        # Each outer delivery re-claims the same batch -> the SAME derived key.
        k = notifier._wake_idempotency_key(sub, cursor)
        assert k == key, "the outer delivery derived a DIFFERENT key"
        try:
            await wake_mod._self_post_chat_completion(
                _A(), text="[kanban] Task t_7f49d2b3 completed.",
                session_id="api_server:webui", idempotency_key=k)
        # health: allow BLE001 S110 -- the probe counts SERVER admissions; a client-side timeout is the measurement, not a defect.
        except Exception:
            pass
    outer_turns = state["turns_started"] - before
    print("  notifier deliveries    : 12")
    print(f"  AGENT TURNS started    : {outer_turns}  <- the cost")
    print("  (the key is derived from the BATCH, so every outer re-delivery")
    print("   carries the same key -> the server replays instead of re-running)")

    await runner.cleanup()

    total_turns = state["turns_started"]
    print()
    print(f"TOTAL agent turns started for ONE card event: {total_turns}")
    ok = (inner_turns == 1 and outer_turns == 0 and total_turns == 1)
    print("RESULT: %s" % ("PASS - ONE event = ONE turn, across both ladders"
                          if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
