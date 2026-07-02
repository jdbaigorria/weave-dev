# Weave

**Conversation runtime / harness for Loom-built agents.**

Loom is the *engine*: it turns declarative config into a `strands.Agent`
(`loom.build_agent(...)`). Weave is the *harness*: it owns how an agent **lives over
time** — conversation history with branching, sessions, interrupts/resume,
channels (chat/voice), user assets, observability. It depends on Loom and uses it
through its public API only; Loom never depends on Weave.

> **Pre-alpha (v0.0.1).** The surface is settling, but the core is wired: running a
> turn (async or sync), token/voice streaming, fork, multimodal input, DynamoDB-backed
> config, a mountable chat channel with pre-agent routing, a Lambda handler,
> conversation registry, and asset stores. See [the docs](docs/index.md).

```python
from weave import run_agent_sync
from loom import LoomSession

reply = run_agent_sync("assistant", "hello", LoomSession(user_id="u1", session_id="s1"))
print(reply.text)
```

## Installation

Neither Weave nor Loom is on PyPI yet — install both pinned to a release tag (Weave
always needs Loom; it's a hard dependency, not an extra):

```bash
uv add "weave @ git+https://github.com/jdbaigorria/weave-dev@vX.Y.Z" \
       "loom-declarative @ git+https://github.com/jdbaigorria/loom-dev@vY.Z.W"

# or with pip:
pip install "weave @ git+https://github.com/jdbaigorria/weave-dev@vX.Y.Z" \
            "loom-declarative @ git+https://github.com/jdbaigorria/loom-dev@vY.Z.W"

# extras: [fastapi] for the mountable chat/asset routers, [aws] for S3/DynamoDB
uv add "weave[fastapi,aws] @ git+https://github.com/jdbaigorria/weave-dev@vX.Y.Z" ...
```

See [Installing this repo for co-development](#development) below if you're working
on Weave and Loom together.

## Where the line sits (Loom ↔ Weave ↔ backend)

- **Loom = capabilities / definition.** *What* an agent is. A library/factory.
- **Weave = runtime / authorization.** *How* it runs over time, per request.
  Selects the active tool subset, owns history, drives channels.
- **Backend = product policy.** Weave never holds product-specific domain logic.

Weave is a *runtime library*, not "the backend": the deployable artifact is a
composition (composition root + chosen adapters). For conversation-shaped apps
that composition *is* the backend; workflow-shaped apps import Weave's parts into
their own backend.

## Architecture (hexagonal / ports & adapters)

```
src/weave/
  domain/        # ConversationTree, Turn, Interrupt — pure logic, 0 deps
  application/   # use cases: run / stream / fork / content / conversations; errors
  ports/         # AgentEngine (→ Loom), MutableSession, ConversationRegistry, AssetStore
  adapters/
    driven/      # loom_engine, dynamo_config_source, mutable_file_session,
                 #   file_registry, local/s3 assets
    driving/     # chat (ChatChannel + chat_router), routing (by_key/by_rules),
                 #   lambda_handler, assets router (the outside calling in)
  composition/   # composition root: wires the default Loom engine + the *_sync wrappers
```

## Development

During co-development, Loom is used as a local sibling checkout via `[tool.uv.sources]`.

```bash
uv sync          # installs weave + editable ../loom-dev
uv run poe test  # pytest
uv run poe lint  # ruff
uv run poe type  # mypy
```

## Documentation

Per-feature docs (MkDocs Material) live in `docs/`:

```bash
uv sync --group docs    # mkdocs + mkdocs-material
uv run poe docs         # serve at http://127.0.0.1:8000
uv run poe docs-build   # build --strict into site/
```

## License

This project is licensed under the MIT License — see [`LICENSE`](LICENSE).
