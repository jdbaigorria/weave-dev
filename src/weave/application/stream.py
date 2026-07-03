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


def _project(event: Any) -> StreamEvent:
    """Map a Strands bidi output event to a :class:`StreamEvent` (duck-typed).

    Duck-typing (rather than isinstance) keeps this independent of the experimental
    event classes and trivially testable; ``raw`` always carries the original.
    """
    if hasattr(event, "audio") and hasattr(event, "sample_rate"):
        return StreamEvent(type="audio", audio=event.audio, raw=event)
    if hasattr(event, "text") and hasattr(event, "is_final"):
        return StreamEvent(type="text", text=event.text, raw=event)
    if "ToolUse" in type(event).__name__:
        return StreamEvent(type="tool_call", tool_call=event, raw=event)
    # Lifecycle, usage, errors and barge-in (interruption) all surface as control.
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
        audio: str,
        *,
        format: Literal["pcm", "wav", "opus", "mp3"] = "pcm",
        sample_rate: Literal[16000, 24000, 48000] = 16000,
        channels: Literal[1, 2] = 1,
    ) -> None:
        """Send an audio chunk in the *model's* format (transcoding is the channel's job)."""
        try:
            from strands.experimental.bidi import BidiAudioInputEvent
        except ImportError as exc:  # pragma: no cover - depends on installed Strands
            raise HarnessError(
                "bidi audio needs a Strands build with strands.experimental.bidi."
            ) from exc
        await self._agent.send(
            BidiAudioInputEvent(
                audio=audio, format=format, sample_rate=sample_rate, channels=channels
            )
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
