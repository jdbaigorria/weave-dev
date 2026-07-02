"""weave.ports.registry — Capability port for the conversation registry.

Optional, **metadata-only** index of *which* conversations exist per user. Strands
keys sessions by ``session_id`` alone — its ``Session`` record carries no ``user_id``
and there is no list-by-user — so the user→conversations relationship lives nowhere
Strands manages it. Yet it is intrinsic to an assistant ("show me my chats"), so the
harness offers it: record ``(user_id, session_id)`` pairs and list a user's
conversations.

It indexes metadata only (ids, timestamps, an opaque title/metadata the backend
supplies) — **never message content**. Strands stays the single source of truth for
the conversation itself; the registry only answers which sessions belong to a user.
Opening a listed conversation = loading its Strands session by ``session_id``, as
usual. The backend then builds its own domain list from these refs.

A *capability* the harness checks for (like :class:`~weave.ports.session.MutableSession`),
not a built-in: persistence is the backend's, wired in only if it wants the feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ConversationRef:
    """A user's conversation as the registry knows it — metadata, not content.

    ``created_at``/``updated_at`` are ISO-8601 strings (matching Strands' style);
    ``title`` and ``metadata`` are whatever the backend stored (the harness never
    fills them — titling is product/UX).
    """

    session_id: str
    user_id: str
    created_at: str
    updated_at: str
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ConversationRegistry(Protocol):
    """A metadata-only index of conversations per user (record / list / delete)."""

    def record(
        self,
        user_id: str,
        session_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Upsert the ``(user_id, session_id)`` entry.

        Sets ``created_at`` once (first call) and ``updated_at`` on every call, so a
        per-turn auto-record yields recency ordering for free. ``title``/``metadata``
        are stored opaquely; passing ``None`` **preserves** any previously stored
        value (so the per-turn record won't erase a title the backend set later).
        """
        ...

    def list(self, user_id: str) -> list[ConversationRef]:
        """Return the user's conversations, most-recently-updated first."""
        ...

    def delete(self, user_id: str, session_id: str) -> None:
        """Forget one conversation entry (the Strands session is deleted separately)."""
        ...
