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

## Tracing

For latency, token usage, and cold-start spans, **traces beat logs**. Strands (under
Loom) ships OpenTelemetry instrumentation; wire an OTel exporter in your app and the
agent/tool/MCP spans flow through it. Logs answer "what happened"; traces answer
"where did the time go". Use both.
