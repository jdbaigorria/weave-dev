"""weave.application.errors — Harness execution errors.

Execution failures surface as a typed ``HarnessError`` (the reply carries no error
field — a run either returns an :class:`~weave.application.reply.AgentReply` or
raises). Build-time failures keep propagating Loom's own typed errors unchanged.

A request that resolves to no known target raises :class:`RoutingError` instead —
a client error, distinct from an execution failure.
"""

from __future__ import annotations


class HarnessError(Exception):
    """Raised when running a built target fails inside the harness."""


class RoutingError(Exception):
    """Raised when a request resolves to no known target.

    A *client* error (the turn never ran), deliberately **not** a
    :class:`HarnessError`: a channel routing by a request key maps this to
    *404 Not Found*, whereas a ``HarnessError`` (the agent ran and failed) → *502*.
    """


class RequestError(Exception):
    """Raised when a request is missing something the channel's session policy requires.

    A *client* error about **identity** (the turn never ran), the sibling of
    :class:`RoutingError`: the default strict session policy raises this when a request
    carries no ``session_id``. Adapters map it to *400 Bad Request*. A channel built
    with a minting policy (``mint_if_absent``) never raises it — it generates the id
    instead.
    """
