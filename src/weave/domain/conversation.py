"""weave.domain.conversation — The branching conversation model.

Pure domain (stdlib only). Models a conversation as a **tree of turns** and answers
the harness's central question (architecture decisions §15): *how is the history of
the active branch reconstructed and handed to the agent each turn?* →
:meth:`ConversationTree.history`.

Shape:

- A :class:`Message` is an opaque ``(role, content)`` pair — the domain orders
  messages but never inspects ``content`` (Strands content blocks live there, kept
  out of the domain so it stays framework-free).
- A :class:`Turn` is one exchange (the branch node): the messages produced in a
  single round (user input, any tool-use/tool-result, the assistant's reply). It
  links to its ``parent_id``; roots are the turns with ``parent_id is None``.
- A :class:`ConversationTree` is the mutable aggregate: the set of turns plus the
  ``active_leaf_id`` marking the current branch. Branching is just turns that share
  a parent (siblings) — "redo this turn differently".

The agent's context for the next turn is ``history(active_leaf_id)``: the flattened
messages along the path root → active leaf.

Interrupt/resume lifecycle: a turn may pause mid-round awaiting a human-in-the-loop
:class:`Interrupt` (e.g. tool approval). Its ``messages`` hold the partial round up
to the pause; the branch is then *blocked* (:attr:`ConversationTree.is_awaiting_interrupt`)
and cannot grow via :meth:`~ConversationTree.append` until
:meth:`~ConversationTree.resume` folds the continuation into the *same* turn — resume
finishes the round in place, it does not start a new turn. The human's decision is
carried by the continuation messages (e.g. the tool-result), not by a separate
domain object: the domain treats interrupts as data and does not drive the flow.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .errors import ConversationError


def _new_id() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class Message:
    """An opaque ``(role, content)`` message. ``content`` is not interpreted here."""

    role: str
    content: Any

    @classmethod
    def user(cls, content: Any) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: Any) -> Message:
        return cls(role="assistant", content=content)


@dataclass(frozen=True, slots=True)
class Interrupt:
    """A pending human-in-the-loop interrupt raised during a turn.

    Carried by a turn so the application/harness can resume it; the domain treats
    it as data and does not drive the resume flow.
    """

    id: str
    name: str = ""
    reason: Any = None


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange — the unit of branching. Immutable once created.

    ``messages`` is the full ordered sequence produced this round (so replaying a
    branch reconstructs the agent's exact context). ``parent_id is None`` marks a
    root turn.
    """

    id: str
    parent_id: str | None
    messages: tuple[Message, ...]
    created_at: datetime
    interrupts: tuple[Interrupt, ...] = ()

    @property
    def is_interrupted(self) -> bool:
        """True if the turn paused awaiting interrupt responses."""
        return bool(self.interrupts)


@dataclass
class ConversationTree:
    """A branching conversation: turns + the active branch pointer.

    Mutable aggregate (a ``StateRepository`` snapshots it). Roots are the turns
    whose ``parent_id is None``; ``active_leaf_id`` is the turn the next ``append``
    continues from and whose path ``history`` reconstructs.
    """

    id: str = field(default_factory=_new_id)
    turns: dict[str, Turn] = field(default_factory=dict)
    active_leaf_id: str | None = None

    # -- mutating operations ------------------------------------------------

    def append(self, messages: Iterable[Message], *, interrupts: Iterable[Interrupt] = ()) -> Turn:
        """Extend the active branch with a new turn; it becomes the active leaf.

        Refuses to grow a branch whose active turn is still awaiting an interrupt —
        finish it with :meth:`resume`, or abandon it with :meth:`branch_from`.
        """
        leaf = self.active_turn
        if leaf is not None and leaf.is_interrupted:
            raise ConversationError(
                f"Active turn '{leaf.id}' is awaiting an interrupt; resume or branch_from it."
            )
        return self._add(parent_id=self.active_leaf_id, messages=messages, interrupts=interrupts)

    def resume(self, messages: Iterable[Message], *, interrupts: Iterable[Interrupt] = ()) -> Turn:
        """Finish the interrupted active turn by folding in the continuation ``messages``.

        The active leaf must be awaiting an interrupt. The continuation (what the agent
        produced once the human answered) is appended to that turn's messages *in place*
        — same id, parent, and ``created_at`` — so the round completes as one turn rather
        than spawning a new one. ``interrupts`` clears to ``()`` when fully resolved, or
        carries a fresh set if the continuation paused again (re-interrupt), leaving the
        branch awaiting another resume.
        """
        leaf = self.active_turn
        if leaf is None:
            raise ConversationError("No active turn to resume.")
        if not leaf.is_interrupted:
            raise ConversationError(f"Turn '{leaf.id}' is not awaiting an interrupt.")
        resumed = Turn(
            id=leaf.id,
            parent_id=leaf.parent_id,
            messages=leaf.messages + tuple(messages),
            created_at=leaf.created_at,
            interrupts=tuple(interrupts),
        )
        self.turns[leaf.id] = resumed
        return resumed

    def branch_from(
        self, turn_id: str, messages: Iterable[Message], *, interrupts: Iterable[Interrupt] = ()
    ) -> Turn:
        """Add an alternate continuation as a *sibling* of ``turn_id``.

        The new turn shares ``turn_id``'s parent ("redo this turn differently") and
        becomes the active leaf. ``turn_id`` and its subtree are left intact.
        """
        base = self._require(turn_id)
        return self._add(parent_id=base.parent_id, messages=messages, interrupts=interrupts)

    def switch_to(self, turn_id: str) -> None:
        """Move the active pointer to an existing turn (navigate between branches)."""
        self._require(turn_id)
        self.active_leaf_id = turn_id

    def _add(
        self,
        *,
        parent_id: str | None,
        messages: Iterable[Message],
        interrupts: Iterable[Interrupt],
    ) -> Turn:
        turn = Turn(
            id=_new_id(),
            parent_id=parent_id,
            messages=tuple(messages),
            created_at=_now(),
            interrupts=tuple(interrupts),
        )
        self.turns[turn.id] = turn
        self.active_leaf_id = turn.id
        return turn

    # -- navigation / reconstruction ---------------------------------------

    def path(self, turn_id: str | None = None) -> list[Turn]:
        """Return the turns from a root down to ``turn_id`` (default: active leaf)."""
        tid = turn_id if turn_id is not None else self.active_leaf_id
        chain: list[Turn] = []
        while tid is not None:
            turn = self._require(tid)
            chain.append(turn)
            tid = turn.parent_id
        chain.reverse()
        return chain

    def history(self, turn_id: str | None = None) -> list[Message]:
        """Flattened messages along the path root → ``turn_id`` (default: active leaf).

        This is what the harness hands the agent as its context for the next turn —
        the reconstruction of the active branch's history.
        """
        return [m for turn in self.path(turn_id) for m in turn.messages]

    def children(self, turn_id: str | None) -> list[Turn]:
        """Direct children of ``turn_id`` (``None`` → the root turns)."""
        return [t for t in self.turns.values() if t.parent_id == turn_id]

    def roots(self) -> list[Turn]:
        """Turns with no parent (alternate conversation starts)."""
        return self.children(None)

    def leaves(self) -> list[Turn]:
        """Turns with no children (the tip of each branch)."""
        parents = {t.parent_id for t in self.turns.values()}
        return [t for t in self.turns.values() if t.id not in parents]

    def get(self, turn_id: str) -> Turn | None:
        """Return a turn by id, or ``None`` if absent."""
        return self.turns.get(turn_id)

    @property
    def active_turn(self) -> Turn | None:
        """The turn at the active leaf, or ``None`` for an empty tree."""
        return self.turns.get(self.active_leaf_id) if self.active_leaf_id is not None else None

    @property
    def is_awaiting_interrupt(self) -> bool:
        """True if the active branch is paused on an interrupted turn (needs ``resume``)."""
        leaf = self.active_turn
        return leaf is not None and leaf.is_interrupted

    def _require(self, turn_id: str) -> Turn:
        turn = self.turns.get(turn_id)
        if turn is None:
            raise ConversationError(f"Turn '{turn_id}' is not in this conversation.")
        return turn
