---
title: Chat channel
description: A mountable, transport-agnostic request/response chat channel.
---

# Chat channel

A **driving adapter**: it translates an inbound chat request into a `run` call and
returns the harness reply. Two layers, like bidi:

- **`ChatChannel`** — the transport-agnostic core. It parses the request, builds the
  `LoomSession`, dispatches by kind, and surfaces failures as `HarnessError`. Knows
  nothing about HTTP.
- **`chat_router`** — a thin, **mountable** FastAPI `APIRouter` over it (FastAPI
  imported lazily, gated behind `weave[fastapi]`).

One channel serves one target — the mount point is the authorization boundary. Mount
several channels for several agents.

## Build and call directly

```python
from weave import chat_channel, ChatRequest

channel = chat_channel("assistant")            # wired to the default Loom engine

reply = await channel.handle(
    ChatRequest(message="hi", session_id="s1", user_id="u1")
)
print(reply.text)
```

### `ChatRequest`

```python
class ChatRequest(BaseModel):
    message: str | list[Any]    # a string, or Strands content blocks (multimodal)
    session_id: str | None = None  # absent → the channel's SessionPolicy decides
    user_id: str | None = None  # omit → anonymous, session keyed by session_id
    extras: dict[str, Any] | None = None
```

When `user_id` is omitted the session is **anonymous** (keyed by `session_id`).

### Session policy (strict by default)

`session_id` is optional *on the wire*; what happens when it's absent is the channel's
**`SessionPolicy`**. Session lifetime is Weave's domain, so the channel owns the
minting mechanism — you inject the policy (same shape as routing):

- **`require()`** — the default. A request **must** carry its own `session_id`, else
  `RequestError` → **HTTP 400**. A runtime library shouldn't silently mint a session and
  turn a client's "I forgot to persist my `session_id`" bug into a brand-new conversation
  every turn.
- **`mint_if_absent(anon_prefix="anon_")`** — generate a `session_id` when absent (and an
  anonymous `user_id`, optionally prefixed). The effective id is returned on the
  `AgentReply`, so the caller echoes it on the next turn. For a public, gateway-less
  frontend.

```python
from weave import chat_channel, mint_if_absent

# Behind an auth gateway (ids always present): keep the strict default.
chat_channel("assistant")

# Public frontend that may not send ids on the first hit:
chat_channel("assistant", session_policy=mint_if_absent(anon_prefix="anon_"))
```

## Mount on a backend (FastAPI)

```python
from fastapi import FastAPI
from weave import chat_channel, chat_router

app = FastAPI()
app.include_router(chat_router(chat_channel("assistant")))
# → POST /chat        (one-shot reply)
# → POST /chat/stream (token streaming — see Streaming)
```

`chat_router(channel, *, path="/chat", stream_path="/chat/stream")` lets you choose
custom mount paths when a backend exposes several channels.

`POST /chat` returns the `AgentReply` / `WorkflowReply` serialized by FastAPI (the
`raw` escape hatch is excluded by the model). A failure before the reply maps to
**HTTP 502**.

```bash
curl -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"message": "hello", "session_id": "s1", "user_id": "u1"}'
```

!!! note "Identity is the backend's job"
    `user_id` arrives in the request body. The mounted router trusts it — your backend
    should establish identity (auth middleware, a dependency that overrides `user_id`).
    The mount point is where you enforce authorization.

## Routing to multiple agents

A channel can serve **one** target (everything above) or **route** to one of many per
request — the *pre-agent entrypoint*. You inject the policy (where the routing key comes
from, which table); Weave does the dispatch. Build it with `routed_channel`:

```python
from weave import routed_channel, by_key, lambda_handler

channel = routed_channel(by_key(
    lambda req: req.extras["profile"]["department"],   # your key — header, JWT claim, …
    {"SUPPORT": "support_agent", "SALES": "sales_agent"},
))
handler = lambda_handler(channel)   # or chat_router(channel)
```

Single-agent is just the degenerate case: `chat_channel("assistant")` is a router with a
**constant resolver**. Same `ChatChannel`, same mounting, same `handle`/`stream`. The
target's kind is inferred per request, so a router can mix agents and workflows.

A backend with its own response contract (it maps the reply to its own DTO, in a sync
handler) can route + dispatch in one blocking call with `channel.handle_sync(request)` —
no `asyncio.run` by hand; routing/execution errors surface as `RoutingError`/`HarnessError`.

A *resolver* is any `(ChatRequest) -> str`. Two are shipped:

- **`by_key(key_of, routes, *, default=None)`** — deterministic table lookup (key-based;
  also identity-based, when the key is a JWT claim).
- **`by_rules([(predicate, target), …], *, default=None)`** — first matching predicate
  wins (compound, ordered conditions).

A request that maps to no target raises `RoutingError` → **HTTP 404** (vs an execution
failure's 502). Pass `default=` for a catch-all instead.

!!! note "Intent routing is a target, not a resolver"
    Letting an **LLM** pick the agent isn't a resolver (these are synchronous, no model
    call). Model it as a Loom **Swarm / supervisor agent** — a target of its own — and
    point a normal channel at it.

## What the channel adds

- Request parsing + `LoomSession` construction via the **session policy** (strict
  `require()` by default; `mint_if_absent()` to generate ids).
- Target resolution — a fixed name, or a `by_key`/`by_rules`/custom resolver.
- Dispatch by kind (via the wired `run`).
- Optional **auto-recording** into a [conversation registry](conversations.md) — pass
  `chat_channel("assistant", registry=...)`.
- A mountable transport, instead of a sealed server.

## See also

- [Streaming](streaming.md) — the `POST /chat/stream` SSE route.
- [Conversation registry](conversations.md) — list a user's chats.
- [Input shaping](content.md) — send PDFs/images as `message`.
