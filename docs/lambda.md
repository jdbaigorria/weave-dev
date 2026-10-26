---
title: Deploying on Lambda
description: Run Weave in AWS Lambda — sync, streaming, and assets.
---

# Deploying on Lambda

Weave is a runtime *library* consumed in-process, so a Lambda handler just calls the
wired functions. Per-request rebuild + guaranteed `agent.cleanup()` make it
serverless-friendly: MCP connections never leak between invocations.

## One-shot (API Gateway)

The batteries-included path — the synchronous mirror of `chat_router`. No FastAPI,
no extra dependency: `lambda_handler(channel)` returns a ready `def handler(event,
context)` that parses the API Gateway event, runs one turn, and maps errors to
status codes (malformed body → 400, `HarnessError` (execution) or `LoomError`
(invalid agent definition/config) → 502, anything else → 500):

```python
from weave import chat_channel, lambda_handler

handler = lambda_handler(chat_channel("assistant"))
# POST {"message": "...", "session_id": "...", "user_id": "..."} → 200 + AgentReply JSON
```

The request body maps to a [`ChatRequest`](chat-channel.md) (`message`, `session_id`,
optional `user_id`/`extras`); the response `body` is the serialized `AgentReply` /
`WorkflowReply` (the `raw` escape hatch is excluded). One handler serves one target —
mount several for several agents, or route within one (below).

### Multiple agents behind one handler

Route to one of many agents by a request key with `routed_channel` — the same handler,
now multi-agent. You inject the policy; Weave dispatches:

```python
from weave import routed_channel, by_key, lambda_handler

handler = lambda_handler(routed_channel(by_key(
    lambda req: req.extras["profile"]["department"],
    {"SUPPORT": "support_agent", "SALES": "sales_agent"},
)))
# department maps to an agent → 200; unknown department → 404 (RoutingError)
```

See [Routing to multiple agents](chat-channel.md#routing-to-multiple-agents) for
`by_key`/`by_rules` and the single-vs-multi model.

### Your own HTTP contract: `parse` / `render` / `errors` / `headers`

A product whose API already has its own shape doesn't rewrite the handler — it plugs
into four optional, keyword-only seams of `lambda_handler`:

```python
from weave import (
    routed_channel, by_key, mint_if_absent, lambda_handler,
    ChatRequest, decode_body, header,
)

def parse(event) -> ChatRequest:
    body = decode_body(event)                       # base64 + JSON, {} if no body
    return ChatRequest(
        message=body["input"],                      # your field names
        session_id=body.get("conversation"),
        extras={"tenant": header(event, "X-Tenant")},   # case-insensitive (REST v1 / HTTP API v2)
    )

def render(reply, request):                         # → body; status is always 200
    return {"answer": reply.text, "agent": reply.target, "user": reply.user_id,
            "anonymous": reply.is_anonymous}

def overflow(exc, request):                         # → (status, body)
    return 413, {"error": "conversation_too_long"}

handler = lambda_handler(
    routed_channel(
        by_key(lambda r: r.extras["tenant"], ROUTES, default="support_agent"),
        session_policy=mint_if_absent(anon_prefix="anon_"),
    ),
    parse=parse,
    render=render,
    errors={ContextWindowOverflowException: overflow},
    headers={"Access-Control-Allow-Origin": "*"},   # on every response, errors included
)
```

- **`parse(event) -> ChatRequest`** — default `ChatRequest(**decode_body(event))`. Reject
  with `ValueError` or `RequestError` → 400. A malformed or invalid-base64 body is a 400
  too, never an uncaught exception.
- **`render(reply, request)`** — a `str` is the body as-is, a pydantic model goes through
  `model_dump_json()`, anything else through `json.dumps`. Default: the reply's JSON.
  Per-agent policy keys off `reply.target` (or its labels, via `labels_of`).
- **`errors={ExcType: mapper}`** — `mapper(exc, request) -> (status, body)`, with
  `request=None` if the failure came while parsing. Your mappings run **before** Weave's
  (so you can replace the 502 for `HarnessError`), are tried in **insertion order** like
  `except` clauses, and match if the raised exception **or any of its `__cause__` /
  `__context__` chain** is an instance. That last part is what makes the context
  overflow work: the turn raises `HarnessError` *from* the Strands exception. A mapper
  that raises answers 500.
- **`headers`** — added to `Content-Type: application/json` on every response; a
  same-named header replaces the default. CORS preflight (`OPTIONS`) is API Gateway's job.

Default status mapping (after your `errors`): `RequestError`/invalid body → 400,
`RoutingError` → 404, `HarnessError`/`LoomError` → 502, anything else → 500.

### Translate the event yourself (last resort)

If even that is too constraining, drive the run directly with the
[`*_sync` wrappers](harness.md#synchronous-callers-lambda) instead of `asyncio.run`:

```python
import json
from loom import LoomSession
from weave import run_agent_sync
from weave.application.errors import HarnessError

def handler(event, context):
    body = json.loads(event["body"])
    try:
        reply = run_agent_sync(
            "assistant",
            body["message"],
            LoomSession(user_id=body.get("user_id", body["session_id"]),
                        session_id=body["session_id"],
                        is_anonymous="user_id" not in body),
            extras={"jwt": event["headers"].get("authorization")},
        )
    except HarnessError as exc:
        return {"statusCode": 502, "body": json.dumps({"detail": str(exc)})}
    return {"statusCode": 200, "body": reply.model_dump_json()}  # raw excluded
```

Or reuse a FastAPI app (with `chat_router`) via **Mangum** — but note Mangum
**buffers**, so it's for request/response only, not streaming.

## Token streaming

API Gateway REST doesn't stream and Mangum buffers. Use **Lambda Function URLs with
`InvokeMode: RESPONSE_STREAM` + the [Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter)**
(LWA), which lets your FastAPI app stream over the Function URL:

```dockerfile
FROM public.ecr.aws/lambda/python:3.12
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
ENV AWS_LWA_INVOKE_MODE=response_stream
ENV PORT=8000
COPY . ${LAMBDA_TASK_ROOT}
RUN pip install "weave[fastapi]" uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

```python title="app.py"
from fastapi import FastAPI
from weave import chat_channel, chat_router

app = FastAPI()
app.include_router(chat_router(chat_channel("assistant")))
# POST /chat/stream now streams SSE through the Function URL
```

The `agent.cleanup()` still runs when the client disconnects (LWA closes the
generator → `aclose` runs the `finally`). See [Streaming](streaming.md).

## Assets

Prefer **presigned uploads** so large files never pass through Lambda — the browser
PUTs straight to S3:

```python
from weave import S3AssetStore, asset_router

app.include_router(asset_router(S3AssetStore("my-bucket")))
```

## Notes

- **`session_id` persistence** needs a durable `session_manager` in Loom
  (DynamoDB/S3) — Lambda's filesystem is ephemeral, so without it every invocation
  starts clean.
- **Cold start** adds to the time-to-first-token (build + MCP subprocess start);
  provisioned concurrency mitigates it if latency matters.
- **No buffering layers** in front of streaming — hit the Function URL (or ALB)
  directly; CloudFront/API Gateway buffering breaks SSE.
- **Concurrency** — if two requests can hit the same `session_id`, serialize them
  (Strands' sequentiality is per instance, not per session). See [Fork](fork.md).
- **Logging** — Weave and Loom log through stdlib `logging` (`weave.*` / `loom.*`),
  so an `aws-lambda-powertools` `Logger` captures both with one bridge — including
  the handler's `500` traceback. See [Observability](observability.md).
