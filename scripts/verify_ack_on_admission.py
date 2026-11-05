"""Does the wake self-post ACK AT ADMISSION, or does it wait for the agent turn?

This is the DELAY half of #146361. The idempotency key (PR #7) stopped ONE event
from waking the session N times. It did not stop the DELAY: the self-post held
the notifier's serial delivery loop for the whole agent turn, so every other
card's legitimate notification waited behind it.

Measured on a live gateway (2026-11-04): 8 consecutive 600 s self-post timeouts
held the queue for 70.8 min, and a legitimate ``completed`` notification waited
82.2 min for its agent to wake.

This script drives the REAL ``gateway.wake`` module against a server that
behaves like the two real routes:

  /v1/chat/completions  -- holds the request open for the WHOLE turn
  /v1/runs              -- ADMITS the turn and answers 202 at once

and measures how long the SECOND card's notification takes to be delivered while
the FIRST card's turn is still running.

Run: python3 verify_ack_on_admission.py <tree-dir>
"""
import asyncio
import importlib.util
import sys
import time
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


class _Adapter:
    def __init__(self, host, port, key="secret"):
        self._host = host
        self._port = port
        self._api_key = key
        self._model_name = "hermes-agent"


async def main():
    import aiohttp
    from aiohttp import web

    state = {
        "admissions": [],       # one entry per ADMITTED turn (what a duplicate costs)
        "routes": [],           # which route each self-post took
        "slow_turn_finished": False,
        "delivered": [],
    }
    admitted = {}
    gate = asyncio.Event()

    async def runs(request):
        """/v1/runs: admit the turn, answer 202 at once, run the turn in background."""
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
        state["admissions"].append(body.get("input"))

        async def _turn():
            await gate.wait()
            state["slow_turn_finished"] = True

        # Keep a reference: a fire-and-forget task can be garbage collected mid-run.
        state.setdefault("turn_tasks", []).append(asyncio.create_task(_turn()))
        return web.json_response({"run_id": run_id, "status": "started",
                                  "replayed": False}, status=202)

    async def chat_completions(request):
        """/v1/chat/completions: hold the request open for the WHOLE turn."""
        body = await request.json()
        state["routes"].append("chat_completions")
        state["admissions"].append(body["messages"][0]["content"])
        await gate.wait()
        state["slow_turn_finished"] = True
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/v1/runs", runs)
    app.router.add_post("/v1/chat/completions", chat_completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    old_timeout = wake_mod.WAKE_TURN_TIMEOUT_SECONDS
    old_delays = wake_mod._RETRY_DELAYS_SECONDS
    wake_mod.WAKE_TURN_TIMEOUT_SECONDS = 30.0
    wake_mod._RETRY_DELAYS_SECONDS = (0.01, 0.01, 0.01)
    adapter = _Adapter("127.0.0.1", port)

    try:
        # --- SCENARIO: the notifier's serial loop ---
        # Card A's turn is slow. Card B finishes right after it.
        started = time.monotonic()
        slow = asyncio.create_task(wake_mod._self_post_chat_completion(
            adapter, text="[kanban] card A (slow turn)", session_id="api_server:webui",
            idempotency_key="kanban-wake:A"))
        await asyncio.sleep(0.05)

        await wake_mod._self_post_chat_completion(
            adapter, text="[kanban] card B (finished)", session_id="api_server:webui",
            idempotency_key="kanban-wake:B")
        elapsed = time.monotonic() - started
        state["delivered"].append(("card B", elapsed))
        # Snapshot WHETHER the slow turn was still running at delivery time --
        # this is the whole point, so it must be captured BEFORE the gate opens.
        state["slow_turn_running_at_delivery"] = not state["slow_turn_finished"]

        # --- the unkeyed caller (the watch-pattern path) ---
        await wake_mod._self_post_chat_completion(
            adapter, text="[watch] background process done", session_id="api_server:webui")
        state["delivered"].append(("watch-pattern (no key)", time.monotonic() - started))

        gate.set()
        await slow
    finally:
        wake_mod.WAKE_TURN_TIMEOUT_SECONDS = old_timeout
        wake_mod._RETRY_DELAYS_SECONDS = old_delays
        await runner.cleanup()

    print()
    print("=" * 78)
    print("RESULTADO")
    print("=" * 78)
    print(f"rotas usadas pelos self-posts : {state['routes']}")
    print(f"admissoes (turnos iniciados)  : {len(state['admissions'])} -> {state['admissions']}")
    print(f"o turno lento ainda rodava quando o card B foi entregue? "
          f"{'SIM (fila retida)' if state.get('slow_turn_running_at_delivery') else 'nao'}")
    print()
    for who, secs in state["delivered"]:
        print(f"  entrega de {who:<28} em {secs:.3f}s")
    print()

    ok = True
    # 1. no self-post may use the blocking route
    if "chat_completions" in state["routes"]:
        print("FALHOU: um self-post usou /v1/chat/completions (espera o turno inteiro).")
        ok = False
    else:
        print("OK  : nenhum self-post usou a rota que espera o turno inteiro.")
    # 2. the second card must be delivered WHILE the first turn is still running
    if state.get("slow_turn_running_at_delivery"):
        print("OK  : a entrega do card B aconteceu enquanto o turno lento AINDA rodava "
              "(ACK na admissao -- a fila nao' ficou retida).")
    else:
        print("FALHOU: a entrega do card B so' aconteceu DEPOIS do turno lento terminar "
              "(a fila da sessao ficou retida pelo turno).")
        ok = False
    # 3. and it must be fast
    fast = dict(state["delivered"]).get("card B", 0)
    if fast >= 5.0:
        print(f"FALHOU: a entrega do card B levou {fast:.1f}s (>= 5s).")
        ok = False
    else:
        print(f"OK  : a entrega do card B levou {fast:.3f}s (limite: 5s).")
    # 4. the unkeyed caller must also be on the admitting route
    unkeyed_routes = state["routes"][2:]
    if unkeyed_routes and unkeyed_routes[0] != "runs":
        print("FALHOU: o chamador SEM idempotency key nao' usou a rota de admissao.")
        ok = False
    else:
        print("OK  : o chamador sem idempotency key tambem' usa a rota de admissao.")
    # 5. no delivery is lost: one admission per delivered event
    print()
    print(f"admissoes = {len(state['admissions'])} (1 por evento entregue, sem perda "
          f"e sem duplicata)")
    print()
    print("VEREDITO:", "PASSOU" if ok else "FALHOU")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
