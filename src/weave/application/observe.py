"""weave.application.observe — What happened in one turn, told to the observers.

A turn is the use case's window: build → invoke → teardown. :class:`TurnEvent` is the
immutable record of it and :func:`observe_turn` the context manager every use case
wraps itself in once, so the timing and the observer rules live in one place:

1. Observers are called in the order they were configured, **once per turn**.
2. An observer that raises changes neither the reply nor the exception: it is logged
   as a ``WARNING`` here and the next observer still runs.
3. With no observers no event is built (zero cost).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal

from loom import LoomSession, ModelSettings
from pydantic import BaseModel, ConfigDict

from weave.application.reply import Usage

if TYPE_CHECKING:
    from weave.ports.observer import TurnObserver

logger = logging.getLogger(__name__)

Outcome = Literal["ok", "error", "cancelled"]


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
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    target: str
    kind: Literal["agent", "workflow"]
    operation: Literal["run", "stream", "fork"]
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


class TurnClock:
    """The running turn: :func:`observe_turn` hands it to the use case."""

    def __init__(self, *, active: bool = True) -> None:
        self._start = time.perf_counter()
        self.active = active
        self.build_ms: float | None = None
        self.reply: Any = None
        self.description: AgentDescription | None = None

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

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0


@contextmanager
def observe_turn(
    observers: Sequence[TurnObserver],
    *,
    target: str,
    kind: Literal["agent", "workflow"],
    operation: Literal["run", "stream", "fork"],
    session: LoomSession,
) -> Iterator[TurnClock]:
    """Time one turn and tell ``observers`` how it ended. Re-raises whatever the turn raised.

    The window closes when the ``with`` block exits, so the teardown inside it counts.
    ``GeneratorExit`` / ``CancelledError`` before :meth:`TurnClock.done` is ``cancelled``;
    after it, the turn already answered and stays ``ok``.
    """
    clock = TurnClock(active=bool(observers))
    try:
        yield clock
    except BaseException as exc:
        if observers:
            if clock.reply is not None and isinstance(exc, GeneratorExit | asyncio.CancelledError):
                _notify(observers, _event(clock, "ok", target, kind, operation, session))
            elif isinstance(exc, GeneratorExit | asyncio.CancelledError):
                _notify(observers, _event(clock, "cancelled", target, kind, operation, session))
            else:
                _notify(
                    observers, _event(clock, "error", target, kind, operation, session, error=exc)
                )
        raise
    if observers:
        _notify(observers, _event(clock, "ok", target, kind, operation, session))


def _event(
    clock: TurnClock,
    outcome: Outcome,
    target: str,
    kind: Literal["agent", "workflow"],
    operation: Literal["run", "stream", "fork"],
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
