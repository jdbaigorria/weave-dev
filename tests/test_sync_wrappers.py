"""Tests for the synchronous wrappers (weave.composition.*_sync).

The harness is async-first; these wrappers let a caller without an event loop of
its own (a Lambda handler) run a turn without writing ``asyncio.run`` by hand.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from loom import LoomSession

import weave.composition as comp
from weave.application.errors import HarnessError
from weave.application.reply import AgentReply
from weave.composition import _block_on, run_agent_sync


def _result(text: str = "hi") -> SimpleNamespace:
    """A stand-in for a Strands AgentResult with the fields the projection reads."""
    return SimpleNamespace(
        message={"content": [{"text": text}]},
        structured_output=None,
        stop_reason="end_turn",
        interrupts=[],
        metrics=SimpleNamespace(
            get_summary=lambda: {
                "accumulated_usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                "total_duration": 1.0,
                "tool_usage": {},
            }
        ),
    )


class _FakeAgent:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.cleaned_up = False

    async def invoke_async(self, prompt, *, invocation_state=None):
        return self._result

    def cleanup(self) -> None:
        self.cleaned_up = True


class _FakeEngine:
    """An AgentEngine that hands back a prebuilt fake agent (no Bedrock)."""

    def __init__(self, agent: _FakeAgent) -> None:
        self.agent = agent

    def build_agent(self, name: str, session: LoomSession):
        return self.agent


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


def test_block_on_runs_a_coroutine_to_completion():
    async def coro() -> int:
        return 42

    assert _block_on(coro()) == 42


def test_block_on_errors_inside_a_running_loop():
    async def coro() -> int:
        return 1

    async def driver() -> None:
        # We are inside a running loop here — blocking on it must be refused.
        c = coro()
        with pytest.raises(HarnessError, match="running event loop"):
            _block_on(c)
        c.close()  # never awaited (the call was refused); close to silence the warning

    asyncio.run(driver())


def test_run_agent_sync_returns_reply(monkeypatch):
    monkeypatch.setattr(comp, "_engine", _FakeEngine(_FakeAgent(_result("Paris"))))

    reply = run_agent_sync("assistant", "capital of France?", _session())

    assert isinstance(reply, AgentReply)
    assert reply.text == "Paris"
    assert reply.session_id == "s-1"
    assert reply.usage.total_tokens == 15
