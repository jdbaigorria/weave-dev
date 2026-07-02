"""Tests for token-streaming chat: stream_agent use-case + ChatChannel.stream + SSE."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from loom import LoomSession

from weave.adapters.driving.chat import ChatChannel, ChatRequest, chat_router
from weave.application.errors import HarnessError
from weave.application.reply import AgentReply, ChatStreamEvent, Usage
from weave.application.run import stream_agent


def _result(text: str = "Paris") -> SimpleNamespace:
    """Stand-in Strands AgentResult, projectable by agent_reply_from_result."""
    return SimpleNamespace(
        message={"content": [{"text": text}]},
        structured_output=None,
        stop_reason="end_turn",
        interrupts=[],
        metrics=SimpleNamespace(
            get_summary=lambda: {
                "accumulated_usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5},
                "total_duration": 12.0,
                "tool_usage": {},
            }
        ),
    )


class _StreamAgent:
    """A built agent whose stream_async replays canned events (then optionally fails)."""

    def __init__(self, events: list[Any], *, fail: bool = False) -> None:
        self._events = events
        self._fail = fail
        self.invoked_with: dict[str, Any] = {}
        self.cleaned_up = False

    async def stream_async(self, prompt, *, invocation_state=None):
        self.invoked_with = {"prompt": prompt, "invocation_state": invocation_state}
        for event in self._events:
            yield event
        if self._fail:
            raise RuntimeError("boom")

    def cleanup(self) -> None:
        self.cleaned_up = True


class _FakeEngine:
    def __init__(self, agent: _StreamAgent) -> None:
        self.agent = agent

    def build_agent(self, name: str, session: LoomSession):
        return self.agent


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


async def _drain(aiter) -> list[ChatStreamEvent]:
    return [event async for event in aiter]


# --- stream_agent use-case ------------------------------------------------------


def test_stream_agent_projects_deltas_then_done():
    agent = _StreamAgent([{"data": "Par"}, {"data": "is"}, {"result": _result("Paris")}])
    events = asyncio.run(_drain(stream_agent(_FakeEngine(agent), "assistant", "hi", _session())))

    assert [e.type for e in events] == ["delta", "delta", "done"]
    assert "".join(e.text or "" for e in events if e.type == "delta") == "Paris"
    done = events[-1]
    assert isinstance(done.reply, AgentReply)
    assert done.reply.text == "Paris"
    assert done.reply.usage.total_tokens == 5
    assert done.reply.session_id == "s-1"


def test_stream_agent_forwards_extras_and_cleans_up():
    agent = _StreamAgent([{"result": _result()}])
    asyncio.run(
        _drain(stream_agent(_FakeEngine(agent), "assistant", "hi", _session(), extras={"jwt": "t"}))
    )
    assert agent.invoked_with["invocation_state"] == {"jwt": "t"}
    assert agent.cleaned_up is True


def test_stream_agent_wraps_midstream_error_and_cleans_up():
    agent = _StreamAgent([{"data": "hi"}], fail=True)

    async def run():
        async for _ in stream_agent(_FakeEngine(agent), "assistant", "hi", _session()):
            pass

    with pytest.raises(HarnessError, match="failed during streaming"):
        asyncio.run(run())
    assert agent.cleaned_up is True


def test_stream_agent_cleans_up_when_consumer_abandons():
    agent = _StreamAgent([{"data": "a"}, {"data": "b"}, {"result": _result()}])

    async def run():
        aiter = stream_agent(_FakeEngine(agent), "assistant", "hi", _session())
        async for _ in aiter:
            break  # abandon after the first event
        await aiter.aclose()

    asyncio.run(run())
    assert agent.cleaned_up is True


# --- ChatChannel.stream ---------------------------------------------------------


def test_channel_stream_dispatches_with_name_session_and_extras():
    calls: list[dict[str, Any]] = []

    async def streamer(name, input, session, *, extras=None):
        calls.append({"name": name, "input": input, "session": session, "extras": extras})
        yield ChatStreamEvent(type="delta", text="hi")

    channel = ChatChannel("assistant", _unused_runner, streamer=streamer)
    events = asyncio.run(
        _drain(channel.stream(ChatRequest(message="hi", session_id="s-1", extras={"k": "v"})))
    )

    assert [e.type for e in events] == ["delta"]
    call = calls[0]
    assert call["name"] == "assistant"
    assert call["extras"] == {"k": "v"}
    assert call["session"].is_anonymous is True  # no user_id


def test_channel_stream_without_streamer_raises():
    channel = ChatChannel("assistant", _unused_runner)  # no streamer
    with pytest.raises(HarnessError, match="no streamer"):
        channel.stream(ChatRequest(message="hi", session_id="s-1"))


def test_composition_chat_channel_wires_streamer():
    from weave.composition import chat_channel
    from weave.composition import stream_agent as wired_stream_agent

    channel = chat_channel("assistant")
    assert channel._streamer is wired_stream_agent


# --- SSE over chat_router -------------------------------------------------------


def _app_with(channel: ChatChannel):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(chat_router(channel))
    return app


def test_chat_router_streams_sse_frames():
    from fastapi.testclient import TestClient

    async def streamer(name, input, session, *, extras=None):
        yield ChatStreamEvent(type="delta", text="Par")
        yield ChatStreamEvent(type="delta", text="is")
        yield ChatStreamEvent(
            type="done", reply=AgentReply(text="Paris", usage=Usage(), session_id="s-1")
        )

    channel = ChatChannel("assistant", _unused_runner, streamer=streamer)
    resp = TestClient(_app_with(channel)).post(
        "/chat/stream", json={"message": "hi", "session_id": "s-1"}
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in resp.text.split("\n\n") if line.strip()]
    assert len(frames) == 3
    assert all(f.startswith("data: ") for f in frames)
    assert '"text":"Par"' in frames[0]
    assert '"type":"done"' in frames[2]
    assert '"raw"' not in frames[2]  # escape hatch excluded


def test_chat_router_stream_emits_error_frame_midstream():
    from fastapi.testclient import TestClient

    async def streamer(name, input, session, *, extras=None):
        yield ChatStreamEvent(type="delta", text="hi")
        raise HarnessError("agent 'x' failed during streaming: boom")

    channel = ChatChannel("assistant", _unused_runner, streamer=streamer)
    resp = TestClient(_app_with(channel)).post(
        "/chat/stream", json={"message": "hi", "session_id": "s-1"}
    )

    assert resp.status_code == 200  # status already sent; failure rides as a frame
    assert '"type": "error"' in resp.text
    assert "failed during streaming" in resp.text


async def _unused_runner(*args, **kwargs):  # pragma: no cover - never called in stream tests
    raise AssertionError("runner should not be used by stream tests")
