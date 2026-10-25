"""Tests for the bidi streaming executor (weave.application.stream)."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import Any

import pytest
from loom import LoomSession
from strands.bidi.types import (
    AudioDelta,
    BidiAudioDeltaEvent,
    BidiBargeInEvent,
    BidiReasoningDeltaEvent,
    BidiTextBlockEvent,
    BidiTextDeltaEvent,
    BidiToolUseBlocksEvent,
    BidiTranscriptBlockEvent,
    BidiTranscriptDeltaEvent,
)

from weave.application.errors import HarnessError
from weave.application.stream import BidiStream, StreamEvent, _project, open_stream


class _FakeBidiModel:
    """Exposes the resolved audio config the way a Strands bidi model does."""

    def get_audio_config(self) -> dict[str, Any]:
        stream = {"sample_rate": 16000, "channels": 1, "format": "pcm"}
        return {"input": stream, "output": stream}


class _FakeBidiAgent:
    """A BidiAgent stand-in recording lifecycle + sends, yielding scripted events."""

    def __init__(self, events: list[Any] | None = None, model: Any = None) -> None:
        self._events = events or []
        self.model = model
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

    def build_agent(self, name: str, session: LoomSession, *, model_params=None):
        return self._agent


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


# --- projection (real Strands bidi events) ---------------------------------------


def test_project_audio_delta_event():
    ev = BidiAudioDeltaEvent(
        audio="b64", format="pcm", sample_rate=16000, channels=1, content_id="c-1"
    )
    out = _project(ev)
    assert out.type == "audio" and out.audio == "b64" and out.raw is ev


@pytest.mark.parametrize(
    "ev",
    [
        BidiTextDeltaEvent(delta="hello", content_id="c-1"),
        BidiTranscriptDeltaEvent(delta="hello", role="user", content_id="c-1"),
    ],
)
def test_project_text_and_transcript_deltas_are_text(ev):
    out = _project(ev)
    assert out.type == "text" and out.text == "hello"


@pytest.mark.parametrize(
    "ev",
    [
        # Completed blocks repeat the deltas; projecting them as text would duplicate it.
        BidiTextBlockEvent(text="hello", content_id="c-1"),
        BidiTranscriptBlockEvent(transcript="hello", role="assistant", content_id="c-1"),
        BidiReasoningDeltaEvent(delta="thinking", content_id="c-1"),
    ],
)
def test_project_blocks_and_reasoning_are_control(ev):
    out = _project(ev)
    assert out.type == "control" and out.control is ev


def test_project_tool_use_blocks_event():
    ev = BidiToolUseBlocksEvent(tool_uses=[{"toolUseId": "t-1", "name": "lookup", "input": {}}])
    out = _project(ev)
    assert out.type == "tool_call" and out.tool_call is ev


def test_project_barge_in_is_control():
    ev = BidiBargeInEvent()
    out = _project(ev)
    assert out.type == "control" and out.control is ev


def test_project_non_dict_event_is_control():
    ev = SimpleNamespace(reason="unknown")
    assert _project(ev).type == "control"


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


def _send_audio(agent: _FakeBidiAgent, audio: bytes | str, **kwargs: Any) -> None:
    async def scenario():
        async with open_stream(_FakeEngine(agent), "voice", _session()) as stream:
            await stream.send_audio(audio, **kwargs)

    asyncio.run(scenario())


def test_send_audio_sends_raw_bytes_as_audio_delta():
    agent = _FakeBidiAgent(model=_FakeBidiModel())
    _send_audio(agent, b"\x00\x01", format="pcm")
    assert agent.sent == [AudioDelta(format="pcm", source={"bytes": b"\x00\x01"})]


def test_send_audio_decodes_base64_strings():
    agent = _FakeBidiAgent(model=_FakeBidiModel())
    _send_audio(agent, base64.b64encode(b"\x00\x01").decode())
    assert agent.sent == [AudioDelta(format="pcm", source={"bytes": b"\x00\x01"})]


def test_send_audio_rejects_a_str_that_is_not_base64():
    agent = _FakeBidiAgent(model=_FakeBidiModel())
    with pytest.raises(HarnessError, match="not base64"):
        _send_audio(agent, "not base64!")
    assert agent.sent == []


def test_send_audio_accepts_format_matching_the_model_input_stream():
    agent = _FakeBidiAgent(model=_FakeBidiModel())
    _send_audio(agent, b"\x00", sample_rate=16000, channels=1)
    assert len(agent.sent) == 1


@pytest.mark.parametrize("declared", [{"sample_rate": 24000}, {"channels": 2}])
def test_send_audio_rejects_format_not_matching_the_model_input_stream(declared):
    agent = _FakeBidiAgent(model=_FakeBidiModel())
    with pytest.raises(HarnessError, match="does not match"):
        _send_audio(agent, b"\x00", **declared)
    assert agent.sent == []


def test_send_audio_skips_the_check_when_the_model_has_no_audio_config():
    agent = _FakeBidiAgent(model=None)
    _send_audio(agent, b"\x00", sample_rate=48000, channels=2)
    assert len(agent.sent) == 1


def test_receive_yields_projected_events():
    events = [
        BidiTranscriptDeltaEvent(delta="hi", role="assistant", content_id="c-1"),
        BidiAudioDeltaEvent(
            audio="b64", format="pcm", sample_rate=24000, channels=1, content_id="c-2"
        ),
        BidiBargeInEvent(),
    ]
    agent = _FakeBidiAgent(events)

    async def scenario() -> list[StreamEvent]:
        async with open_stream(_FakeEngine(agent), "voice", _session()) as stream:
            return [ev async for ev in stream.receive()]

    out = asyncio.run(scenario())
    assert [e.type for e in out] == ["text", "audio", "control"]
