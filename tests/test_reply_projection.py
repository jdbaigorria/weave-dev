"""Tests for the reply projection: Usage latency units (W1) and reply identity (W2)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from loom import LoomSession, TargetInfo

import weave
from weave import composition
from weave.adapters.driving.chat import ChatRequest
from weave.adapters.driving.identity import mint_if_absent
from weave.adapters.driving.lambda_handler import lambda_handler
from weave.adapters.driving.routing import by_key
from weave.application.reply import (
    AgentReply,
    WorkflowReply,
    agent_reply_from_result,
    workflow_reply_from_result,
)
from weave.application.run import (
    fork,
    fork_stateless,
    run_agent,
    run_workflow,
    stream_agent,
)

# --- W1: Usage.latency_ms / model_latency_ms ------------------------------------


def _agent_result(summary: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        message={"content": [{"text": "hi"}]},
        structured_output=None,
        stop_reason="end_turn",
        interrupts=[],
        metrics=SimpleNamespace(get_summary=lambda: summary),
    )


def _project(result: Any) -> AgentReply:
    return agent_reply_from_result(result, LoomSession(session_id="s", user_id="u"), target="a")


def test_agent_latency_is_wall_clock_in_milliseconds():
    reply = _project(_agent_result({"total_duration": 1.5}))
    assert reply.usage.latency_ms == 1500.0


def test_agent_model_latency_comes_from_accumulated_metrics():
    reply = _project(_agent_result({"accumulated_metrics": {"latencyMs": 820}}))
    assert reply.usage.model_latency_ms == 820.0


def test_workflow_latency_is_execution_time_and_model_latency_is_separate():
    result = SimpleNamespace(
        status=SimpleNamespace(name="COMPLETED"),
        results={},
        accumulated_usage={},
        accumulated_metrics={"latencyMs": 900},
        execution_time=2300,
        interrupts=[],
    )
    reply = workflow_reply_from_result(result, LoomSession(session_id="s", user_id="u"), target="w")
    assert reply.usage.latency_ms == 2300.0
    assert reply.usage.model_latency_ms == 900.0


def test_missing_latency_keys_default_to_zero():
    agent = _project(_agent_result({}))
    assert (agent.usage.latency_ms, agent.usage.model_latency_ms) == (0.0, 0.0)

    workflow = workflow_reply_from_result(
        SimpleNamespace(status=None, results={}, interrupts=[]),
        LoomSession(session_id="s", user_id="u"),
        target="w",
    )
    assert (workflow.usage.latency_ms, workflow.usage.model_latency_ms) == (0.0, 0.0)


# --- W2: target / user_id / is_anonymous on the reply ---------------------------


class _Agent:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def invoke_async(self, prompt, *, invocation_state=None):
        return _agent_result({})

    async def stream_async(self, prompt, *, invocation_state=None):
        yield {"result": _agent_result({})}

    def cleanup(self) -> None:
        pass


class _Workflow:
    async def invoke_async(self, task, invocation_state=None):
        return SimpleNamespace(
            status=SimpleNamespace(name="COMPLETED"),
            results={},
            accumulated_usage={},
            accumulated_metrics={},
            interrupts=[],
        )


class _Engine:
    def build_agent(self, name, session, *, model_params=None):
        return _Agent()

    def build_workflow(self, name, session, *, model_params=None):
        return _Workflow(), []

    def discover(self):
        return [
            TargetInfo(name="sales_agent", kind="agent"),
            TargetInfo(name="support_agent", kind="agent"),
        ]


class _Store:
    def truncate(self, session_id, *, keep, agent_id="default") -> None:
        pass


_ANON = LoomSession(session_id="s-9", user_id="anon_s-9", is_anonymous=True)


def _assert_identity(reply: AgentReply | WorkflowReply, target: str) -> None:
    assert reply.target == target
    assert reply.session_id == "s-9"
    assert reply.user_id == "anon_s-9"
    assert reply.is_anonymous is True


def test_run_agent_reports_target_and_identity():
    _assert_identity(asyncio.run(run_agent(_Engine(), "sales_agent", "hi", _ANON)), "sales_agent")


def test_run_workflow_reports_target_and_identity():
    _assert_identity(asyncio.run(run_workflow(_Engine(), "flow", "hi", _ANON)), "flow")


def test_fork_reports_target_and_identity():
    reply = asyncio.run(fork(_Engine(), _Store(), "sales_agent", "hi", _ANON, at=0))
    _assert_identity(reply, "sales_agent")


def test_fork_stateless_reports_target_and_identity():
    reply, _ = asyncio.run(fork_stateless(_Engine(), "sales_agent", [], "hi", _ANON))
    _assert_identity(reply, "sales_agent")


def test_stream_agent_done_event_reports_target_and_identity():
    async def _drain():
        return [e async for e in stream_agent(_Engine(), "sales_agent", "hi", _ANON)]

    done = asyncio.run(_drain())[-1]
    assert done.type == "done"
    _assert_identity(done.reply, "sales_agent")


@pytest.fixture
def _engine():
    weave.configure(engine=_Engine())
    yield
    weave.configure()


def test_routed_channel_reports_the_resolved_target_not_the_requested_one(_engine):
    channel = composition.routed_channel(
        by_key(lambda r: r.extras["dept"], {"SALES": "sales_agent"}, default="support_agent"),
        session_policy=mint_if_absent(anon_prefix="anon_"),
    )

    reply = asyncio.run(channel.handle(ChatRequest(message="hi", extras={"dept": "SALES"})))
    assert reply.target == "sales_agent"
    assert reply.is_anonymous is True
    assert reply.user_id == f"anon_{reply.session_id}"

    fallback = asyncio.run(channel.handle(ChatRequest(message="hi", extras={"dept": "?"})))
    assert fallback.target == "support_agent"


def test_lambda_json_carries_the_identity_fields_and_no_raw(_engine):
    handler = lambda_handler(
        composition.chat_channel("sales_agent", session_policy=mint_if_absent(anon_prefix="a_"))
    )

    resp = handler({"body": json.dumps({"message": "hi", "session_id": "s-1"})}, None)

    body = json.loads(resp["body"])
    assert (body["target"], body["user_id"], body["is_anonymous"]) == ("sales_agent", "a_s-1", True)
    assert "raw" not in body


def test_hand_built_reply_still_works_with_defaults():
    reply = AgentReply(text="x", usage=weave.Usage(), session_id="s")
    assert (reply.target, reply.user_id, reply.is_anonymous) == (None, None, False)


def test_chat_router_json_carries_the_identity_fields_and_no_raw(_engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from weave.adapters.driving.chat import chat_router

    app = FastAPI()
    app.include_router(
        chat_router(
            composition.chat_channel("sales_agent", session_policy=mint_if_absent(anon_prefix="a_"))
        )
    )

    body = TestClient(app).post("/chat", json={"message": "hi", "session_id": "s-1"}).json()

    assert (body["target"], body["user_id"], body["is_anonymous"]) == ("sales_agent", "a_s-1", True)
    assert "raw" not in body
