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

from loom import LoomSession
from pydantic import BaseModel, ConfigDict

from weave.application.reply import Usage

if TYPE_CHECKING:
    from weave.ports.observer import TurnObserver

logger = logging.getLogger(__name__)

Outcome = Literal["ok", "error", "cancelled"]


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

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self.build_ms: float | None = None
        self.reply: Any = None

    def built(self) -> None:
        """Mark the end of the build."""
        self.build_ms = self.elapsed_ms()

    def done(self, reply: Any) -> None:
        """Record the reply: from here on, the turn is ``ok`` even if the stream is closed."""
        self.reply = reply

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
    clock = TurnClock()
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
