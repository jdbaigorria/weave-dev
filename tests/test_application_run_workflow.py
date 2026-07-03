"""Tests for the workflow executor (weave.application.run.run_workflow)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from loom import LoomSession

from weave.application.errors import HarnessError
from weave.application.reply import WorkflowReply
from weave.application.run import run_workflow


def _node(text: str) -> SimpleNamespace:
    """A NodeResult whose .result is an AgentResult-like with one text block."""
    return SimpleNamespace(result=SimpleNamespace(message={"content": [{"text": text}]}))


def _result(status: str = "COMPLETED", nodes: dict | None = None) -> SimpleNamespace:
    """Stand-in for a Strands MultiAgentResult."""
    nodes = nodes if nodes is not None else {"triage": _node("routed"), "billing": _node("done")}
    return SimpleNamespace(
        status=SimpleNamespace(name=status),
        results=nodes,
        accumulated_usage={"inputTokens": 30, "outputTokens": 12, "totalTokens": 42},
        accumulated_metrics={"latencyMs": 200},
        interrupts=[],
    )


class _FakeWorkflow:
    def __init__(self, result: Any, *, fail: bool = False) -> None:
        self._result = result
        self._fail = fail
        self.invoked_with: dict[str, Any] = {}

    async def invoke_async(self, task, invocation_state=None):
        self.invoked_with = {"task": task, "invocation_state": invocation_state}
        if self._fail:
            raise RuntimeError("boom")
        return self._result


class _FakeNodeAgent:
    def __init__(self) -> None:
        self.cleaned_up = False

    def cleanup(self) -> None:
        self.cleaned_up = True


class _FakeEngine:
    def __init__(self, workflow: _FakeWorkflow, node_agents: list[_FakeNodeAgent]) -> None:
        self._workflow = workflow
        self._node_agents = node_agents
        self.built_with: dict = {}

    def build_workflow(self, name: str, session: LoomSession, *, model_params=None):
        self.built_with = {"name": name, "session": session, "model_params": model_params}
        return self._workflow, self._node_agents


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


def test_run_workflow_projects_reply():
    nodes = [_FakeNodeAgent(), _FakeNodeAgent()]
    engine = _FakeEngine(_FakeWorkflow(_result()), nodes)
    reply = asyncio.run(run_workflow(engine, "support", "help", _session()))

    assert isinstance(reply, WorkflowReply)
    assert reply.text == "done"  # terminal (last) node text
    assert reply.status == "COMPLETED"
    assert reply.usage.total_tokens == 42
    assert reply.usage.latency_ms == 200.0
    assert reply.execution_order == ["triage", "billing"]
    assert reply.session_id == "s-1"


def test_run_workflow_forwards_model_params_to_build_workflow():
    engine = _FakeEngine(_FakeWorkflow(_result()), [_FakeNodeAgent()])
    overlay = {"boto_session": object()}
    asyncio.run(run_workflow(engine, "support", "help", _session(), model_params=overlay))
    assert engine.built_with["model_params"] is overlay


def test_run_workflow_forwards_extras_as_invocation_state():
    wf = _FakeWorkflow(_result())
    engine = _FakeEngine(wf, [_FakeNodeAgent()])
    asyncio.run(run_workflow(engine, "support", "help", _session(), extras={"tenant": "acme"}))
    assert wf.invoked_with["invocation_state"] == {"tenant": "acme"}


def test_run_workflow_cleans_up_every_node_on_success():
    nodes = [_FakeNodeAgent(), _FakeNodeAgent()]
    engine = _FakeEngine(_FakeWorkflow(_result()), nodes)
    asyncio.run(run_workflow(engine, "support", "help", _session()))
    assert all(n.cleaned_up for n in nodes)


def test_run_workflow_cleans_up_and_wraps_error_on_failure():
    nodes = [_FakeNodeAgent(), _FakeNodeAgent()]
    engine = _FakeEngine(_FakeWorkflow(_result(), fail=True), nodes)
    with pytest.raises(HarnessError, match="failed during execution"):
        asyncio.run(run_workflow(engine, "support", "help", _session()))
    assert all(n.cleaned_up for n in nodes)  # teardown ran for every node


def test_run_workflow_surfaces_status_and_no_terminal_text():
    nodes = {"a": SimpleNamespace(result=SimpleNamespace(message={"content": []}))}
    engine = _FakeEngine(_FakeWorkflow(_result(status="FAILED", nodes=nodes)), [_FakeNodeAgent()])
    reply = asyncio.run(run_workflow(engine, "support", "help", _session()))
    assert reply.status == "FAILED"
    assert reply.text == ""
