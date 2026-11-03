"""weave.application.feedback — A user's rating of one turn.

Feedback rates a turn, so it shares the turn's id (:mod:`weave.application.turn_id`),
identity, target and metric dimensions. It builds no agent: :func:`submit_feedback`
cleans what the product parsed, merges it with what was stored for the turn (the
thumb first, the panel with issues and comment after), saves it and tells the
observers **what changed**, so each thing is counted once.

What is the product's: the issue types (per target, through :data:`PolicyFor`), the
HTTP field names and where it is stored (:class:`~weave.ports.feedback.FeedbackStore`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from loom import LoomSession
from pydantic import BaseModel, ConfigDict, Field, field_validator

from weave.application.turn_id import valid_turn_id

if TYPE_CHECKING:
    from weave.ports.feedback import FeedbackStore
    from weave.ports.observer import FeedbackObserver

logger = logging.getLogger(__name__)

Rating = Literal["up", "down"]

_ISSUE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class IssueType(BaseModel):
    """A kind of problem: ``id`` is what is stored and counted, ``label`` what users see."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ISSUE_ID.fullmatch(value):
            raise ValueError(f"issue id {value!r}: expected ^[a-z][a-z0-9_]{{0,63}}$")
        return value


class FeedbackPolicy(BaseModel):
    """The product's policy for one target.

    ``issue_types`` in display order; empty means no issue panel (thumb and comment only).
    Renaming a ``label`` keeps the metrics comparable; changing an ``id`` is a new issue.
    """

    model_config = ConfigDict(frozen=True)

    issue_types: tuple[IssueType, ...] = ()
    max_comment: int = Field(default=2000, gt=0)

    @field_validator("issue_types")
    @classmethod
    def _unique(cls, value: tuple[IssueType, ...]) -> tuple[IssueType, ...]:
        ids = [issue.id for issue in value]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"duplicated issue ids: {duplicated}")
        return value

    @property
    def issue_ids(self) -> frozenset[str]:
        return frozenset(issue.id for issue in self.issue_types)


#: ``target -> policy``, called on every request so the product can vary it per agent.
PolicyFor = Callable[[str], FeedbackPolicy]


class Feedback(BaseModel):
    """What the product parsed from a feedback request."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    turn_id: str
    user_id: str | None = None
    rating: Rating
    issues: tuple[str, ...] = ()
    comment: str | None = None
    extras: dict[str, Any] | None = None  # e.g. headers, to route like the chat

    @field_validator("turn_id")
    @classmethod
    def _check_turn_id(cls, value: str) -> str:
        if not valid_turn_id(value):
            raise ValueError(f"invalid turn_id {value!r}: expected 1-64 of [A-Za-z0-9_-].")
        return value

    @field_validator("session_id")
    @classmethod
    def _check_session_id(cls, value: str) -> str:
        # It becomes part of a storage key: no separators, bounded length.
        if not _SESSION_ID.fullmatch(value):
            raise ValueError(f"invalid session_id {value!r}: expected 1-128 of [A-Za-z0-9_.:-].")
        return value


class FeedbackRecord(BaseModel):
    """The stored state: one per ``(session_id, turn_id)``, the last write wins."""

    session_id: str
    turn_id: str
    target: str
    user_id: str
    is_anonymous: bool
    rating: Rating
    issues: tuple[str, ...] = ()
    comment: str | None = None
    created_at: datetime
    updated_at: datetime


class FeedbackEvent(BaseModel):
    """What changed with one feedback request — what observers count.

    It carries no comment text: free text may hold personal data (an id number, a
    phone), so it only lives in the store.
    """

    model_config = ConfigDict(frozen=True)

    target: str
    session_id: str
    user_id: str
    is_anonymous: bool
    turn_id: str
    rating: Rating
    first: bool
    previous_rating: Rating | None = None
    new_issues: tuple[str, ...] = ()
    comment_added: bool = False


def submit_feedback(
    store: FeedbackStore,
    feedback: Feedback,
    *,
    target: str,
    session: LoomSession,
    policy: FeedbackPolicy,
    observers: Sequence[FeedbackObserver] = (),
) -> FeedbackRecord:
    """Clean, merge with the stored state, save, and tell ``observers`` what changed.

    Never rejects a feedback for its content: an unknown issue is dropped and a long
    comment cut, both with a ``WARNING`` — losing the thumb is worse than losing an
    issue. The second request of a two-step flow completes the first: ``rating`` and
    ``issues`` are replaced (the panel sends the full set), ``comment`` is kept when the
    new one is ``None``, and the turn keeps the ``target`` it was first rated under.
    """
    issues = _clean_issues(feedback, policy)
    comment = _clean_comment(feedback, policy)
    previous = store.get(feedback.session_id, feedback.turn_id)
    now = datetime.now(UTC)
    record = FeedbackRecord(
        session_id=feedback.session_id,
        turn_id=feedback.turn_id,
        target=previous.target if previous else target,
        user_id=session.user_id,
        is_anonymous=session.is_anonymous,
        rating=feedback.rating,
        issues=issues,
        comment=comment if comment is not None else (previous.comment if previous else None),
        created_at=previous.created_at if previous else now,
        updated_at=now,
    )
    store.put(record)
    if observers:
        _notify(observers, _event(record, previous))
    return record


def _clean_issues(feedback: Feedback, policy: FeedbackPolicy) -> tuple[str, ...]:
    known = policy.issue_ids
    unknown = sorted({i for i in feedback.issues if i not in known})
    if unknown:
        logger.warning("dropped unknown feedback issues %s (turn=%s)", unknown, feedback.turn_id)
    return tuple(dict.fromkeys(i for i in feedback.issues if i in known))


def _clean_comment(feedback: Feedback, policy: FeedbackPolicy) -> str | None:
    if feedback.comment is None or not feedback.comment.strip():
        return None
    comment = feedback.comment.strip()
    if len(comment) > policy.max_comment:
        logger.warning(
            "feedback comment cut from %d to %d chars (turn=%s)",
            len(comment),
            policy.max_comment,
            feedback.turn_id,
        )
        comment = comment[: policy.max_comment]
    return comment


def _event(record: FeedbackRecord, previous: FeedbackRecord | None) -> FeedbackEvent:
    before = set(previous.issues) if previous else set()
    return FeedbackEvent(
        target=record.target,
        session_id=record.session_id,
        user_id=record.user_id,
        is_anonymous=record.is_anonymous,
        turn_id=record.turn_id,
        rating=record.rating,
        first=previous is None,
        previous_rating=previous.rating if previous else None,
        new_issues=tuple(i for i in record.issues if i not in before),
        comment_added=record.comment is not None and (previous is None or previous.comment is None),
    )


def _notify(observers: Sequence[FeedbackObserver], event: FeedbackEvent) -> None:
    for observer in observers:
        try:
            observer.on_feedback(event)
        except Exception:
            logger.warning(
                "feedback observer %s failed (target=%s)",
                type(observer).__name__,
                event.target,
                exc_info=True,
            )
