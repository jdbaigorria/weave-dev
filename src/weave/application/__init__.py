"""weave.application — Use cases. Depend only on ports, never on adapters.

Orchestrates the domain to fulfil harness operations: ``start_conversation``,
``send_turn``, ``branch``, ``resume``, ``export``. Each use case talks to the
outside world exclusively through :mod:`weave.ports`, so concrete storage/engine/
channel choices are swappable without touching this layer.

Design note: the turn/channel use cases are async/streaming from day one — voice
needs token streaming (Strands provides the bidi primitive; Weave routes it), and
wrapping that in a sync request/response later would throw the streaming away.
"""
