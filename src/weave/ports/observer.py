"""weave.ports.observer — Where finished turns and feedback are reported (metrics, audit, ...)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from weave.application.feedback import FeedbackEvent
    from weave.application.observe import TurnEvent


@runtime_checkable
class TurnObserver(Protocol):
    """Receives one :class:`~weave.application.observe.TurnEvent` per turn.

    **Synchronous on purpose:** it is called before the reply is returned. In Lambda the
    process freezes as soon as the handler returns, and anything emitted in the
    background after that point can be lost. An observer that needs the network (e.g.
    ``PutMetricData``) handles its own buffering; the EMF adapter only writes a line.

    An observer that raises does not change the turn's reply or exception: Weave logs a
    warning and calls the next one.
    """

    def on_turn(self, event: TurnEvent) -> None: ...


@runtime_checkable
class FeedbackObserver(Protocol):
    """Receives one :class:`~weave.application.feedback.FeedbackEvent` per feedback request.

    Same rules as :class:`TurnObserver`: synchronous, called in order, and one that
    raises is logged and skipped. An observer may implement both (``EmfTurnObserver``
    does); :func:`weave.configure` hands each event to the ones that have its method.
    """

    def on_feedback(self, event: FeedbackEvent) -> None: ...
