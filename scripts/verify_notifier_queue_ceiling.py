"""The notifier's serial delivery loop: what is the worst-case wait NOW?

The card asks the design question: "the notifier delivers one card at a time
(session-wide). Is that delay acceptable BY DESIGN even without a loop?"

After ACK-on-admission the self-post returns at admission, so the serial loop's
per-card cost drops from "the whole agent turn" to "the HTTP round-trip". This
script measures the new ceiling with the REAL notifier + wake modules.

Run: python3 verify_notifier_queue_ceiling.py <tree-dir>
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

    state = {"admissions": [], "turn_seconds": []}
    gate = asyncio.Event()

    async def runs(request):
        body = await request.json()
        state["admissions"].append(body.get("input"))

        async def _turn():
            # The agent turn is SLOW -- 600s in production. The delivery must not
            # wait for it.
            await gate.wait()

        # Keep a reference: a fire-and-forget task can be garbage collected mid-run.
        state.setdefault("turn_tasks", []).append(asyncio.create_task(_turn()))
        return web.json_response({"run_id": "run_1", "status": "started",
                                  "replayed": False}, status=202)

    async def chat_completions(request):
        body = await request.json()
        state["admissions"].append(body["messages"][0]["content"])
        await gate.wait()
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
    wake_mod.WAKE_TURN_TIMEOUT_SECONDS = 600.0   # the production ceiling
    wake_mod._RETRY_DELAYS_SECONDS = (0.01, 0.01, 0.01)
    adapter = _Adapter("127.0.0.1", port)

    try:
        # The notifier's loop is serial: N cards, one at a time.
        N = 10
        started = time.monotonic()
        for i in range(N):
            await wake_mod._self_post_chat_completion(
                adapter, text=f"[kanban] card {i}", session_id="api_server:webui",
                idempotency_key=f"kanban-wake:{i}")
        elapsed = time.monotonic() - started
    finally:
        wake_mod.WAKE_TURN_TIMEOUT_SECONDS = old_timeout
        wake_mod._RETRY_DELAYS_SECONDS = old_delays
        gate.set()
        await runner.cleanup()

    print()
    print("=" * 78)
    print("TETO DA FILA DA SESSAO (com ACK na admissao)")
    print("=" * 78)
    print(f"cards entregues em serie      : {N}")
    print(f"tempo total                  : {elapsed:.3f}s")
    print(f"por card (media)              : {elapsed/N*1000:.1f}ms")
    print("turno do agente em producao   : 600s (WAKE_TURN_TIMEOUT_SECONDS)")
    print()
    print("ANTES (self-post no /v1/chat/completions): o loop serial ficava "
          "600s POR CARD")
    print(f"  -> {N} cards = {N*600}s = {N*600/60:.0f} min de fila retida")
    print(f"DEPOIS (ACK na admissao)      : {elapsed:.3f}s para {N} cards "
          f"(~{elapsed/N*1000:.0f}ms por card)")
    print("  -> a fila nao' depende mais do turno do agente")
    print()
    print(f"admissoes: {len(state['admissions'])} (1 por card entregue -- "
          f"nenhuma entrega perdida, nenhuma duplicata)")
    print("=" * 78)
    return 0 if len(state["admissions"]) == N else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
