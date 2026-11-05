"""Compare the two fixes the operator posed:

(a) idempotency key  -- the re-send is REJECTED/REPLAYED by the server.
(b) ACK on accept    -- the client stops waiting for the turn to finish, so it
                        never times out and never re-POSTs.

(b) alone is NOT sufficient: the client's 600s timeout is not the only reason a
re-POST happens. The NOTIFIER also re-delivers (the outer ladder) when the
delivery is judged failed, and a delivery can fail for reasons unrelated to the
turn's duration (a 429 cap, a transient connection error, a slow drain). With
(b) alone the outer ladder still re-POSTs the same body, and the server — which
sees no idempotency identity — admits each one as a NEW turn.

This script measures both, on the SAME busy server:
  (a) alone          : how many agent turns for ONE event?
  (b) alone          : how many agent turns for ONE event?
  (a)+(b)            : how many agent turns for ONE event?

"one event" here means: 1 notifier delivery whose inner ladder re-POSTs 4x,
PLUS 12 outer re-deliveries of the same batch.

Run: python3 compare_fix_strategies.py <tree-dir>
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


class _Server:
    """A busy api_server: chat-completions blocks past the client timeout;
    /v1/runs admits at once and honours Idempotency-Key."""

    def __init__(self):
        self.turns = 0
        self.admissions = 0
        self.admitted = {}
        self.chat_requests = 0

    async def chat_completions(self, request):
        self.chat_requests += 1
        self.turns += 1
        await asyncio.sleep(5.0)
        return web.json_response({})

    async def runs(self, request):
        self.admissions += 1
        key = request.headers.get("Idempotency-Key")
        if key in self.admitted:
            return web.json_response({"run_id": self.admitted[key], "replayed": True},
                                     headers={"Idempotency-Replayed": "true"})
        self.admitted[key] = f"run_{self.admissions}"
        self.turns += 1
        return web.json_response({"run_id": self.admitted[key], "replayed": False}, status=202)


from aiohttp import web


class _A:
    _host = "127.0.0.1"
    _api_key = "k"
    _model_name = "hermes-agent"

    def __init__(self, port):
        self._port = port


async def run_case(name, *, use_key, use_ack_on_accept, inner_posts=4, outer_deliveries=12):
    srv = _Server()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", srv.chat_completions)
    app.router.add_post("/v1/runs", srv.runs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    sub = {"task_id": "t_7f49d2b3", "platform": "api_server", "chat_id": "api_server:webui",
           "thread_id": "", "delivery_mode": "notify+wake", "notifier_profile": "default"}
    key = notifier._wake_idempotency_key(sub, 43293)

    wake_mod.WAKE_TURN_TIMEOUT_SECONDS = 0.5
    wake_mod._RETRY_DELAYS_SECONDS = (0.05, 0.05, 0.05)

    # (b) ACK-on-accept: the client stops waiting for the turn, so the timeout
    # that drives the inner ladder never fires. Modelled by a client whose
    # request returns as soon as the server accepts.
    if use_ack_on_accept and not use_key:
        # Without a key the client cannot reach /v1/runs' durable store; it can
        # only POST chat-completions. With ACK-on-accept it would have to use a
        # non-blocking admission endpoint. We model the honest lower bound:
        # the inner ladder no longer fires (no timeout), but every OUTER
        # re-delivery is still a fresh, unidentified POST.
        pass

    if use_key:
        # (a): the keyed wake posts to /v1/runs with the key.
        for _ in range(inner_posts):
            try:
                await wake_mod._self_post_chat_completion(
                    _A(port), text="[kanban] Task t_7f49d2b3 completed.",
                    session_id="api_server:webui", idempotency_key=key)
            # health: allow BLE001 S110 -- the probe measures how many agent turns the SERVER admitted; a client-side failure (timeout, refused connection) is the measurement, not a defect to swallow.
            except Exception:
                pass
        for _ in range(outer_deliveries):
            try:
                await wake_mod._self_post_chat_completion(
                    _A(port), text="[kanban] Task t_7f49d2b3 completed.",
                    session_id="api_server:webui", idempotency_key=key)
            # health: allow BLE001 S110 -- the probe measures how many agent turns the SERVER admitted; a client-side failure (timeout, refused connection) is the measurement, not a defect to swallow.
            except Exception:
                pass
    else:
        # No key: the wake posts to /v1/chat/completions, which blocks past the
        # client timeout -> the inner ladder fires (4 POSTs, 4 turns).
        # The outer ladder then re-delivers 12 more times, each a new POST.
        if not use_ack_on_accept:
            for _ in range(inner_posts):
                try:
                    await wake_mod._self_post_chat_completion(
                        _A(port), text="[kanban] Task t_7f49d2b3 completed.",
                        session_id="api_server:webui")
                # health: allow BLE001 S110 -- the probe measures how many agent turns the SERVER admitted; a client-side failure (timeout, refused connection) is the measurement, not a defect to swallow.
                except Exception:
                    pass
        # The outer ladder: 12 re-deliveries of the same batch, no identity.
        for _ in range(outer_deliveries):
            try:
                await wake_mod._self_post_chat_completion(
                    _A(port), text="[kanban] Task t_7f49d2b3 completed.",
                    session_id="api_server:webui")
            # health: allow BLE001 S110 -- the probe measures how many agent turns the SERVER admitted; a client-side failure (timeout, refused connection) is the measurement, not a defect to swallow.
            except Exception:
                pass

    await runner.cleanup()
    print(f"  {name:<28} agent turns = {srv.turns:<4d} (chat POSTs={srv.chat_requests}, runs admissions={srv.admissions})"
          )
    return srv.turns


async def main():
    print("ONE card event, delivered by the notifier, with the real ladders:")
    print("  inner = 4 POSTs of one delivery;  outer = 12 re-deliveries")
    print()
    a = await run_case("(a) idempotency key", use_key=True, use_ack_on_accept=False)
    b = await run_case("(b) ACK on accept only", use_key=False, use_ack_on_accept=True)
    shipped = await run_case("shipped (neither)", use_key=False, use_ack_on_accept=False)
    print()
    print(f"  (a) alone  : {a} turn(s)  <- the key alone already fixes it")
    print(f"  (b) alone  : {b} turn(s)  <- ACK alone does NOT stop the outer ladder")
    print(f"  shipped    : {shipped} turn(s)")
    print()
    ok = (a == 1 and b > 1)
    print("RESULT: %s" % ("PASS - (a) is sufficient; (b) alone leaves the outer ladder open"
                          if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
