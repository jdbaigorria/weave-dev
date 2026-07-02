---
title: Config source (DynamoDB)
description: Serve Loom agent/workflow config from DynamoDB instead of a filesystem workspace.
---

# Config source (DynamoDB)

By default, the wired Weave engine lets Loom read declarative config from its normal
filesystem workspace. In production you may want the same agents and workflows to live
in a database instead. `DynamoConfigSource` is Weave's AWS-backed implementation of
Loom's `ConfigSource` port.

Use it when the runtime should **build agents/workflows from DynamoDB** and touch no
local `warp/` folder at request time.

## Install and configure

```bash
uv sync --extra aws        # installs boto3 for DynamoConfigSource / S3AssetStore
```

```python
import weave
from weave.adapters.driven.dynamo_config_source import DynamoConfigSource

weave.configure(source=DynamoConfigSource("my-agents-table"))

# Every wired entry point now builds from DynamoDB:
reply = await weave.run_agent("assistant", "hello", session)
```

Call `configure(...)` once at startup, before serving traffic. Passing no arguments
resets Weave to the default Loom filesystem source; passing `engine=...` installs a
fully custom `AgentEngine` instead.

## Table layout

Single-table layout, keyed by `target_name`:

| `target_name` | `entity_type` | `kind` | `description` | `config` |
| --- | --- | --- | --- | --- |
| `assistant` | `agent` | `agent` | `Support assistant` | JSON `AgentConfig` |
| `voice` | `agent` | `bidi` | `Voice assistant` | JSON `AgentConfig` |
| `triage` | `workflow` | `workflow` | `Ticket triage` | JSON `WorkflowConfig` |
| `__app__` | `app` | — | — | JSON `AppConfig` |

`list_targets()` scans only `target_name`, `entity_type`, `kind`, and `description`, so
catalog discovery does not read the full `config` blob. Reads parse the JSON config and
validate it with Loom's Pydantic schemas.

## Hydrated config requirement

A non-filesystem source must store **hydrated** config:

- prompts are literal text, not file paths;
- tools, output schemas, and hooks are dotted paths like `package.module:function`;
- local `.py` files are not read by Loom at build time.

That means the writer side must validate that every referenced dotted path is importable
in the deployed runtime image. If a stored item points at code that is not installed,
`build_agent` / `build_workflow` fails with Loom's typed config errors.

## Writing config

`DynamoConfigSource` includes small writer helpers for seeding or admin tooling:

```python
source = DynamoConfigSource("my-agents-table")

source.put_app_config(app_config)
source.put_agent_config(agent_config)
source.put_workflow_config(workflow_config)
```

The helpers serialize the Pydantic model with `model_dump(mode="json")` and write a
JSON string into `config`, avoiding DynamoDB `Decimal` coercion surprises.

## Migrating from a filesystem source

To seed DynamoDB from an existing Loom filesystem workspace, reuse Loom's file source
and let it hydrate the config as it reads:

```python
from loom.adapters.config import FileConfigSource
from weave.adapters.driven.dynamo_config_source import (
    DynamoConfigSource,
    migrate_from_file_source,
)

file_source = FileConfigSource("warp")
dynamo_source = DynamoConfigSource("my-agents-table")

written = migrate_from_file_source(file_source, dynamo_source)
print(written)  # target names written
```

The helper writes the app config plus every target returned by `list_targets()`.

## Errors

`DynamoConfigSource` follows Loom's `ConfigSource` contract:

| Situation | Error |
| --- | --- |
| Missing agent | `AgentNotFoundError` |
| Missing workflow | `WorkflowNotFoundError` |
| Invalid agent/app config | `AgentConfigError` |
| Invalid workflow config | `WorkflowConfigError` |
| Unknown or mismatched kind | `TargetResolutionError` |
| Missing `boto3` / missing table injection | `HarnessError` |

Mounted adapters map Loom definition/config failures to **HTTP 502** because the target
could not be built before it ran.

## Testing / local injection

For tests, inject a fake boto3 Table-like object instead of creating a real resource:

```python
source = DynamoConfigSource(table=fake_table)
```

The fake only needs the methods this adapter uses: `get_item`, `put_item`, and `scan`.
