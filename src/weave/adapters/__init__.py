"""weave.adapters — Concrete implementations of :mod:`weave.ports`.

- ``driven`` — Weave calls out: the Loom engine, state repositories, asset
  stores, config sources, metrics sinks.
- ``driving`` — the outside calls in: chat/voice channels that translate external
  input into use-case invocations.

This is the only layer allowed to touch vendor SDKs and Loom's runnable output.
It uses Loom through its public API (``loom.build`` etc.); reaching into
``loom.core``/``loom.adapters`` is banned (see the ruff boundary rule).
"""
