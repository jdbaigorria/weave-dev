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
| `target`, `kind`, `operation` | what ran (`agent` / `workflow`; `run` / `stream` / `fork`) |
| `session_id`, `user_id`, `is_anonymous` | the effective identity (after the session policy) |
| `outcome` | `ok`, `error`, or `cancelled` (a stream closed before `done`, or a cancelled task) |
| `duration_ms` | the use case's wall clock, **build included** |
| `build_ms` | time to build the agent; `None` if the build failed |
| `usage`, `stop_reason` | only on `ok` |
| `error`, `error_type` | only on `error`; `error_type` unwraps the cause chain (`ContextWindowOverflowException`, not `HarnessError`) |

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
| `TurnDuration` | Milliseconds | always |
| `BuildDuration` | Milliseconds | when the build finished |
| `InputTokens`, `OutputTokens`, `TotalTokens`, `ToolCalls` | Count | `ok` |

Dimensions default to `target` (plus `service`). `session_id`, `user_id`, `kind`,
`operation`, `outcome`, `stop_reason` and `error_type` travel as properties — searchable
in Logs Insights, never dimensions (each combination is billed as a metric). Override
either with `dimensions=lambda e: {...}` / `properties=lambda e: {...}`.

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

## Tracing

For latency, token usage, and cold-start spans, **traces beat logs**. Strands (under
Loom) ships OpenTelemetry instrumentation; wire an OTel exporter in your app and the
agent/tool/MCP spans flow through it. Logs answer "what happened"; traces answer
"where did the time go". Use both.
