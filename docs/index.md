---
title: Weave
description: Conversation runtime / harness for Loom-built agents.
---

# Weave

**Conversation runtime / harness for [Loom](https://github.com/jdbaigorria/loom-dev)-built agents.**

Loom is the *engine*: it turns declarative config into a `strands.Agent`
(`loom.build_agent(...)`). **Weave is the *harness*:** it owns how an agent **lives
over time** — running a turn with guaranteed teardown, token streaming, full-duplex
voice, editing/regenerating a conversation, shaping multimodal input, listing a
user's chats, and storing their uploads. It depends on Loom and uses it through its
public API only; Loom never depends on Weave.

!!! warning "Pre-alpha (v0.0.1)"
    The surface is settling. Conversation persistence is Strands' job (a
    `session_manager` configured in Loom), not Weave's.

## Install

Neither Weave nor Loom is on PyPI yet — install both pinned to a release tag. Weave
always needs Loom (it's a hard dependency, not an extra):

=== "uv"
    ```bash
    uv add "weave @ git+https://github.com/jdbaigorria/weave-dev@vX.Y.Z" \
           "loom-declarative @ git+https://github.com/jdbaigorria/loom-dev@vY.Z.W"

    # extras, as needed:
    uv add "weave[fastapi] @ git+https://github.com/jdbaigorria/weave-dev@vX.Y.Z" ...  # mountable chat/asset routers
    uv add "weave[aws] @ git+https://github.com/jdbaigorria/weave-dev@vX.Y.Z" ...      # S3 asset store, DynamoDB config source
    ```

=== "pip"
    ```bash
    pip install "weave @ git+https://github.com/jdbaigorria/weave-dev@vX.Y.Z" \
                "loom-declarative @ git+https://github.com/jdbaigorria/loom-dev@vY.Z.W"
    ```

During **co-development** on this repo, Loom is a local sibling checkout instead
(`[tool.uv.sources]` points at `../loom-dev`, editable):

```bash
uv sync                       # weave + editable ../loom-dev
uv sync --extra fastapi       # for the mountable chat / asset routers
uv sync --extra aws           # for the S3 asset store
```

## A first turn

```python
import asyncio
from loom import LoomSession
from weave import run_agent

async def main():
    reply = await run_agent(
        "assistant",                       # a target Loom can build
        "What's the capital of France?",
        LoomSession(user_id="u1", session_id="s1"),
    )
    print(reply.text)        # "Paris"
    print(reply.usage.total_tokens)

asyncio.run(main())
```

`run_agent` builds the agent, runs one turn, shapes the result into an
[`AgentReply`](harness.md), and **always** tears the agent down (closing any MCP
connections) — the boilerplate Weave collapses. No event loop of your own (a Lambda
handler)? Use the synchronous sibling `run_agent_sync` — same call, no `asyncio.run`.

## Features

<div class="grid cards" markdown>

- :material-cog: **[Harness (`run`)](harness.md)** — one-shot execution with
  guaranteed teardown; dispatch by kind (agent / workflow / bidi); `*_sync` wrappers
  for callers without an event loop.
- :material-chat: **[Chat channel](chat-channel.md)** — a mountable, transport-agnostic
  request/response channel, with pre-agent routing to one of many agents
  (`routed_channel` / `by_key`).
- :material-waveform: **[Streaming](streaming.md)** — token streaming over SSE, and
  full-duplex voice over bidi.
- :material-source-branch: **[Fork](fork.md)** — edit / regenerate / explore a
  conversation.
- :material-file-document: **[Input shaping](content.md)** — bytes → multimodal
  content blocks.
- :material-database: **[Config source](config-source.md)** — serve Loom config from
  DynamoDB instead of a filesystem workspace.
- :material-format-list-bulleted: **[Conversation registry](conversations.md)** — list a
  user's chats (opt-in, metadata only).
- :material-folder-upload: **[Asset store](assets.md)** — user uploads, swappable
  backend (S3 / local), presigned URLs.
- :material-aws: **[Deploying on Lambda](lambda.md)** — a batteries-included
  `lambda_handler(channel)` for API Gateway, plus streaming notes.

</div>

New to the boundaries? Start with **[Concepts](concepts.md)**. Want copy-paste snippets
for each public API? See the **[API cookbook](api-cookbook.md)**.
