"""Tests for the minimal one-shot executor (weave.application.run.run_agent)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from loom import LoomSession

from weave.application.errors import HarnessError
from weave.application.reply import AgentReply
from weave.application.run import run_agent


def _result(text: str = "hi", *, interrupts: tuple = ()) -> SimpleNamespace:
    """A stand-in for a Strands AgentResult with the fields the projection reads."""
    return SimpleNamespace(
        message={"content": [{"text": text}]},
        structured_output=None,
        stop_reason="end_turn",
        interrupts=list(interrupts),
        metrics=SimpleNamespace(
            get_summary=lambda: {
                "accumulated_usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                "total_duration": 123.0,
                "tool_usage": {"search": {}},
            }
        ),
    )


class _FakeAgent:
    """A built agent: records the invoke call and whether cleanup ran."""

    def __init__(self, result: Any = None, *, fail: bool = False) -> None:
        self._result = result if result is not None else _result()
        self._fail = fail
        self.invoked_with: dict[str, Any] = {}
        self.cleaned_up = False

    async def invoke_async(self, prompt, *, invocation_state=None):
        self.invoked_with = {"prompt": prompt, "invocation_state": invocation_state}
        if self._fail:
            raise RuntimeError("boom")
        return self._result

    def cleanup(self) -> None:
        self.cleaned_up = True


class _FakeEngine:
    """An AgentEngine that hands back a prebuilt fake agent."""

    def __init__(self, agent: _FakeAgent) -> None:
        self.agent = agent
        self.built_with: dict[str, Any] = {}

    def build_agent(self, name: str, session: LoomSession, *, model_params=None):
        self.built_with = {"name": name, "session": session, "model_params": model_params}
        return self.agent


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


def test_run_agent_projects_reply():
    agent = _FakeAgent(_result(text="Paris"))
    reply = asyncio.run(
        run_agent(_FakeEngine(agent), "assistant", "capital of France?", _session())
    )

    assert isinstance(reply, AgentReply)
    assert reply.text == "Paris"
    assert reply.stop_reason == "end_turn"
    assert reply.session_id == "s-1"
    assert reply.usage.total_tokens == 15
    assert reply.usage.tool_calls == ["search"]
    assert reply.raw is agent._result


def test_run_agent_forwards_extras_as_invocation_state():
    agent = _FakeAgent()
    asyncio.run(run_agent(_FakeEngine(agent), "assistant", "hi", _session(), extras={"jwt": "tok"}))
    assert agent.invoked_with["invocation_state"] == {"jwt": "tok"}


def test_run_agent_defaults_invocation_state_to_empty():
    agent = _FakeAgent()
    asyncio.run(run_agent(_FakeEngine(agent), "assistant", "hi", _session()))
    assert agent.invoked_with["invocation_state"] == {}


def test_run_agent_forwards_model_params_to_build_agent():
    agent = _FakeAgent()
    engine = _FakeEngine(agent)
    overlay = {"boto_session": object()}
    asyncio.run(run_agent(engine, "assistant", "hi", _session(), model_params=overlay))
    assert engine.built_with["model_params"] is overlay


def test_run_agent_defaults_model_params_to_none():
    agent = _FakeAgent()
    engine = _FakeEngine(agent)
    asyncio.run(run_agent(engine, "assistant", "hi", _session()))
    assert engine.built_with["model_params"] is None


def test_run_agent_always_cleans_up_on_success():
    agent = _FakeAgent()
    asyncio.run(run_agent(_FakeEngine(agent), "assistant", "hi", _session()))
    assert agent.cleaned_up is True


def test_run_agent_cleans_up_and_wraps_error_on_failure():
    agent = _FakeAgent(fail=True)
    with pytest.raises(HarnessError, match="failed during execution"):
        asyncio.run(run_agent(_FakeEngine(agent), "assistant", "hi", _session()))
    assert agent.cleaned_up is True  # teardown ran even though the turn failed


def test_run_agent_reports_interrupts():
    agent = _FakeAgent(_result(interrupts=({"id": "i1", "name": "approve", "reason": "why"},)))
    reply = asyncio.run(run_agent(_FakeEngine(agent), "assistant", "hi", _session()))
    assert [i.id for i in reply.interrupts] == ["i1"]
    assert reply.interrupts[0].name == "approve"
