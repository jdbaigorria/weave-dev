"""Tests for the conversation registry (port + file adapter + list use case + auto-record)."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from weave.adapters.driven.file_registry import FileConversationRegistry
from weave.adapters.driving.chat import ChatChannel, ChatRequest
from weave.application.conversations import list_conversations
from weave.application.errors import HarnessError
from weave.application.reply import AgentReply, Usage


def _counter_clock():
    """Monotonic, lexically sortable timestamps for deterministic ordering."""
    counter = itertools.count()
    return lambda: f"{next(counter):020d}"


def _registry(tmp_path, **kw) -> FileConversationRegistry:
    return FileConversationRegistry(str(tmp_path), **kw)


# --- FileConversationRegistry ---------------------------------------------------


def test_record_then_list_returns_metadata(tmp_path):
    reg = _registry(tmp_path)
    reg.record("u1", "s1", title="Trip planning", metadata={"pinned": True})

    [ref] = reg.list("u1")
    assert ref.session_id == "s1"
    assert ref.user_id == "u1"
    assert ref.title == "Trip planning"
    assert ref.metadata == {"pinned": True}
    assert ref.created_at and ref.updated_at


def test_upsert_preserves_created_at_and_title_bumps_updated_at(tmp_path):
    reg = _registry(tmp_path, clock=_counter_clock())
    reg.record("u1", "s1", title="Original")
    reg.record("u1", "s1")  # per-turn auto-record: no title, must not erase it

    [ref] = reg.list("u1")
    assert ref.title == "Original"  # preserved
    assert ref.created_at == "00000000000000000000"  # first clock tick
    assert ref.updated_at == "00000000000000000001"  # bumped on the second record


def test_list_orders_most_recent_first(tmp_path):
    reg = _registry(tmp_path, clock=_counter_clock())
    reg.record("u1", "s1")
    reg.record("u1", "s2")
    reg.record("u1", "s1")  # touch s1 again → newest

    assert [r.session_id for r in reg.list("u1")] == ["s1", "s2"]


def test_list_empty_for_unknown_user(tmp_path):
    assert _registry(tmp_path).list("nobody") == []


def test_users_are_isolated(tmp_path):
    reg = _registry(tmp_path)
    reg.record("u1", "s1")
    reg.record("u2", "s2")

    assert [r.session_id for r in reg.list("u1")] == ["s1"]
    assert [r.session_id for r in reg.list("u2")] == ["s2"]


def test_delete_forgets_the_entry(tmp_path):
    reg = _registry(tmp_path)
    reg.record("u1", "s1")
    reg.delete("u1", "s1")
    assert reg.list("u1") == []


# --- list_conversations use case ------------------------------------------------


def test_list_conversations_passes_through(tmp_path):
    reg = _registry(tmp_path)
    reg.record("u1", "s1")
    refs = list_conversations(reg, "u1")
    assert [r.session_id for r in refs] == ["s1"]


def test_list_conversations_rejects_non_registry():
    with pytest.raises(HarnessError, match="needs a ConversationRegistry"):
        list_conversations(object(), "u1")  # type: ignore[arg-type]


# --- ChatChannel auto-record ----------------------------------------------------


class _SpyRegistry:
    """Records record() calls; satisfies the ConversationRegistry protocol."""

    def __init__(self) -> None:
        self.recorded: list[tuple[str, str]] = []

    def record(self, user_id, session_id, *, title=None, metadata=None) -> None:
        self.recorded.append((user_id, session_id))

    def list(self, user_id):  # pragma: no cover - not exercised here
        return []

    def delete(self, user_id, session_id) -> None:  # pragma: no cover
        ...


async def _runner(name, input, session, *, kind=None, extras=None):
    return AgentReply(text="ok", usage=Usage(), session_id=session.session_id)


async def _streamer(name, input, session, *, extras=None):
    return
    yield  # pragma: no cover - makes this an async generator


def test_handle_records_identified_turn(tmp_path):
    reg = _SpyRegistry()
    channel = ChatChannel("assistant", _runner, registry=reg)
    asyncio.run(channel.handle(ChatRequest(message="hi", session_id="s1", user_id="u1")))
    assert reg.recorded == [("u1", "s1")]


def test_handle_skips_anonymous_turn(tmp_path):
    reg = _SpyRegistry()
    channel = ChatChannel("assistant", _runner, registry=reg)
    asyncio.run(channel.handle(ChatRequest(message="hi", session_id="s1")))  # no user_id
    assert reg.recorded == []


def test_handle_without_registry_is_noop():
    channel = ChatChannel("assistant", _runner)  # no registry
    reply = asyncio.run(channel.handle(ChatRequest(message="hi", session_id="s1", user_id="u1")))
    assert reply.text == "ok"  # just works, nothing recorded


def test_stream_records_on_call(tmp_path):
    reg = _SpyRegistry()
    channel = ChatChannel("assistant", _runner, streamer=_streamer, registry=reg)
    channel.stream(ChatRequest(message="hi", session_id="s1", user_id="u1"))
    assert reg.recorded == [("u1", "s1")]


def test_registry_protocol_is_satisfied_by_file_adapter(tmp_path):
    from weave.ports.registry import ConversationRegistry

    assert isinstance(_registry(tmp_path), ConversationRegistry)
    assert isinstance(_SpyRegistry(), ConversationRegistry)
