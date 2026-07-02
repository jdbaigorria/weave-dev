"""Tests for the mountable chat channel (weave.adapters.driving.chat)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from loom import LoomSession

from weave.adapters.driving.chat import ChatChannel, ChatRequest, chat_router
from weave.application.errors import HarnessError
from weave.application.reply import AgentReply, Usage


def _reply(text: str = "hi", session_id: str = "s-1") -> AgentReply:
    return AgentReply(text=text, usage=Usage(), session_id=session_id)


class _FakeRunner:
    """Records the dispatch call and returns a canned reply (or raises)."""

    def __init__(self, reply: AgentReply | None = None, *, fail: bool = False) -> None:
        self._reply = reply if reply is not None else _reply()
        self._fail = fail
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, name, input, session, *, kind=None, extras=None):
        self.calls.append(
            {"name": name, "input": input, "session": session, "kind": kind, "extras": extras}
        )
        if self._fail:
            raise HarnessError("agent 'x' failed during execution: boom")
        return self._reply


def test_handle_dispatches_with_bound_name_kind_and_extras():
    runner = _FakeRunner(_reply(text="Paris"))
    channel = ChatChannel("assistant", runner, kind="agent")

    reply = asyncio.run(
        channel.handle(
            ChatRequest(
                message="capital of France?", session_id="s-1", user_id="u-1", extras={"jwt": "tok"}
            )
        )
    )

    assert reply.text == "Paris"
    call = runner.calls[0]
    assert call["name"] == "assistant"
    assert call["input"] == "capital of France?"
    assert call["kind"] == "agent"
    assert call["extras"] == {"jwt": "tok"}


def test_handle_builds_identified_session():
    runner = _FakeRunner()
    channel = ChatChannel("assistant", runner)

    asyncio.run(channel.handle(ChatRequest(message="hi", session_id="s-1", user_id="u-1")))

    session: LoomSession = runner.calls[0]["session"]
    assert session.session_id == "s-1"
    assert session.user_id == "u-1"
    assert session.is_anonymous is False


def test_handle_treats_missing_user_as_anonymous():
    runner = _FakeRunner()
    channel = ChatChannel("assistant", runner)

    asyncio.run(channel.handle(ChatRequest(message="hi", session_id="s-1")))

    session: LoomSession = runner.calls[0]["session"]
    assert session.is_anonymous is True
    assert session.user_id == "s-1"  # keyed by session_id when anonymous


def test_handle_accepts_content_blocks():
    runner = _FakeRunner()
    channel = ChatChannel("assistant", runner)
    blocks = [{"text": "describe"}, {"image": {"format": "png", "source": {"bytes": b"x"}}}]

    asyncio.run(channel.handle(ChatRequest(message=blocks, session_id="s-1")))

    assert runner.calls[0]["input"] == blocks


def test_handle_propagates_harness_error():
    channel = ChatChannel("assistant", _FakeRunner(fail=True))
    with pytest.raises(HarnessError, match="failed during execution"):
        asyncio.run(channel.handle(ChatRequest(message="hi", session_id="s-1")))


def test_handle_sync_returns_reply_without_a_loop():
    channel = ChatChannel("assistant", _FakeRunner(_reply(text="Paris")))

    reply = channel.handle_sync(ChatRequest(message="hi", session_id="s-1"))

    assert reply.text == "Paris"


def test_handle_sync_propagates_harness_error():
    channel = ChatChannel("assistant", _FakeRunner(fail=True))
    with pytest.raises(HarnessError, match="failed during execution"):
        channel.handle_sync(ChatRequest(message="hi", session_id="s-1"))


def test_composition_chat_channel_wires_default_engine():
    from weave.composition import chat_channel

    channel = chat_channel("assistant", kind="agent")
    assert isinstance(channel, ChatChannel)
    # A bare name is a constant resolver: it returns that name for any request.
    assert channel._resolve(ChatRequest(message="hi", session_id="s-1")) == "assistant"
    assert channel._kind == "agent"


def test_chat_router_serves_reply_over_http():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    channel = ChatChannel("assistant", _FakeRunner(_reply(text="Paris", session_id="s-7")))
    app = FastAPI()
    app.include_router(chat_router(channel))

    resp = TestClient(app).post("/chat", json={"message": "hi", "session_id": "s-7"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "Paris"
    assert body["session_id"] == "s-7"
    assert "raw" not in body  # escape hatch is excluded from the dump


def test_chat_router_maps_harness_error_to_502():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    channel = ChatChannel("assistant", _FakeRunner(fail=True))
    app = FastAPI()
    app.include_router(chat_router(channel))

    resp = TestClient(app).post("/chat", json={"message": "hi", "session_id": "s-1"})

    assert resp.status_code == 502
    assert "failed during execution" in resp.json()["detail"]


def test_chat_router_maps_loom_error_to_502():
    # An invalid agent definition/config propagates Loom's typed error unwrapped;
    # chat_router maps it to 502, distinct in message from a HarnessError.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from loom.errors import AgentNotFoundError

    async def _runner(name, input, session, *, kind=None, extras=None):
        raise AgentNotFoundError("support_agent")

    app = FastAPI()
    app.include_router(chat_router(ChatChannel("support_agent", _runner)))

    resp = TestClient(app).post("/chat", json={"message": "hi", "session_id": "s-1"})

    assert resp.status_code == 502
    assert "invalid definition or configuration" in resp.json()["detail"]
