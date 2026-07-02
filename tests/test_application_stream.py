"""Tests for the bidi streaming executor (weave.application.stream)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from loom import LoomSession

from weave.application.stream import BidiStream, StreamEvent, _project, open_stream


class _FakeBidiAgent:
    """A BidiAgent stand-in recording lifecycle + sends, yielding scripted events."""

    def __init__(self, events: list[Any] | None = None) -> None:
        self._events = events or []
        self.started_with: Any = "UNSET"
        self.stopped = False
        self.cleaned_up = False
        self.sent: list[Any] = []

    async def start(self, invocation_state=None) -> None:
        self.started_with = invocation_state

    async def stop(self) -> None:
        self.stopped = True

    def cleanup(self) -> None:
        self.cleaned_up = True

    async def send(self, input_data) -> None:
        self.sent.append(input_data)

    async def receive(self):
        for ev in self._events:
            yield ev


class _FakeEngine:
    def __init__(self, agent: _FakeBidiAgent) -> None:
        self._agent = agent

    def build_agent(self, name: str, session: LoomSession):
        return self._agent


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


# --- projection (duck-typed) ---------------------------------------------------


def test_project_audio_event():
    ev = SimpleNamespace(audio="b64", sample_rate=16000, channels=1, format="pcm")
    out = _project(ev)
    assert out.type == "audio" and out.audio == "b64" and out.raw is ev


def test_project_transcript_event():
    ev = SimpleNamespace(text="hello", is_final=True, role="assistant")
    out = _project(ev)
    assert out.type == "text" and out.text == "hello"


def test_project_tool_use_event():
    ev = type("ToolUseStreamEvent", (), {})()
    assert _project(ev).type == "tool_call"


def test_project_interruption_is_control():
    ev = SimpleNamespace(reason="user_speech")  # barge-in
    out = _project(ev)
    assert out.type == "control" and out.control is ev


# --- BidiStream lifecycle ------------------------------------------------------


def test_open_stream_returns_bidi_stream():
    agent = _FakeBidiAgent()
    assert isinstance(open_stream(_FakeEngine(agent), "voice", _session()), BidiStream)


def test_aenter_starts_with_extras_and_aexit_stops_and_cleans_up():
    agent = _FakeBidiAgent()

    async def scenario():
        async with open_stream(_FakeEngine(agent), "voice", _session(), extras={"k": "v"}):
            pass

    asyncio.run(scenario())
    assert agent.started_with == {"k": "v"}
    assert agent.stopped is True
    assert agent.cleaned_up is True


def test_aexit_cleans_up_even_if_stop_raises():
    agent = _FakeBidiAgent()

    async def boom():
        raise RuntimeError("stop failed")

    agent.stop = boom  # type: ignore[assignment]

    async def scenario():
        async with open_stream(_FakeEngine(agent), "voice", _session()):
            pass

    try:
        asyncio.run(scenario())
    except RuntimeError:
        pass
    assert agent.cleaned_up is True


def test_send_text_passes_string_through():
    agent = _FakeBidiAgent()

    async def scenario():
        async with open_stream(_FakeEngine(agent), "voice", _session()) as stream:
            await stream.send_text("hi there")

    asyncio.run(scenario())
    assert agent.sent == ["hi there"]


def test_receive_yields_projected_events():
    events = [
        SimpleNamespace(text="hi", is_final=False, role="assistant"),
        SimpleNamespace(audio="b64", sample_rate=24000, channels=1, format="pcm"),
        SimpleNamespace(reason="user_speech"),
    ]
    agent = _FakeBidiAgent(events)

    async def scenario() -> list[StreamEvent]:
        async with open_stream(_FakeEngine(agent), "voice", _session()) as stream:
            return [ev async for ev in stream.receive()]

    out = asyncio.run(scenario())
    assert [e.type for e in out] == ["text", "audio", "control"]
