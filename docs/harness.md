---
title: Harness (run)
description: One-shot execution with guaranteed teardown, dispatching by kind.
---

# Harness (`run`)

The minimal seam over Loom: collapse the repeated `build → invoke → shape → teardown`
boilerplate and **guarantee** the teardown. It does not reimplement Strands'
execution; it wraps it. Async-first (`agent.invoke_async`).

## Run an agent

```python
from loom import LoomSession
from weave import run_agent

reply = await run_agent(
    "assistant",
    "Summarise the meeting notes.",
    LoomSession(user_id="u1", session_id="s1"),
    extras={"jwt": token},          # forwarded as native invocation_state
)
```

`extras` flow through as Strands `invocation_state` — tools read them via
`tool_context.invocation_state` (a clean channel for identity / tenant / request
context).

### `AgentReply`

```python
class AgentReply:
    text: str                  # all text blocks of the final message, concatenated
    output: BaseModel | None   # structured_output, when the agent produced one
    stop_reason: str | None
    interrupts: list[Interrupt]  # reported (the agent paused) — not resumed here
    usage: Usage
    session_id: str
    raw: AgentResult           # escape hatch; excluded from model_dump()
```

```python
class Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: float
    tool_calls: list[str]
```

### Wrapping `AgentReply` in your own DTO

`AgentReply` is the right shape to consume a turn, but a backend with its own public
HTTP contract (different field names, an existing frontend) translates it into its own
model. That translation is yours to own — but map **every** field explicitly:

```python
class MyResponse(BaseModel):
    answer: str
    finished: bool
    pending: list[Interrupt]
    structured: BaseModel | None

    @classmethod
    def from_reply(cls, reply: AgentReply) -> "MyResponse":
        return cls(
            answer=reply.text,
            finished=not reply.interrupts,
            pending=reply.interrupts,    # easy to forget
            structured=reply.output,     # easy to forget
        )
```

!!! warning "Don't drop `interrupts` and `output`"
    A hand-written mapper that copies `text`/`usage` and stops will **silently lose**
    `interrupts` (the agent paused and is waiting on you) and `output` (the structured
    result). They have no effect on a happy-path text turn, so the gap stays invisible
    until an agent actually pauses or returns structured output. Map them — or assert
    they're empty if your contract truly doesn't support them.

## Run a workflow (Graph / Swarm)

```python
from weave import run_workflow

reply = await run_workflow("triage_team", "ticket #42", session)
reply.status           # "COMPLETED" / "FAILED" / "INTERRUPTED"
reply.execution_order  # node ids in run order
reply.interrupts       # pending human-in-the-loop interrupts, if any
reply.text             # terminal node's answer
```

```python
class WorkflowReply:
    text: str
    status: str
    usage: Usage
    execution_order: list[str]
    interrupts: list[Interrupt]
    session_id: str
    raw: MultiAgentResult  # escape hatch; excluded from model_dump()
```

A workflow has no `cleanup()` of its own, so Weave tears down **each node agent**
individually (closing its MCP connections).

## Dispatch by kind

If you don't know the target's kind ahead of time, let `run` discover it:

```python
from weave import run, catalog

for target in catalog():           # passthrough of loom.discover()
    print(target.name, target.kind, target.description)

reply = await run("assistant", "hi", session)         # kind discovered
reply = await run("assistant", "hi", session, kind="agent")  # or pass it
```

- `kind="agent"` → `AgentReply`
- `kind="workflow"` → `WorkflowReply`
- `kind="bidi"` → raises; bidi is full-duplex, use [`open_stream`](streaming.md#voice-bidi).

## Synchronous callers (Lambda)

The harness is async-first, but the most common serverless target — an AWS Lambda
`def handler(event, context)` — runs no event loop of its own. Rather than wrap
every call in `asyncio.run(...)`, use the `_sync` siblings:

```python
from weave import run_agent_sync

def handler(event, context):
    reply = run_agent_sync("assistant", event["query"], session)
    return {"statusCode": 200, "body": reply.model_dump_json()}
```

Every one-shot coroutine has one: `run_agent_sync`, `run_workflow_sync`, `run_sync`,
`fork_sync`, `fork_stateless_sync`. They block until the turn completes. Calling one
from **inside** a running event loop raises `HarnessError` — you're already async, so
`await` the coroutine directly instead. The streaming entry points (`stream_agent`,
`open_stream`) are inherently async and have no `_sync` form.

!!! tip
    Mounting on Lambda + API Gateway? [`lambda_handler(channel)`](lambda.md) wraps this
    plus event parsing and error mapping into a ready-made handler.

## Errors and teardown

```python
from weave.application.errors import HarnessError

try:
    reply = await run_agent("assistant", "hi", session)
except HarnessError as exc:
    ...   # execution failed; the agent was still cleaned up
```

- **Build-time** failures propagate Loom's own typed errors unchanged.
- **Execution** failures are wrapped in `HarnessError`.
- The agent is **always** torn down via `agent.cleanup()`, success or failure.

Mounted adapters turn typed errors into HTTP status codes:

| Error | Meaning | `chat_router` / `lambda_handler` |
| --- | --- | --- |
| `RequestError` | Invalid request shape or missing required identity/session data | 400 |
| `RoutingError` | The request did not map to any target | 404 |
| `HarnessError` | The target built but execution failed | 502 |
| `LoomError` | The target definition/config could not be built | 502 |
| Unexpected exception | Adapter bug or unhandled runtime failure | 500 (`lambda_handler`; FastAPI default otherwise) |

## Injecting a different engine

The top-level `run_agent` / `run_workflow` / `run` / `catalog` are wired to the
default Loom engine. The dependency-injected cores live in
`weave.application.run` and take an `AgentEngine` first — useful for testing against
a mock engine:

```python
from weave.application.run import run_agent as run_agent_core
reply = await run_agent_core(my_engine, "assistant", "hi", session)
```
