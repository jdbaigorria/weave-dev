---
title: Concepts
description: Where the line sits between Loom, Weave, and your backend.
---

# Concepts

## Loom ↔ Weave ↔ backend

- **Loom = capabilities / definition.** *What* an agent is. A library/factory that
  turns config into a native `strands.Agent` (`loom.build_agent`).
- **Weave = runtime / how it lives over time.** Per request, per turn: run it, stream
  it, branch it, list it, store its assets — through Loom's public API only.
- **Backend = product policy.** Weave never holds product-specific domain logic.

Weave is a *runtime library*, not "the backend". The deployable artifact is a
**composition** (a composition root + chosen adapters). For conversation-shaped apps
that composition *is* the backend; workflow-shaped apps import Weave's parts into
their own backend.

## Hexagonal layout

```text
src/weave/
  domain/        # pure conversation logic (branching), 0 deps
  application/   # use cases: run, stream, fork, content, conversations
  ports/         # capability protocols: AgentEngine, MutableSession,
                 #   ConversationRegistry, AssetStore
  adapters/
    driven/      # loom_engine, dynamo config source, mutable session,
                 #   file/s3 stores, conversation registry
    driving/     # chat/router/Lambda/assets (the outside calling in)
  composition/   # wires the default (Loom) engine to the use cases
```

The top-level functions you import from `weave` are the **wired** ones — already
bound to the default Loom engine:

```python
from weave import run_agent, run, stream_agent, open_stream, fork, catalog, chat_channel
from weave import run_agent_sync, run_sync, routed_channel, by_key, lambda_handler  # sync + routing + Lambda
```

Every one-shot coroutine has a `*_sync` sibling (`run_agent_sync`, `run_sync`,
`run_workflow_sync`, `fork_sync`, …) for a caller with no event loop of its own (a
Lambda handler); calling one from inside a running loop raises `HarnessError`.

## Capability ports

Some features need a piece of infrastructure the backend owns. Rather than build it
in, Weave defines a small **capability protocol** the backend implements (or uses a
reference adapter for), and checks for it at runtime:

| Capability | Port | Reference adapter | Used by |
| --- | --- | --- | --- |
| Rewind a persisted session | `MutableSession` | `MutableFileSessionManager` | [`fork`](fork.md) |
| List a user's chats | `ConversationRegistry` | `FileConversationRegistry` | [registry](conversations.md) |
| Store user uploads | `AssetStore` / `PresignedAssetStore` | `LocalAssetStore` / `S3AssetStore` | [assets](assets.md) |

Each is **opt-in**: wire it and the feature lights up; omit it and Weave does
nothing extra.

## Where config comes from (the config source)

*Which* agents and workflows exist, and what they declare, is config — and **where
that config lives is a deployment decision, so it's Weave's to make.** Loom owns the
contract (the `ConfigSource` port in `loom.ports`) and a default filesystem adapter
for standalone use (its CLI, dev runner, evals read `warp/` off disk). Weave owns the
*production* adapters and injects them.

| Source | Adapter | Lives in |
| --- | --- | --- |
| `warp/` workspace folder | `FileConfigSource` | Loom (default) |
| DynamoDB table | `DynamoConfigSource` | Weave (`weave[aws]`) |

The default engine passes no source, so Loom falls back to the filesystem. To serve
config from DynamoDB, call `weave.configure(...)` once at startup — every wired
top-level function then builds from that source:

```python
import weave
from weave.adapters.driven.dynamo_config_source import DynamoConfigSource

weave.configure(source=DynamoConfigSource("my-agents-table"))

reply = await weave.run_agent("assistant", "hello", session)  # built from DynamoDB
```

`configure` is the composition root: pass a `source` to pick where config lives, or
an `engine` to install a fully custom `AgentEngine`. Call it before serving traffic.

The same source backs `build_agent`, `build_workflow` **and** `discover`. A
non-filesystem source serves **hydrated** config (prompt as literal text, tools by
dotted path `module:fn`) so Loom's `build()` touches no disk; `migrate_from_file_source`
seeds a table from an existing `warp/` workspace, reusing the file source's hydration.
See [Config source (DynamoDB)](config-source.md) for the table layout, writer helpers,
migration flow, and error mapping.

## Pre-agent entrypoint (routing)

"Which agent runs for this request?" is a runtime decision against a live request, so
it is **not** Loom's job (Loom is declarative; it never sees the request) and not a new
primitive — it reduces to one injected function `(ChatRequest) -> str`. The split:

- **The policy is yours** — where the routing key comes from (a header, a JWT claim,
  `profile.department`) and the key→target table. It is product data.
- **The dispatch is Weave's** — a [`ChatChannel`](chat-channel.md) binds either a fixed
  name (single agent) or a resolver (a router), and runs the resolved target. `by_key` /
  `by_rules` are the two common resolvers; intent routing (an LLM picks) is modeled as a
  Loom Swarm/supervisor — a target of its own, not a resolver.

Single- and multi-agent are the **same channel**: a bare name is just a constant
resolver. A request that maps to no target raises `RoutingError` (→ 404), distinct from
an execution failure (`HarnessError` → 502).

## Lifecycle: rebuild per request, guaranteed teardown

Every use case follows the same discipline:

```python
agent = engine.build_agent(name, session)   # fresh per request
try:
    ... # invoke / stream
finally:
    agent.cleanup()                          # always — closes MCP connections
```

Each request builds its **own** agent instance (serverless-friendly; MCP stdio = a
subprocess per request). The value Weave adds isn't reimplementing Strands'
execution — it's *guaranteeing the `finally`*.

!!! warning "Strands sequentiality is per instance, not per session"
    Strands serializes turns only **per agent instance** (`ConcurrencyException` on a
    concurrent invoke). Because each request rebuilds its own instance, that guard
    does **not** span a `session_id`. If you allow concurrent turns on one
    conversation, serialize them yourself (a per-`session_id` lock/queue) — it
    matters most for [`fork`](fork.md), which mutates the persisted session.

## Replies

Running a target yields a *fine projection* of the native Strands result into Weave's
own envelope, plus a `raw` escape hatch to everything:

- [`AgentReply`](harness.md) — text, structured `output`, `stop_reason`, `interrupts`,
  `usage`, `session_id`, `raw`.
- [`WorkflowReply`](harness.md) — `text`, `status`, `usage`, `execution_order`,
  `interrupts`, `session_id`, `raw`.
- [`ChatStreamEvent`](streaming.md) / [`StreamEvent`](streaming.md) — the streaming
  projections.

Execution failures raise a typed `HarnessError` (→ 502 at a mounted channel); a request
that routes to no target raises `RoutingError` (→ 404). A reply never carries an error
field — a run either returns a reply or raises.
