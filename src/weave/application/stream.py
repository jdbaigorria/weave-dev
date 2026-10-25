"""weave.application.stream — Full-duplex (bidi) streaming over a BidiAgent.

The transport-agnostic, generic half of bidi: open a stream over a built
``BidiAgent`` and exchange events. Transport plumbing (a ``pump`` over a WebSocket,
the Twilio/SIP protocol, audio transcoding) is **not** here — it lives in channel
adapters that wrap this. ``send_audio`` speaks the *model's* audio format; the
channel adapter transcodes (e.g. μ-law 8k ↔ PCM 16k) before calling it.

``StreamEvent`` is a fine projection (like ``AgentReply``) of a Strands bidi output
event, with a ``raw`` escape hatch. Barge-in (real-time interruption) arrives as a
``control`` event — distinct from HITL interrupt/resume.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from weave.application.errors import HarnessError
from weave.ports.agent_engine import AgentEngine


class StreamEvent(BaseModel):
    """One output event from a bidi turn, normalized to a small union."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    type: Literal["text", "audio", "tool_call", "control"]
    text: str | None = None
    audio: str | None = None
    tool_call: Any | None = None
    control: Any | None = None
    raw: Any = Field(default=None, exclude=True, repr=False)


# Incremental text: the model's own text and the transcript of user/assistant speech.
# The completed ``bidi_*_block`` events repeat these deltas, so they stay control.
_TEXT_EVENTS = frozenset({"bidi_text_delta", "bidi_transcript_delta"})


def _project(event: Any) -> StreamEvent:
    """Map a Strands bidi output event to a :class:`StreamEvent`.

    Strands bidi events are dicts tagged by ``type``; dispatching on that tag (rather
    than isinstance) keeps this independent of the event classes; ``raw`` always
    carries the original.
    """
    kind = event.get("type") if isinstance(event, dict) else None
    if kind == "bidi_audio_delta":
        return StreamEvent(type="audio", audio=event["audio"], raw=event)
    if kind in _TEXT_EVENTS:
        return StreamEvent(type="text", text=event["delta"], raw=event)
    if kind == "bidi_tool_use_blocks":
        return StreamEvent(type="tool_call", tool_call=event, raw=event)
    # Lifecycle, usage, reasoning, completed blocks and barge-in all surface as control.
    return StreamEvent(type="control", control=event, raw=event)


class BidiStream:
    """Async context manager over a live ``BidiAgent`` connection (per-stream, not pooled).

    Usage::

        async with open_stream("voice", session) as stream:
            await stream.send_text("hello")
            async for event in stream.receive():
                ...

    ``__aenter__`` starts the connection; ``__aexit__`` stops it and runs
    ``agent.cleanup()`` (releasing tools/MCP). One stream = one connection.
    """

    def __init__(self, agent: Any, *, extras: dict[str, Any] | None = None) -> None:
        self._agent = agent
        self._extras = extras or {}

    async def __aenter__(self) -> BidiStream:
        await self._agent.start(invocation_state=self._extras)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        try:
            await self._agent.stop()
        finally:
            self._agent.cleanup()

    async def send_text(self, text: str) -> None:
        """Send a user text turn (BidiAgent accepts a plain string)."""
        await self._agent.send(text)

    async def send_audio(
        self,
        audio: bytes | str,
        *,
        format: Literal["pcm", "wav", "opus", "mp3"] = "pcm",
        sample_rate: int | None = None,
        channels: Literal[1, 2] | None = None,
    ) -> None:
        """Send an audio chunk in the *model's* format (transcoding is the channel's job).

        ``audio`` is raw bytes or a base64 string. Strands fixes the input sample rate
        and channel count on the bidi model, not per chunk: ``sample_rate``/``channels``
        only assert that the chunk matches the model's input stream.
        """
        try:
            from strands.bidi.types import AudioDelta
        except ImportError as exc:  # pragma: no cover - depends on installed Strands
            raise HarnessError("bidi audio needs a Strands build with strands.bidi.") from exc
        self._check_input_stream(sample_rate=sample_rate, channels=channels)
        if isinstance(audio, str):
            try:
                audio = base64.b64decode(audio, validate=True)
            except binascii.Error as exc:
                raise HarnessError(f"send_audio got a str that is not base64: {exc}") from exc
        await self._agent.send(AudioDelta(format=format, source={"bytes": audio}))

    def _check_input_stream(self, **declared: int | None) -> None:
        """Raise if a declared format value disagrees with the model's input stream."""
        get_audio_config = getattr(getattr(self._agent, "model", None), "get_audio_config", None)
        if get_audio_config is None:
            return
        expected = get_audio_config()["input"]
        for key, value in declared.items():
            if value is not None and value != expected[key]:
                raise HarnessError(
                    f"send_audio {key}={value} does not match the bidi model's input "
                    f"stream ({key}={expected[key]}); configure it on the model instead."
                )

    async def receive(self) -> AsyncIterator[StreamEvent]:
        """Yield projected output events until the connection closes."""
        async for event in self._agent.receive():
            yield _project(event)


def open_stream(
    engine: AgentEngine,
    name: str,
    session: Any,
    *,
    extras: dict[str, Any] | None = None,
    model_params: dict[str, Any] | None = None,
) -> BidiStream:
    """Build the bidi target ``name`` and return a :class:`BidiStream` async-CM over it.

    Building is synchronous; the connection opens on ``async with``. ``extras`` flow
    to the model as ``invocation_state`` when the connection starts. ``model_params``
    is the per-run model-params overlay (e.g. a Bedrock ``boto_session``).
    """
    agent = engine.build_agent(name, session, model_params=model_params)
    return BidiStream(agent, extras=extras)
