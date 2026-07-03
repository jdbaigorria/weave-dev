"""Tests for catalog() and the run() kind dispatcher."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from loom import LoomSession, TargetInfo

from weave.application.errors import HarnessError
from weave.application.reply import AgentReply, WorkflowReply
from weave.application.run import catalog, run


def _agent_result() -> SimpleNamespace:
    return SimpleNamespace(
        message={"content": [{"text": "hi"}]},
        structured_output=None,
        stop_reason="end_turn",
        interrupts=[],
        metrics=SimpleNamespace(get_summary=lambda: {}),
    )


def _workflow_result() -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(name="COMPLETED"),
        results={
            "only": SimpleNamespace(result=SimpleNamespace(message={"content": [{"text": "w"}]}))
        },
        accumulated_usage={},
        accumulated_metrics={},
        interrupts=[],
    )


class _Agent:
    async def invoke_async(self, prompt, *, invocation_state=None):
        return _agent_result()

    def cleanup(self) -> None:
        pass


class _Workflow:
    async def invoke_async(self, task, invocation_state=None):
        return _workflow_result()


class _NodeAgent:
    def cleanup(self) -> None:
        pass


class _Engine:
    """A fake engine with a catalog and both builders."""

    def __init__(self, targets: list[TargetInfo]) -> None:
        self._targets = targets

    def discover(self) -> list[TargetInfo]:
        return self._targets

    def build_agent(self, name: str, session: LoomSession, *, model_params=None):
        return _Agent()

    def build_workflow(self, name: str, session: LoomSession, *, model_params=None):
        return _Workflow(), [_NodeAgent()]


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


_CATALOG = [
    TargetInfo("chat", "agent", "an agent"),
    TargetInfo("pipeline", "workflow", "a workflow"),
    TargetInfo("voice", "bidi", "a bidi agent"),
]


def test_catalog_passes_through_discover():
    assert catalog(_Engine(_CATALOG)) == _CATALOG


def test_run_dispatches_agent():
    reply = asyncio.run(run(_Engine(_CATALOG), "chat", "hi", _session()))
    assert isinstance(reply, AgentReply)
    assert reply.text == "hi"


def test_run_dispatches_workflow():
    reply = asyncio.run(run(_Engine(_CATALOG), "pipeline", "go", _session()))
    assert isinstance(reply, WorkflowReply)
    assert reply.status == "COMPLETED"


def test_run_explicit_kind_skips_discovery():
    # Empty catalog → discovery would fail; explicit kind must bypass it.
    reply = asyncio.run(run(_Engine([]), "chat", "hi", _session(), kind="agent"))
    assert isinstance(reply, AgentReply)


def test_run_rejects_bidi():
    with pytest.raises(HarnessError, match="full-duplex"):
        asyncio.run(run(_Engine(_CATALOG), "voice", "hi", _session()))


def test_run_unknown_target_raises():
    with pytest.raises(HarnessError, match="unknown target"):
        asyncio.run(run(_Engine(_CATALOG), "ghost", "hi", _session()))
