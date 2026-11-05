---
title: Observability
description: Logging in Weave — stdlib namespaces, the NullHandler default, and capturing both Weave and Loom from one application logger.
---

# Observability

Weave is a *library*, so it does not choose where logs go — your application does.
It emits through the standard library `logging` under the `weave.*` namespace
(mirroring Loom's `loom.*`) and attaches a single `NullHandler`, so importing Weave
produces no output and no "no handlers could be found" warning. **No loguru, no
`basicConfig`, no default sink** — picking one would fight whatever your app already
uses.

That means one handler captures everything: Weave, Loom, and the rest of your stack,
in your format, with your context (correlation ids, request keys).

## Capturing Weave + Loom from one logger

Because both libraries log to stdlib `logging`, an [AWS Lambda Powertools](lambda.md)
`Logger` (itself a stdlib handler with structured-JSON sugar) captures both with a
one-time bridge at startup:

```python
import logging
from aws_lambda_powertools import Logger

logger = Logger(service="my-service")

# Redirect the library loggers to Powertools' handler. Children (loom.core.builder,
# weave.application.run, ...) propagate to their parent, so binding the parent is enough.
for name in ("loom", "weave"):
    lib = logging.getLogger(name)
    lib.handlers = logger.handlers   # reuse the JSON formatter + handler
    lib.setLevel("INFO")
    lib.propagate = False            # don't double-log up to the root logger
```

Now a `weave.application.run` debug line and a `loom.core.builder` line both come out
as the same structured JSON, carrying the correlation id and any keys you injected
with `@logger.inject_lambda_context` / `append_keys` — the Powertools formatter reads
those from its contextvar regardless of which logger produced the record.

Powertools also ships sugar for the same thing:

```python
from aws_lambda_powertools.logging import utils
utils.copy_config_to_registered_loggers(source_logger=logger, include={"loom", "weave"})
```

### Plain stdlib / loguru

No Powertools? The pattern is identical — point the `weave` and `loom` loggers at any
handler:

```python
import logging
logging.basicConfig(level=logging.INFO)        # or your own handler/formatter
logging.getLogger("weave").setLevel(logging.DEBUG)
logging.getLogger("loom").setLevel(logging.INFO)
```

For **loguru**, add an `InterceptHandler` to the `weave`/`loom` loggers so their
records flow into loguru's sinks — same mechanism, loguru is just the handler.

## What Weave logs

All application/adapter logs are low-level by default (`DEBUG`/`INFO`); raise the
level on `weave` to quiet them. Notable points:

| Logger | Level | Event |
|--------|-------|-------|
| `weave.composition` | INFO | `configure()` — which engine/source was installed at startup |
| `weave.adapters.driving.chat` | DEBUG | resolved target per chat turn / stream (`target → name`) |
| `weave.application.run` | DEBUG | build → completed → teardown lifecycle per turn (the guaranteed `cleanup()`) |
| `weave.adapters.driving.chat` (`chat_router`) | INFO/WARNING | a request mapped to no target (404), execution / definition failure (502) |
| `weave.adapters.driving.lambda_handler` | INFO/WARNING/**ERROR** | same outcomes; the `500` path logs the **full traceback** (`exc_info`) — the only place it survives, since the response body is opaque |
| `weave.application.observe` | WARNING | a turn observer raised (with traceback); the turn's reply or error is unchanged |

## Metrics

Weave tells **turn observers** how each turn ended. One line in the composition root
publishes turns, errors, duration and tokens per target to CloudWatch, with no extra
dependency:

```python
import weave
from weave import EmfTurnObserver

weave.configure(observers=[EmfTurnObserver("Acme/Agents", service="acme")])
```

Every wired use case notifies the observers once per turn — `run_agent`,
`stream_agent`, `run_workflow`, `fork`, `fork_stateless`, `run` — and so every
`ChatChannel` and `lambda_handler`. Your `render` and error mappers no longer need to
know about metrics.

### The turn event

An observer receives a frozen `TurnEvent`:

| Field | Meaning |
| --- | --- |
| `target`, `kind`, `operation` | what ran (`agent` / `workflow`; `run` / `stream` / `fork` / `regenerate`) |
| `session_id`, `user_id`, `is_anonymous` | the effective identity (after the session policy) |
| `turn_id`, `replaces` | the turn's id (on every outcome) and, on a `regenerate`, the turn it overwrote |
| `outcome` | `ok`, `error`, or `cancelled` (a stream closed before `done`, or a cancelled task) |
| `duration_ms` | the use case's wall clock, **build included** |
| `build_ms` | time to build the agent; `None` if the build failed |
| `usage`, `stop_reason` | only on `ok` |
| `error`, `error_type` | only on `error`; `error_type` unwraps the cause chain (`ContextWindowOverflowException`, not `HarnessError`) |
| `model_id`, `model_settings`, `model_fallbacks`, `variant` | the model the agent answered with, Loom's whitelisted settings, failed router tries, and the `metrics.variant` label — also on `error` once the agent was built; `None` for workflows and failed builds |

`duration_ms - usage.latency_ms` is roughly what rebuilding the agent per request
costs. A build that fails (bad YAML, a tool that does not load) is still a turn, with
`outcome="error"` and `build_ms=None`.

Observers are called in order, synchronously, before the reply is returned — in Lambda
the process freezes right after the handler returns. One that raises is logged and
skipped; the turn's result does not change.

### `EmfTurnObserver`

Writes one [Embedded Metric Format](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html)
line per turn to stdout. CloudWatch Logs turns it into metrics: no API call, no
`PutMetricData` permission.

| Metric | Unit | When |
| --- | --- | --- |
| `Turns` | Count (1) | always |
| `TurnErrors` | Count (0/1) | always — so `TurnErrors / Turns` has no gaps |
| `TurnDuration` | Milliseconds (whole) | always |
| `BuildDuration` | Milliseconds (whole) | when the build finished |
| `InputTokens`, `OutputTokens`, `TotalTokens`, `ToolCalls` | Count | `ok` |
| `CacheReadInputTokens`, `CacheWriteInputTokens` | Count | `ok` (0 without prompt caching) |
| `Regenerations` | Count (1) | a `regenerate` turn |

The same observer counts [user feedback](feedback.md#metrics) (`on_feedback`) under the same dimensions.

On Bedrock `TotalTokens` includes the prompt-cache reads and writes, and each kind is
priced differently. For cost, multiply `InputTokens`, `CacheReadInputTokens`,
`CacheWriteInputTokens` and `OutputTokens` by their own price; do not price
`TotalTokens`. The same split is on `Usage` (`cache_read_input_tokens`,
`cache_write_input_tokens`).

Dimensions default to `target` (plus `service`). When the turn has a model, the line
also declares `[base + model]`, and `[base + variant]` when the agent has a
`metrics.variant` label — `base` being the default or your `dimensions`. That gives
tokens and cost per model, and per experiment variant, next to the per-target view.
Turn them off with `model_dimension=False` / `variant_dimension=False`; each extra set is
one more series per metric. `model_config` (the whitelisted settings) and
`model_fallbacks` travel as properties.
 `session_id`, `user_id`, `turn_id`, `replaces`, `kind`,
`operation`, `outcome`, `stop_reason` and `error_type` travel as properties — searchable
in Logs Insights, never dimensions (each combination is billed as a metric). Override
either with `dimensions=lambda e: {...}` / `properties=lambda e: {...}`.

To look at one turn — say, one a user rated — filter by its id in Logs Insights:

```
fields @timestamp, target, outcome, TurnDuration, model, error_type
| filter turn_id = "…"
```

### Your own observer

Any object with `on_turn(event)` works — for example, to keep existing metric names
with Powertools:

```python
from aws_lambda_powertools import Metrics
from aws_lambda_powertools.metrics import MetricUnit

metrics = Metrics(namespace="Acme/Agents")


class PowertoolsObserver:
    def on_turn(self, event):
        metrics.add_dimension(name="agent", value=event.target)
        metrics.add_metric(name="AgentInvocations", unit=MetricUnit.Count, value=1)
        if event.outcome == "error":
            metrics.add_metric(name="AgentErrors", unit=MetricUnit.Count, value=1)
        metrics.flush_metrics()


weave.configure(observers=[PowertoolsObserver()])
```

### With Loom's `metrics` hook

Weave sees the turn from outside the agent; it does not split model time from tool
time. Loom's built-in `metrics` hook does, from inside. Use both, without counting
tokens twice:

```yaml title="warp/config/app.yml"
defaults:
  hooks:
    - metrics: { sink: emf, namespace: Acme/Agents, include: [breakdown, tools] }
```

## Turn transcripts

Metrics say *how* a turn went; a transcript says *what was said*: who asked, the question,
the answer and the tools the agent called. One `TurnTranscript` per agent turn, handed
to every observer with `on_transcript`:

```python
import weave
from weave import EmfTurnObserver, S3TranscriptObserver

weave.configure(
    observers=[
        EmfTurnObserver("Acme/Agents"),
        S3TranscriptObserver("acme-agents-data", prefix="turns/"),
    ]
)
```

It is a separate port from `on_turn` on purpose: `TurnEvent` never carries content, so a
metrics observer never sees what users wrote. Storing it is the product's explicit choice.

| Field | What it is |
| --- | --- |
| `turn_id`, `session_id`, `user_id`, `is_anonymous` | The turn and who asked (the effective session) |
| `target`, `operation`, `replaces` | Which agent, and whether it was a `run`, `stream`, `fork` or `regenerate` |
| `outcome`, `error_type` | `ok`, `error` or `cancelled`, as in `TurnEvent` |
| `started_at`, `duration_ms`, `model_id`, `usage` | When (UTC), how long, which model, tokens |
| `question`, `attachments` | The text of the message that opens the turn; the type of each non-text block (`"image/png"`) |
| `answer` | The reply's text; `None` unless the turn ended `ok` |
| `tool_calls` | Each tool call: `name`, `input`, `status` and `result` |

- **The question comes from the input**, so a turn whose build failed still has one.
- **The tool calls come from the turn's messages**: from the one stamped with its
  `turn_id` to the end, read before the agent's teardown. The messages are read only
  when some observer has `on_transcript`.
- **Never copied:** the bytes of images and documents, and the request's headers or
  `extras` (they may carry credentials).
- **Workflows and bidi streams have no transcript.** A workflow's input is not stamped
  and each node has its own messages.

### `S3TranscriptObserver`

One JSON object per turn at `{prefix}dt={YYYY-MM-DD}/{session_id}/{turn_id}.json`, the
day being the turn's start in UTC. A regenerated turn is another object (another
`turn_id`, with `replaces`), so the answer it replaced is kept — usually the one that got
the thumbs-down. Needs `weave[aws]`.

Tool **results** are dropped by default: they can be large and carry personal data. The
tool's `input` is always kept. Two ways to keep them:

- `include_tool_results=True` keeps every result whole.
- `tool_result=` decides per call. It receives the `ToolCall` (`name`, `input`, `status`,
  `result`) and returns the content blocks to store, or `None` to drop the result. It
  excludes `include_tool_results`.

For example, to record which documents a knowledge-base search returned without storing
its passages, with Loom's `builtin.kb` and its default name `kb_search`:

```python
from loom.builtins import kb_sources
from weave import S3TranscriptObserver, ToolCall


def sources_only(call: ToolCall):
    if call.name == "kb_search":
        return [{"json": {"sources": kb_sources(call.result)}}]
    return None  # every other tool: input only


S3TranscriptObserver("acme-agents-data", tool_result=sources_only)
```

A call the policy raises on, or returns something other than a list of blocks for, is
stored without result and logged as a `WARNING`; the rest of the transcript is written.

The write is a synchronous `PutObject` (Lambda freezes the process when the handler
returns). A failure is logged as a `WARNING` and the turn still answers.

!!! warning "Personal data"
    A transcript holds what users wrote, keyed by their `user_id`. Retention (a lifecycle
    rule on the prefix) and who can read the prefix are the product's to decide.

### Querying with Athena

A table with partition projection on `dt`, so no crawler or `MSCK REPAIR` is needed:

```sql
CREATE EXTERNAL TABLE turns (
  turn_id string, session_id string, user_id string, is_anonymous boolean,
  target string, operation string, replaces string, outcome string, error_type string,
  started_at string, duration_ms double, model_id string,
  question string, answer string,
  tool_calls array<struct<tool_use_id:string, name:string, status:string>>
)
PARTITIONED BY (dt string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://acme-agents-data/turns/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.dt.type' = 'date',
  'projection.dt.format' = 'yyyy-MM-dd',
  'projection.dt.range' = '2026-01-01,NOW',
  'storage.location.template' = 's3://acme-agents-data/turns/dt=${dt}/'
);
```

Joined with the feedback records (`S3FeedbackStore`, see [feedback](feedback.md)) by
`(session_id, turn_id)`, it shows the exact answer each thumbs-down was about:

```sql
SELECT t.user_id, t.target, t.question, t.answer, f.issues, f.comment
FROM turns t
JOIN feedback f ON f.session_id = t.session_id AND f.turn_id = t.turn_id
WHERE t.dt >= '2026-11-01' AND f.rating = 'down';
```

## Tracing

For latency, token usage, and cold-start spans, **traces beat logs**. Strands (under
Loom) ships OpenTelemetry instrumentation; wire an OTel exporter in your app and the
agent/tool/MCP spans flow through it. Logs answer "what happened"; traces answer
"where did the time go". Use both.
