"""weave.ports — Abstract interfaces the use cases depend on (Protocols).

Each port is a capability the application needs, with no commitment to a concrete
implementation (those live in :mod:`weave.adapters`):

- ``AgentEngine`` — builds/runs an agent. The seam over Loom (``loom.build``);
  also lets the domain be tested with a mocked engine.
- ``StateRepository`` — persists the conversation tree (Postgres/Dynamo/...).
- ``AssetStore`` — user assets (S3/local/...).
- ``Channel`` — driving side: chat/voice transport (async/streaming).
- ``TurnObserver`` / ``FeedbackObserver`` / ``TranscriptObserver`` — told how each
  turn ended, how users rated it and what was said in it (metrics, audit); see
  ``observer``.
- ``FeedbackStore`` — where user feedback is kept; see ``feedback``.
- ``ConfigSource`` — where agent definitions come from (re-uses Loom's port).
"""
