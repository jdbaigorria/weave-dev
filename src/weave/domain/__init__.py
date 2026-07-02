"""weave.domain — Pure conversation model. Zero external dependencies.

The heart of the harness: the branching conversation tree and the rules for how a
conversation evolves, branches, and is replayed. No I/O, no Loom, no Strands — just
data and logic, so it is trivially testable and the rest of Weave is built around it.

Public surface:

- :class:`ConversationTree` — the aggregate (turns + active branch pointer).
- :class:`Turn`, :class:`Message`, :class:`Interrupt` — the value objects.
- :class:`ConversationError` — invalid operation against a tree.
"""

from .conversation import ConversationTree, Interrupt, Message, Turn
from .errors import ConversationError

__all__ = [
    "ConversationError",
    "ConversationTree",
    "Interrupt",
    "Message",
    "Turn",
]
