"""weave.domain.errors — Domain-level errors (pure, no framework coupling)."""

from __future__ import annotations


class ConversationError(Exception):
    """Raised on an invalid operation against a ``ConversationTree``.

    e.g. referencing a turn id that does not exist in the tree.
    """
