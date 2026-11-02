"""weave.ports.feedback — Where user feedback is kept."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from weave.application.feedback import FeedbackRecord


@runtime_checkable
class FeedbackStore(Protocol):
    """One :class:`~weave.application.feedback.FeedbackRecord` per ``(session_id, turn_id)``.

    ``put`` replaces the stored record: the last state wins. ``get`` returns ``None``
    only when there is none — any other failure must raise, or a stored feedback would
    be overwritten as if it did not exist.
    """

    def get(self, session_id: str, turn_id: str) -> FeedbackRecord | None: ...

    def put(self, record: FeedbackRecord) -> None: ...
