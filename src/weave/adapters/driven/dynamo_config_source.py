"""weave.adapters.driven.dynamo_config_source — ``ConfigSource`` over DynamoDB (``weave[aws]``).

A non-filesystem source for Loom's declarative config: agents, workflows and the
app config live as items in one DynamoDB table instead of YAML under ``warp/``.
The harness wires it into the engine —
``LoomAgentEngine(source=DynamoConfigSource("my-table"))`` — and Loom builds every
target straight from the table, touching no disk.

**The config must be hydrated at write time.** Loom's ``build()`` reads no
filesystem for a non-file source (``paths=None``): the system prompt is stored as
literal text and every tool/output/hook is referenced by dotted path
(``module:fn``), never a local ``.py``. The :func:`migrate_from_file_source` helper
produces exactly this shape from an existing ``warp/`` workspace (the file source
hydrates as it reads), which is also the easiest way to seed the table.

Table layout (single-table)::

    target_name (PK, S)   entity_type (S)   kind (S)     description (S)   config (S, JSON)
    "assistant"           "agent"           "agent"      "Support bot"     {...AgentConfig...}
    "voice"               "agent"           "bidi"       "Voice agent"     {...}
    "triage"              "workflow"        "workflow"   "Routing graph"   {...WorkflowConfig...}
    "__app__"             "app"             -            -                 {...AppConfig...}

``kind``/``description`` are projected by :meth:`list_targets` without reading
``config``; ``config`` is a JSON string (exact round-trip, no DynamoDB ``Decimal``
coercion) parsed and re-validated by Pydantic on read.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

# The ConfigSource contract is defined in terms of Loom's errors; an adapter must
# raise them so callers of build_agent/build_workflow catch the documented types.
from loom.errors import (
    AgentConfigError,
    AgentNotFoundError,
    TargetResolutionError,
    WorkflowConfigError,
    WorkflowNotFoundError,
)
from loom.schemas.config.agent import AgentConfig
from loom.schemas.config.app import AppConfig
from loom.schemas.config.workflow import (
    GraphWorkflowConfig,
    SwarmWorkflowConfig,
    WorkflowConfig,
)
from loom.schemas.target import TargetInfo, TargetKind
from pydantic import TypeAdapter, ValidationError

from weave.application.errors import HarnessError

if TYPE_CHECKING:
    from pydantic import BaseModel

# Reserved primary key for the single app-config item (no agent may use it).
_APP_KEY = "__app__"

_ENTITY_AGENT = "agent"
_ENTITY_WORKFLOW = "workflow"
_ENTITY_APP = "app"

_workflow_adapter: TypeAdapter[GraphWorkflowConfig | SwarmWorkflowConfig] = TypeAdapter(
    WorkflowConfig
)


class DynamoConfigSource:
    """Loom :class:`~loom.ports.ConfigSource` backed by a DynamoDB table.

    Args:
        table_name: The DynamoDB table name. Ignored when ``table`` is supplied.
        table: A preconstructed boto3 DynamoDB ``Table`` resource. Injected in
            tests; otherwise built from ``boto3.resource("dynamodb")``.
        env: Active environment, accepted for parity with the port. The
            single-table layout keeps one app config, so it is currently unused
            for resolution (per-env isolation is a different table layout).
    """

    def __init__(
        self, table_name: str | None = None, *, table: Any = None, env: str | None = None
    ) -> None:
        if table is None:
            if table_name is None:
                raise HarnessError("DynamoConfigSource needs a table_name or an injected table.")
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - depends on the optional extra
                raise HarnessError(
                    "DynamoConfigSource needs boto3; install the extra: weave[aws]."
                ) from exc
            table = boto3.resource("dynamodb").Table(table_name)
        self._table = table
        self._env = env

    # ------------------------------------------------------------------
    # ConfigSource reads
    # ------------------------------------------------------------------

    def get_agent_config(self, name: str) -> AgentConfig:
        item = self._get_item(name)
        if item is None or item.get("entity_type") != _ENTITY_AGENT:
            raise AgentNotFoundError(name)
        try:
            return AgentConfig(**self._config_of(item))
        except ValidationError as exc:
            raise AgentConfigError(
                f"Invalid agent configuration for '{name}' in DynamoDB.", tip=str(exc)
            ) from exc

    def get_app_config(self, env: str | None = None) -> AppConfig:
        item = self._get_item(_APP_KEY)
        if item is None:
            return AppConfig()  # standalone-convenience defaults, like the file source
        try:
            return AppConfig(**self._config_of(item))
        except ValidationError as exc:
            raise AgentConfigError("Invalid app configuration in DynamoDB.", tip=str(exc)) from exc

    def get_workflow_config(self, name: str) -> GraphWorkflowConfig | SwarmWorkflowConfig:
        item = self._get_item(name)
        if item is None or item.get("entity_type") != _ENTITY_WORKFLOW:
            raise WorkflowNotFoundError(name)
        try:
            return _workflow_adapter.validate_python(self._config_of(item))
        except ValidationError as exc:
            raise WorkflowConfigError(
                f"Invalid workflow configuration for '{name}' in DynamoDB.", tip=str(exc)
            ) from exc

    def list_targets(self) -> list[TargetInfo]:
        """Scan the table for buildable targets (agents and workflows).

        Projects only ``target_name``/``kind``/``description`` — the ``config``
        blob is never read here. The app-config item is skipped.
        """
        targets: list[TargetInfo] = []
        for item in self._scan(("target_name", "entity_type", "kind", "description")):
            entity = item.get("entity_type")
            if entity not in (_ENTITY_AGENT, _ENTITY_WORKFLOW):
                continue
            targets.append(
                TargetInfo(
                    name=item["target_name"],
                    kind=item.get("kind", entity),
                    description=item.get("description", "") or "",
                )
            )
        targets.sort(key=lambda t: t.name)
        return targets

    def get_target_kind(self, name: str, kind: TargetKind | None = None) -> TargetKind:
        """Resolve ``name`` to its kind from its item's ``entity_type``/``kind``.

        Single-table: a name maps to at most one item, so there is no agent/workflow
        collision to disambiguate. ``kind`` only narrows (and validates) the lookup.
        """
        item = self._get_item(name)
        entity = item.get("entity_type") if item else None

        if kind == "workflow":
            if entity != _ENTITY_WORKFLOW:
                raise TargetResolutionError(
                    f"No workflow '{name}' found.",
                    tip="Store it as a workflow item, or drop kind='workflow'.",
                )
            return "workflow"
        if kind in ("agent", "bidi"):
            if entity != _ENTITY_AGENT:
                raise TargetResolutionError(
                    f"No agent '{name}' found.",
                    tip=f"Store it as an agent item, or drop kind='{kind}'.",
                )
            return item["kind"] if item else "agent"

        if entity == _ENTITY_AGENT:
            return item["kind"] if item else "agent"
        if entity == _ENTITY_WORKFLOW:
            return "workflow"
        raise TargetResolutionError(
            f"'{name}' is neither an agent nor a workflow.",
            tip="Store it as an agent or workflow item first.",
        )

    # ------------------------------------------------------------------
    # Writes (seeding / agentbuilder)
    # ------------------------------------------------------------------

    def put_agent_config(self, config: AgentConfig) -> None:
        """Write a (hydrated) agent config as an item keyed by its name."""
        self._put(
            config.name, _ENTITY_AGENT, config, kind=config.kind, description=config.description
        )

    def put_workflow_config(self, config: GraphWorkflowConfig | SwarmWorkflowConfig) -> None:
        """Write a workflow config as an item keyed by its name."""
        self._put(
            config.name, _ENTITY_WORKFLOW, config, kind="workflow", description=config.description
        )

    def put_app_config(self, config: AppConfig) -> None:
        """Write the single app-config item (reserved key ``__app__``)."""
        self._put(_APP_KEY, _ENTITY_APP, config)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_item(self, name: str) -> dict[str, Any] | None:
        resp = self._table.get_item(Key={"target_name": name})
        return cast("dict[str, Any] | None", resp.get("Item"))

    def _scan(self, attributes: tuple[str, ...]) -> list[dict[str, Any]]:
        """Scan the whole table, paginating, projecting only ``attributes``.

        ``#kind`` is aliased because ``kind`` could collide with a reserved word.
        """
        names = {f"#{a}": a for a in attributes}
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "ProjectionExpression": ", ".join(names),
            "ExpressionAttributeNames": names,
        }
        while True:
            resp = self._table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                return items
            kwargs["ExclusiveStartKey"] = start_key

    def _put(
        self,
        name: str,
        entity_type: str,
        config: BaseModel,
        *,
        kind: str | None = None,
        description: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "target_name": name,
            "entity_type": entity_type,
            "config": json.dumps(config.model_dump(mode="json")),
        }
        if kind is not None:
            item["kind"] = kind
        if description is not None:
            item["description"] = description
        self._table.put_item(Item=item)

    @staticmethod
    def _config_of(item: dict[str, Any]) -> dict[str, Any]:
        """Parse the JSON ``config`` blob of an item into a plain dict."""
        raw = item.get("config")
        if not raw:
            return {}
        return cast("dict[str, Any]", json.loads(raw))


def migrate_from_file_source(file_source: Any, dynamo_source: DynamoConfigSource) -> list[str]:
    """Seed a :class:`DynamoConfigSource` from a Loom ``FileConfigSource``.

    Reads every target the file source knows (its ``list_targets``) plus the app
    config, and writes each into DynamoDB. Because the file source *hydrates* on
    read (prompt → text, declarative tools → builtin specs), the items land in the
    fully-hydrated shape the Dynamo source requires. Returns the target names
    written.

    Note: code-by-dotted-path is the writer's responsibility — a workspace whose
    tools/output/hooks are local ``.py`` files will write items that fail to
    *build* from Dynamo (no filesystem at build time). Hydration covers declarative
    config, not code.
    """
    written: list[str] = []
    dynamo_source.put_app_config(file_source.get_app_config())
    for target in file_source.list_targets():
        if target.kind == "workflow":
            dynamo_source.put_workflow_config(file_source.get_workflow_config(target.name))
        else:
            dynamo_source.put_agent_config(file_source.get_agent_config(target.name))
        written.append(target.name)
    return written
