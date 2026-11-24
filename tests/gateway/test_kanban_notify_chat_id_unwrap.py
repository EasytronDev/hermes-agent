"""Regression: a kanban subscription whose chat_id holds a FULL SESSION KEY must not
poison the local-session restore with "storage_unavailable".

The bug (measured on a live gateway, card t_e5857df2):

    kanban_notify_subs.chat_id = 'agent:main:local:dm:local-e8f9b7a1c...'
      -> notifier builds SessionSource(chat_id=<that whole key>)
      -> build_session_key() prefixes it a SECOND time:
             agent:main:local:dm:agent:main:local:dm:local-e8f9b7a1c...
      -> _parse_session_key() reads parts[4] == 'agent' as the chat id
      -> restore_local_session(authority, 'agent')
      -> local_receipt(db, 'agent') -> state_meta has no
             'gateway.local_policy.v1:agent' row -> RuntimeStoreError('storage_unavailable')
      -> WARNING "Local session agent receipt refused: storage_unavailable"

Because the notifier ticks every 5s and there was no per-subscription backoff, the
warning was logged once per pending subscription per tick: ~37.000 lines per 5 MB
log rotation, which made the dispatcher lines in gateway.log unreadable.

The fix unwraps a session-key-shaped chat_id back to the chat id it encodes, so the
rebuilt session key round-trips and the receipt lookup finds its row.
"""
import asyncio
import contextlib

from gateway.config import Platform
from gateway.run import GatewayRunner, _parse_session_key
from gateway.session import SessionSource, bare_chat_id, build_session_key
from hermes_state_keys import profile_from_session_key_namespace


BARE_CHAT_ID = "local-e8f9b7a1c3a6637daaa55c491b46dd2871fb8b5ed7b72ecf7d77da569dce6550"
SESSION_KEY = "agent:main:local:dm:" + BARE_CHAT_ID
# The id the double-prefixed key used to parse back to — never a real session id.
POISONED_CHAT_ID = "agent"


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})

    async def handle_message(self, event):
        event._gateway_accepted = True


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.LOCAL: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _sub_row(chat_id):
    return {
        "task_id": "t_deadbeef",
        "platform": "local",
        "chat_id": chat_id,
        "chat_type": "dm",
        "thread_id": None,
        "user_id": None,
        "user_id_alt": None,
        "notifier_profile": "default",
        "delivery_metadata": {},
    }


def _notifier(runner, sub, adapter):
    """A _KanbanNotification wired the way deliver() wires it before the wake."""
    from gateway.kanban_watchers_notifier import _KanbanNotification

    class _Task:
        title = "a task"
        assignee = "devops"
        session_id = SESSION_KEY

    notif = _KanbanNotification(
        runner,
        {"sub": sub, "task": _Task(), "board": "test-board", "events": []},
        platform_cls=Platform,
        sub_fail_counts={},
    )
    notif.plat = Platform.LOCAL
    notif.adapter = adapter
    notif.is_push_adapter = True
    notif.wake_kinds = {"completed"}
    notif.wake_agent = True
    notif.wake_diagnostic = False
    notif.session_key = SESSION_KEY
    notif.synth = "wake text"
    return notif


def test_bare_chat_id_unwraps_a_session_key():
    assert bare_chat_id(SESSION_KEY) == BARE_CHAT_ID


def test_bare_chat_id_leaves_a_real_chat_id_alone():
    assert bare_chat_id(BARE_CHAT_ID) == BARE_CHAT_ID
    assert bare_chat_id("agent") == "agent"
    assert bare_chat_id("12345:987") == "12345:987"
    assert bare_chat_id("") == ""


def test_session_key_round_trips_through_the_parser():
    """The bug's whole mechanism: parts[4] of the rebuilt key must be the chat id."""
    source = SessionSource(platform=Platform.LOCAL, chat_id=SESSION_KEY, chat_type="dm")
    broken = build_session_key(source, group_sessions_per_user=True, thread_sessions_per_user=False)
    assert broken == "agent:main:local:dm:agent:main:local:dm:" + BARE_CHAT_ID
    assert (_parse_session_key(broken) or {}).get("chat_id") == POISONED_CHAT_ID

    fixed_source = SessionSource(platform=Platform.LOCAL, chat_id=bare_chat_id(SESSION_KEY), chat_type="dm")
    fixed = build_session_key(fixed_source, group_sessions_per_user=True, thread_sessions_per_user=False)
    assert fixed == SESSION_KEY
    parsed = _parse_session_key(fixed) or {}
    assert parsed.get("chat_id") == BARE_CHAT_ID
    assert parsed.get("platform") == "local"


def test_notifier_wake_builds_an_unpoisoned_session_key(monkeypatch):
    """The notifier must hand the runtime the creator's REAL chat id, not a session key."""
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    notif = _notifier(runner, _sub_row(SESSION_KEY), adapter)

    seen = {}

    async def fake_deliver_wake(adapter_arg, *, text=None, session_id=None, source=None, **kwargs):
        seen["source"] = source
        seen["session_id"] = session_id
        return True

    monkeypatch.setattr("gateway.wake.deliver_wake", fake_deliver_wake)

    def fake_scope(_home):
        class _Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False
        return _Ctx()

    monkeypatch.setattr("gateway.run._async_profile_runtime_scope", fake_scope)
    monkeypatch.setattr(runner, "_resolve_profile_home_for_source", lambda _src: "/tmp/home")

    asyncio.run(notif.wake())

    source = seen["source"]
    assert source is not None, "the notifier must build a SessionSource for push adapters"
    assert source.chat_id == BARE_CHAT_ID, (
        "the notifier must unwrap a session-key-shaped chat_id before building the source"
    )
    key = build_session_key(source, group_sessions_per_user=True, thread_sessions_per_user=False)
    parsed = _parse_session_key(key) or {}
    assert parsed.get("chat_id") != POISONED_CHAT_ID, (
        "the rebuilt session key must not parse back to the literal 'agent' chat id"
    )
    assert parsed.get("chat_id") == BARE_CHAT_ID


def test_owner_scope_uses_the_unwrapped_chat_id(monkeypatch):
    """The non-push leg (owner scope) builds a source too — it must unwrap as well."""
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner.config = type("_Cfg", (), {"multiplex_profiles": True})()  # type: ignore[assignment]
    notif = _notifier(runner, _sub_row(SESSION_KEY), adapter)

    captured = {}

    def fake_scope(_home):
        captured["home"] = _home
        return contextlib.nullcontext()

    monkeypatch.setattr("gateway.run._async_profile_runtime_scope", fake_scope)

    def fake_resolve(src):
        captured["source"] = src
        return "/tmp/home"

    monkeypatch.setattr(runner, "_resolve_profile_home_for_source", fake_resolve)

    with notif._owner_scope():
        pass

    source = captured.get("source")
    assert source is not None
    assert source.chat_id == BARE_CHAT_ID


def test_profile_namespace_is_not_mistaken_for_a_chat_id():
    """A named-profile key must unwrap to its chat id too (parts[1] is the namespace)."""
    assert bare_chat_id("agent:devops:local:dm:local-abc") == "local-abc"
    assert profile_from_session_key_namespace("devops") == "devops"
