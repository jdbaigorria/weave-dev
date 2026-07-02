"""weave.application.conversations — Use case: list a user's conversations.

The read side of the conversation registry. A thin, validated passthrough of the
backend-provided :class:`~weave.ports.registry.ConversationRegistry` (writes happen
automatically per turn through the chat channel; deletes are a direct port call).
The harness returns the metadata refs; the backend builds its own domain list and,
on click, opens the chosen conversation by its Strands ``session_id``.
"""

from __future__ import annotations

from weave.application.errors import HarnessError
from weave.ports.registry import ConversationRef, ConversationRegistry


def list_conversations(registry: ConversationRegistry, user_id: str) -> list[ConversationRef]:
    """Return ``user_id``'s conversations (most-recently-updated first).

    ``registry`` is the backend's :class:`~weave.ports.registry.ConversationRegistry`
    (the conversation index is opt-in — Strands has no per-user list of its own).
    """
    if not isinstance(registry, ConversationRegistry):
        raise HarnessError(
            "list_conversations needs a ConversationRegistry (implementing record/list/delete)."
        )
    return list(registry.list(user_id))
