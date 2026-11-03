"""weave.ports.session — Capability port for editable (mutable) session history.

Strands sessions are **append-only** (you can append and redact-the-latest, but not
delete/truncate by index). A backend that wants to support edit / regenerate /
explore (``fork``) implements this small capability on top of its Strands
``SessionManager``: the ability to rewind the persisted conversation to a point.

This is a *capability* the harness checks for, not a full session abstraction —
Strands' SessionManager still owns persistence; this only adds truncation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MutableSession(Protocol):
    """A session store that can truncate persisted history (for fork/edit)."""

    def truncate(self, session_id: str, *, keep: int, agent_id: str) -> None:
        """Delete persisted messages so only the first ``keep`` remain (ids 0..keep-1).

        ``keep`` may be negative (counted from the end, like a list slice): ``-1``
        keeps all but the last, ``0`` clears the history.

        ``agent_id`` is required: the caller (``fork``) decides which agent folder to
        rewind; a store has no way to know it.

        .. warning::

            **Serialize calls per ``session_id``.** Truncation rewrites the
            append-only session destructively, and Strands enforces sequentiality
            only *per agent instance* — not across the instances that the
            rebuild-per-request model creates for one session. A truncate racing a
            concurrent turn (a :func:`~weave.application.run.fork` or ``run_agent``)
            on the same ``session_id`` corrupts the session out from under the other
            turn. Weave imposes no locking; the backend must serialize concurrent
            turns on a conversation (e.g. a per-``session_id`` lock or queue).
        """
        ...


@runtime_checkable
class RegenerableSession(MutableSession, Protocol):
    """A :class:`MutableSession` that can also find where a turn starts (for ``regenerate``).

    A separate capability so that a store written for ``fork`` keeps satisfying
    :class:`MutableSession` unchanged.
    """

    def turn_start(self, session_id: str, *, agent_id: str, turn_id: str) -> int | None:
        """``message_id`` of the user message that opened ``turn_id`` — only if it is the
        **last** turn of ``agent_id``'s history.

        Searches from the end for the first message stamped with a turn id
        (``metadata.custom["weave.turn_id"]``): if it is ``turn_id``, returns its
        ``message_id``; if it is another one, or there is none, ``None``. Searching from
        the end keeps it cheap: the last turn is a few messages from the end.
        """
        ...
