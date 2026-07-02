"""Tests for DynamoConfigSource — Loom ConfigSource backed by a DynamoDB table.

A FakeTable stands in for the boto3 DynamoDB ``Table`` resource (get_item /
put_item / scan), so these exercise the adapter's contract — validation, error
mapping, the single-table layout — with no network and no real table.
"""

from __future__ import annotations

from typing import Any

import pytest
from loom.errors import (
    AgentConfigError,
    AgentNotFoundError,
    TargetResolutionError,
    WorkflowNotFoundError,
)
from loom.ports import ConfigSource
from loom.schemas.config.agent import AgentConfig
from loom.schemas.config.app import AppConfig
from loom.schemas.config.workflow import SwarmWorkflowConfig

from weave.adapters.driven.dynamo_config_source import (
    DynamoConfigSource,
    migrate_from_file_source,
)
from weave.application.errors import HarnessError


class FakeTable:
    """In-memory stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def put_item(self, *, Item: dict[str, Any]) -> None:
        self.items[Item["target_name"]] = Item

    def get_item(self, *, Key: dict[str, Any]) -> dict[str, Any]:
        item = self.items.get(Key["target_name"])
        return {"Item": item} if item is not None else {}

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        # Projection is honoured by the real service; the fake returns whole items
        # (the adapter only reads the projected attributes anyway).
        return {"Items": list(self.items.values())}


@pytest.fixture
def source() -> DynamoConfigSource:
    return DynamoConfigSource(table=FakeTable())


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_requires_table_name_or_table():
    with pytest.raises(HarnessError, match="table_name or an injected table"):
        DynamoConfigSource()


def test_satisfies_config_source_protocol(source):
    assert isinstance(source, ConfigSource)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def test_put_then_get_agent_roundtrips(source):
    cfg = AgentConfig(name="assistant", description="Support bot")
    source.put_agent_config(cfg)

    loaded = source.get_agent_config("assistant")
    assert loaded.name == "assistant"
    assert loaded.description == "Support bot"


def test_get_missing_agent_raises(source):
    with pytest.raises(AgentNotFoundError):
        source.get_agent_config("ghost")


def test_get_agent_rejects_a_workflow_item(source):
    source.put_workflow_config(SwarmWorkflowConfig(name="triage", agents=["a"]))
    with pytest.raises(AgentNotFoundError):
        source.get_agent_config("triage")


def test_invalid_agent_config_raises_agent_config_error(source):
    # Bypass put_* to plant a malformed config blob (kind must be agent|bidi).
    source._table.put_item(
        Item={
            "target_name": "broken",
            "entity_type": "agent",
            "config": '{"name": "broken", "kind": "nonsense"}',
        }
    )
    with pytest.raises(AgentConfigError):
        source.get_agent_config("broken")


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------


def test_get_app_config_defaults_when_absent(source):
    assert source.get_app_config() == AppConfig()


def test_put_then_get_app_config_roundtrips(source):
    app = AppConfig()
    source.put_app_config(app)
    assert source.get_app_config() == app


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


def test_put_then_get_workflow_roundtrips(source):
    wf = SwarmWorkflowConfig(name="triage", agents=["a", "b"], description="Routes")
    source.put_workflow_config(wf)

    loaded = source.get_workflow_config("triage")
    assert isinstance(loaded, SwarmWorkflowConfig)
    assert loaded.agents == ["a", "b"]
    assert loaded.description == "Routes"


def test_get_missing_workflow_raises(source):
    with pytest.raises(WorkflowNotFoundError):
        source.get_workflow_config("ghost")


def test_get_workflow_rejects_an_agent_item(source):
    source.put_agent_config(AgentConfig(name="assistant"))
    with pytest.raises(WorkflowNotFoundError):
        source.get_workflow_config("assistant")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_list_targets_reports_kind_and_description_and_skips_app(source):
    source.put_app_config(AppConfig())
    source.put_agent_config(AgentConfig(name="billing", description="Invoices"))
    source.put_agent_config(AgentConfig(name="voice", kind="bidi", description="Voice"))
    source.put_workflow_config(
        SwarmWorkflowConfig(name="triage", agents=["a"], description="Routes")
    )

    targets = {t.name: t for t in source.list_targets()}

    assert set(targets) == {"billing", "voice", "triage"}  # __app__ excluded
    assert targets["billing"].kind == "agent"
    assert targets["billing"].description == "Invoices"
    assert targets["voice"].kind == "bidi"
    assert targets["triage"].kind == "workflow"
    assert targets["triage"].description == "Routes"


def test_list_targets_sorted_by_name(source):
    source.put_agent_config(AgentConfig(name="zeta"))
    source.put_agent_config(AgentConfig(name="alpha"))
    assert [t.name for t in source.list_targets()] == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# get_target_kind
# ---------------------------------------------------------------------------


def test_target_kind_resolves_agent_workflow_and_bidi(source):
    source.put_agent_config(AgentConfig(name="a"))
    source.put_agent_config(AgentConfig(name="v", kind="bidi"))
    source.put_workflow_config(SwarmWorkflowConfig(name="w", agents=["a"]))

    assert source.get_target_kind("a") == "agent"
    assert source.get_target_kind("v") == "bidi"
    assert source.get_target_kind("w") == "workflow"


def test_target_kind_narrowing_mismatch_raises(source):
    source.put_agent_config(AgentConfig(name="a"))
    with pytest.raises(TargetResolutionError):
        source.get_target_kind("a", "workflow")


def test_target_kind_unknown_name_raises(source):
    with pytest.raises(TargetResolutionError, match="neither an agent nor a workflow"):
        source.get_target_kind("ghost")


# ---------------------------------------------------------------------------
# Migration helper
# ---------------------------------------------------------------------------


class FakeFileSource:
    """Minimal file-source stand-in: canned hydrated configs + catalog."""

    def __init__(self) -> None:
        from loom.schemas.target import TargetInfo

        self._agents = {
            "billing": AgentConfig(name="billing", description="Invoices"),
        }
        self._workflows = {
            "triage": SwarmWorkflowConfig(name="triage", agents=["billing"], description="Routes"),
        }
        self._targets = [
            TargetInfo("billing", "agent", "Invoices"),
            TargetInfo("triage", "workflow", "Routes"),
        ]

    def get_app_config(self, env: str | None = None) -> AppConfig:
        return AppConfig()

    def get_agent_config(self, name: str) -> AgentConfig:
        return self._agents[name]

    def get_workflow_config(self, name: str):
        return self._workflows[name]

    def list_targets(self):
        return self._targets


def test_migrate_from_file_source_seeds_every_target(source):
    written = migrate_from_file_source(FakeFileSource(), source)

    assert set(written) == {"billing", "triage"}
    # Targets are now readable straight from Dynamo, re-validated.
    assert source.get_agent_config("billing").description == "Invoices"
    assert source.get_workflow_config("triage").agents == ["billing"]
    assert source.get_app_config() == AppConfig()
