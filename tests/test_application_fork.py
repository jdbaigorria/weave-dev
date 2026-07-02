"""Tests for fork_stateless — regenerate/edit/explore over a caller-owned transcript."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from loom import LoomSession

from weave.application.errors import HarnessError
from weave.application.reply import AgentReply
from weave.application.run import fork_stateless


def _result(text: str = "new answer") -> SimpleNamespace:
    return SimpleNamespace(
        message={"content": [{"text": text}]},
        structured_output=None,
        stop_reason="end_turn",
        interrupts=[],
        metrics=SimpleNamespace(get_summary=lambda: {}),
    )


class _FakeAgent:
    """Records the seeded messages; invoke_async appends the new exchange to them."""

    def __init__(self, result: Any, *, fail: bool = False) -> None:
        self._result = result
        self._fail = fail
        self.messages: list[Any] = []
        self.cleaned_up = False
        self.seeded: list[Any] | None = None
        self.invoked_with: dict[str, Any] = {}

    async def invoke_async(self, prompt, *, invocation_state=None):
        self.seeded = list(self.messages)  # capture what fork seeded before running
        self.invoked_with = {"prompt": prompt, "invocation_state": invocation_state}
        if self._fail:
            raise RuntimeError("boom")
        self.messages.append({"role": "user", "content": prompt})
        self.messages.append(
            {"role": "assistant", "content": self._result.message["content"][0]["text"]}
        )
        return self._result

    def cleanup(self) -> None:
        self.cleaned_up = True


class _FakeEngine:
    def __init__(self, agent: _FakeAgent) -> None:
        self.agent = agent

    def build_agent(self, name: str, session: LoomSession):
        return self.agent


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


_HISTORY = [
    {"role": "user", "content": "q1"},
    {"role": "assistant", "content": "a1"},
    {"role": "user", "content": "q2"},
    {"role": "assistant", "content": "a2"},
]


def test_fork_truncates_at_and_returns_new_transcript():
    agent = _FakeAgent(_result("a2-prime"))
    (reply, transcript) = asyncio.run(
        fork_stateless(_FakeEngine(agent), "assistant", _HISTORY, "q2", _session(), at=2)
    )
    # Seeded with history[:2] (q1/a1); then ran q2 → q2/a2-prime appended.
    assert agent.seeded == _HISTORY[:2]
    assert isinstance(reply, AgentReply)
    assert reply.text == "a2-prime"
    assert transcript == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2-prime"},
    ]


def test_fork_regenerate_drops_last_exchange():
    agent = _FakeAgent(_result("a2-regen"))
    # regenerate q2: drop q2+a2 (at=-2), replay "q2"
    (reply, transcript) = asyncio.run(
        fork_stateless(_FakeEngine(agent), "assistant", _HISTORY, "q2", _session(), at=-2)
    )
    assert agent.seeded == _HISTORY[:2]
    assert transcript[-1] == {"role": "assistant", "content": "a2-regen"}
    assert len(transcript) == 4  # no duplicated turn


def test_fork_at_none_keeps_full_history():
    agent = _FakeAgent(_result())
    asyncio.run(fork_stateless(_FakeEngine(agent), "assistant", _HISTORY, "q3", _session()))
    assert agent.seeded == _HISTORY  # full history kept


def test_fork_forwards_extras_as_invocation_state():
    agent = _FakeAgent(_result())
    asyncio.run(
        fork_stateless(
            _FakeEngine(agent), "assistant", _HISTORY, "q", _session(), extras={"k": "v"}
        )
    )
    assert agent.invoked_with["invocation_state"] == {"k": "v"}


def test_fork_cleans_up_and_wraps_error_on_failure():
    agent = _FakeAgent(_result(), fail=True)
    with pytest.raises(HarnessError, match="fork of 'assistant' failed"):
        asyncio.run(
            fork_stateless(_FakeEngine(agent), "assistant", _HISTORY, "q", _session(), at=2)
        )
    assert agent.cleaned_up is True
