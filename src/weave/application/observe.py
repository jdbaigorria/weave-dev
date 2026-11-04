"""weave.application.observe — What happened in one turn, told to the observers.

A turn is the use case's window: build → invoke → teardown. :class:`TurnEvent` is the
immutable record of it and :func:`observe_turn` the context manager every use case
wraps itself in once, so the timing and the observer rules live in one place:

1. Observers are called in the order they were configured, **once per turn**.
2. An observer that raises changes neither the reply nor the exception: it is logged
   as a ``WARNING`` here and the next observer still runs.
3. With no observers no event is built (zero cost).

Each observer hears what its method asks for: ``on_turn`` gets the :class:`TurnEvent`
(no content), ``on_transcript`` the :class:`TurnTranscript` of an agent turn (W22) —
question, answer and tool calls. The turn's messages are read only when some observer
has ``on_transcript``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from loom import LoomSession, ModelSettings
from pydantic import BaseModel, ConfigDict

from weave.application.reply import Usage
from weave.application.transcript import ToolCall, question_of, tool_calls_of, turn_messages

if TYPE_CHECKING:
    from weave.ports.observer import AnyTurnObserver, TranscriptObserver, TurnObserver

logger = logging.getLogger(__name__)

Outcome = Literal["ok", "error", "cancelled"]
Operation = Literal["run", "stream", "fork", "regenerate"]


class AgentDescription(BaseModel):
    """Which model a built agent answered with — what an engine's ``describe`` returns.

    ``model_settings`` is Loom's whitelist (:class:`loom.ModelSettings`); ``variant`` the
    agent's ``metrics.variant`` label.
    """

    model_config = ConfigDict(frozen=True)

    model_id: str | None = None
    model_settings: ModelSettings | None = None
    model_fallbacks: int = 0
    variant: str | None = None


class TurnEvent(BaseModel):
    """One turn as Weave saw it.

    ``duration_ms`` is the use case's wall clock **including the build**; compare it with
    ``usage.latency_ms`` (what Strands measured inside the agent) to see what rebuilding
    the agent per request costs. ``build_ms`` is ``None`` when the build did not finish.
    ``usage`` and ``stop_reason`` are set only on ``ok``; ``error`` only on ``error``.
    ``turn_id`` is set on every outcome (the id exists before the build); ``replaces``
    only on a ``regenerate``.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    target: str
    kind: Literal["agent", "workflow"]
    operation: Operation
    session_id: str
    user_id: str
    is_anonymous: bool
    outcome: Outcome
    duration_ms: float
    build_ms: float | None = None
    usage: Usage | None = None
    stop_reason: str | None = None
    error: BaseException | None = None
    model_id: str | None = None
    model_settings: ModelSettings | None = None
    model_fallbacks: int = 0
    variant: str | None = None
    turn_id: str | None = None
    replaces: str | None = None

    @property
    def error_type(self) -> str | None:
        """Class name of the innermost cause of ``error`` (follows ``__cause__``)."""
        if self.error is None:
            return None
        current = self.error
        seen = {id(current)}
        while current.__cause__ is not None and id(current.__cause__) not in seen:
            current = current.__cause__
            seen.add(id(current))
        return type(current).__name__


class TurnTranscript(BaseModel):
    """One agent turn with its content — what a :class:`~weave.ports.observer.TranscriptObserver` stores.

    The same identity and outcome as :class:`TurnEvent`, plus what was said:
    ``question`` is the text of the message that opens the turn (read from the input, so
    it exists even when the build fails) and ``attachments`` the type of each block that
    is not text (``"image/png"``; the bytes are never copied). ``answer`` is the reply's
    text, ``None`` unless the turn ended ``ok``. ``tool_calls`` come from the turn's
    messages, read before the agent's teardown; empty when the agent was never built.
    """

    model_config = ConfigDict(frozen=True)

    turn_id: str
    session_id: str
    user_id: str
    is_anonymous: bool
    target: str
    operation: Operation
    replaces: str | None = None
    outcome: Outcome
    error_type: str | None = None
    started_at: datetime
    duration_ms: float
    model_id: str | None = None
    usage: Usage | None = None
    question: str
    attachments: tuple[str, ...] = ()
    answer: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class TurnClock:
    """The running turn: :func:`observe_turn` hands it to the use case."""

    def __init__(self, *, active: bool = True, transcripts: bool = False) -> None:
        self._start = time.perf_counter()
        self.started_at = datetime.now(UTC)
        self.active = active
        self.transcripts = transcripts
        self.build_ms: float | None = None
        self.reply: Any = None
        self.description: AgentDescription | None = None
        self.turn_id: str | None = None
        self.replaces: str | None = None
        self.input: Any = None
        self.tool_calls: tuple[ToolCall, ...] = ()

    def built(self) -> None:
        """Mark the end of the build."""
        self.build_ms = self.elapsed_ms()

    def done(self, reply: Any) -> None:
        """Record the reply: from here on, the turn is ``ok`` even if the stream is closed."""
        self.reply = reply

    def describe(self, engine: Any, agent: Any) -> None:
        """Ask the engine which model ``agent`` answered with, before its teardown.

        Optional capability: an engine without ``describe`` leaves the model empty. One
        that raises is logged and ignored. Skipped when no observer will read it.
        """
        describe = getattr(engine, "describe", None)
        if not self.active or not callable(describe):
            return
        try:
            self.description = describe(agent)
        except Exception:
            logger.warning("engine describe failed; the turn reports no model", exc_info=True)

    def capture(self, agent: Any) -> None:
        """Read the turn's tool calls from ``agent.messages``, before its teardown.

        Skipped when no observer has ``on_transcript``: the messages are not even read.
        A failure is logged and leaves ``tool_calls`` empty; the transcript is still sent.
        """
        if not self.transcripts or self.turn_id is None:
            return
        try:
            self.tool_calls = tool_calls_of(turn_messages(agent.messages, self.turn_id))
        except Exception:
            logger.warning(
                "could not read the turn's messages; the transcript has no tools", exc_info=True
            )

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0


@contextmanager
def observe_turn(
    observers: Sequence[AnyTurnObserver],
    *,
    target: str,
    kind: Literal["agent", "workflow"],
    operation: Operation,
    session: LoomSession,
    turn_id: str | None = None,
    replaces: str | None = None,
    input: Any = None,
) -> Iterator[TurnClock]:
    """Time one turn and tell ``observers`` how it ended. Re-raises whatever the turn raised.

    The window closes when the ``with`` block exits, so the teardown inside it counts.
    ``GeneratorExit`` / ``CancelledError`` before :meth:`TurnClock.done` is ``cancelled``;
    after it, the turn already answered and stays ``ok``.

    ``input`` is the turn's input as the caller gave it: the transcript's question. Only
    agent turns with a ``turn_id`` have a transcript (a workflow is not stamped).
    """
    # Split by method, not by type: an observer may implement both.
    turn_observers = [
        cast("TurnObserver", o) for o in observers if callable(getattr(o, "on_turn", None))
    ]
    transcript_observers = (
        [
            cast("TranscriptObserver", o)
            for o in observers
            if callable(getattr(o, "on_transcript", None))
        ]
        if kind == "agent" and turn_id is not None
        else []
    )
    clock = TurnClock(active=bool(observers), transcripts=bool(transcript_observers))
    clock.turn_id = turn_id
    clock.replaces = replaces
    clock.input = input

    def finish(outcome: Outcome, error: BaseException | None = None) -> None:
        event = _event(clock, outcome, target, kind, operation, session, error=error)
        _notify(turn_observers, event)
        if transcript_observers:
            _notify_transcripts(transcript_observers, clock, event)

    try:
        yield clock
    except BaseException as exc:
        if observers:
            if clock.reply is not None and isinstance(exc, GeneratorExit | asyncio.CancelledError):
                finish("ok")
            elif isinstance(exc, GeneratorExit | asyncio.CancelledError):
                finish("cancelled")
            else:
                finish("error", exc)
        raise
    if observers:
        finish("ok")


def _event(
    clock: TurnClock,
    outcome: Outcome,
    target: str,
    kind: Literal["agent", "workflow"],
    operation: Operation,
    session: LoomSession,
    *,
    error: BaseException | None = None,
) -> TurnEvent:
    reply = clock.reply if outcome == "ok" else None
    description = clock.description or AgentDescription()
    return TurnEvent(
        target=target,
        kind=kind,
        operation=operation,
        session_id=session.session_id,
        user_id=session.user_id,
        is_anonymous=session.is_anonymous,
        outcome=outcome,
        duration_ms=clock.elapsed_ms(),
        build_ms=clock.build_ms,
        usage=getattr(reply, "usage", None),
        stop_reason=getattr(reply, "stop_reason", None),
        error=error,
        model_id=description.model_id,
        model_settings=description.model_settings,
        model_fallbacks=description.model_fallbacks,
        variant=description.variant,
        turn_id=clock.turn_id,
        replaces=clock.replaces,
    )


def _transcript(clock: TurnClock, event: TurnEvent) -> TurnTranscript:
    question, attachments = question_of(clock.input)
    reply = clock.reply if event.outcome == "ok" else None
    return TurnTranscript(
        turn_id=str(event.turn_id),
        session_id=event.session_id,
        user_id=event.user_id,
        is_anonymous=event.is_anonymous,
        target=event.target,
        operation=event.operation,
        replaces=event.replaces,
        outcome=event.outcome,
        error_type=event.error_type,
        started_at=clock.started_at,
        duration_ms=event.duration_ms,
        model_id=event.model_id,
        usage=event.usage,
        question=question,
        attachments=attachments,
        answer=getattr(reply, "text", None),
        tool_calls=clock.tool_calls,
    )


def _notify_transcripts(
    observers: Sequence[TranscriptObserver], clock: TurnClock, event: TurnEvent
) -> None:
    try:
        transcript = _transcript(clock, event)
    except Exception:
        logger.warning(
            "could not build the turn transcript (target=%s)", event.target, exc_info=True
        )
        return
    for observer in observers:
        try:
            observer.on_transcript(transcript)
        except Exception:
            logger.warning(
                "transcript observer %s failed (target=%s)",
                type(observer).__name__,
                event.target,
                exc_info=True,
            )


def _notify(observers: Sequence[TurnObserver], event: TurnEvent) -> None:
    for observer in observers:
        try:
            observer.on_turn(event)
        except Exception:
            logger.warning(
                "turn observer %s failed (target=%s)",
                type(observer).__name__,
                event.target,
                exc_info=True,
            )
