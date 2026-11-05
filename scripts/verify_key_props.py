"""Prove the two properties the fix must have, against the PATCHED code.

P1  STABILITY: the same claimed batch derives the SAME idempotency key; a
    different batch (different cursor / task / chat) derives a DIFFERENT key.
    A retry of one batch must not be able to collide with another batch.

P2  REPLAY: two deliveries of the SAME batch (what the notifier's rewind does
    after a timeout) produce ONE admitted turn, because the server replays the
    durable Idempotency-Key record instead of starting a second turn.

Run: python3 verify_key_props.py <wake.py> <notifier.py>
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

# The repo root goes on sys.path: this script lives under scripts/, but the modules
# it loads (gateway/kanban_watchers_notifier.py) import the repo's own packages
# (``agent.i18n``), which resolve only from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WAKE, NOTIFIER = sys.argv[1], sys.argv[2]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def check_key_props():
    n = load(NOTIFIER, "notifier_under_test")
    sub = {"task_id": "t_1298d286", "platform": "api_server",
           "chat_id": "api_server:webui", "thread_id": ""}
    k1 = n._wake_idempotency_key(sub, 43293)
    k2 = n._wake_idempotency_key(dict(sub), 43293)
    k3 = n._wake_idempotency_key(sub, 43294)
    k4 = n._wake_idempotency_key(dict(sub, task_id="t_other"), 43293)
    k5 = n._wake_idempotency_key(dict(sub, chat_id="webui"), 43293)
    ok = True
    print("P1 key stability:")
    print(f"  same batch twice      : {'SAME' if k1 == k2 else 'DIFFERENT'}  -> {k1}")
    print("  next cursor (43294)   : %s" % ("DIFFERENT" if k1 != k3 else "SAME (BAD)"))
    print("  other task_id         : %s" % ("DIFFERENT" if k1 != k4 else "SAME (BAD)"))
    print("  other chat_id         : %s" % ("DIFFERENT" if k1 != k5 else "SAME (BAD)"))
    ok = ok and k1 == k2 and k1 != k3 and k1 != k4 and k1 != k5
    # The key must satisfy the server's own validation: 1-255 visible ASCII.
    valid = 1 <= len(k1) <= 255 and all(33 <= ord(c) <= 126 for c in k1)
    print("  server key validation (1-255 visible ASCII): %s" % ("OK" if valid else "INVALID"))
    ok = ok and valid
    return ok, k1


async def check_replay(wakepath, key):
    import aiohttp
    from aiohttp import web

    state = {"turns_started": 0, "keys": [], "replays": 0}
    admitted = {}

    async def runs(request):
        k = request.headers.get("Idempotency-Key")
        state["keys"].append(k)
        if k in admitted:
            state["replays"] += 1
            return web.json_response({"id": admitted[k], "status": "admitted"},
                                     headers={"Idempotency-Replayed": "true"})
        admitted[k] = "run_1"
        state["turns_started"] += 1
        return web.json_response({"id": admitted[k], "status": "admitted"}, status=202)

    async def cc(request):
        state["turns_started"] += 1
        await asyncio.sleep(5.0)
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/v1/runs", runs)
    app.router.add_post("/v1/chat/completions", cc)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    wake = load(wakepath, "wake_replay")
    wake.WAKE_TURN_TIMEOUT_SECONDS = 0.5
    wake._RETRY_DELAYS_SECONDS = (0.05, 0.05, 0.05)

    class A:
        _host = "127.0.0.1"
        _port = port
        _api_key = "k"
        _model_name = "hermes-agent"

    # Two deliveries of the SAME batch -> exactly what the notifier's rewind does.
    for attempt in (1, 2):
        try:
            await wake._self_post_chat_completion(
                A(), text="[kanban] Task t_x completed.", session_id="api_server:webui",
                idempotency_key=key)
        # health: allow BLE001 -- the probe counts SERVER admissions; a client-side timeout is the measurement, not a defect.
        except Exception as exc:
            print(f"  delivery {attempt} raised {type(exc).__name__}")
    await runner.cleanup()
    print("P2 replay (two deliveries of the SAME batch):")
    print(f"  turns started   : {state['turns_started']}")
    print(f"  idempotent replays: {state['replays']}")
    print(f"  keys sent       : {state['keys']}")
    ok = state["turns_started"] == 1 and state["replays"] == 1
    return ok


def main():
    ok1, key = check_key_props()
    ok2 = asyncio.run(check_replay(WAKE, key))
    print()
    print("RESULT: %s" % ("PASS - both properties hold" if (ok1 and ok2) else "FAIL"))
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
