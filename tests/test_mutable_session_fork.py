"""Tests for the session-backed fork (Model C) + the mutable session store."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from loom import LoomSession
from strands.types.session import SessionMessage

from weave.adapters.driven.mutable_file_session import MutableFileSessionManager
from weave.application.errors import HarnessError
from weave.application.reply import AgentReply
from weave.application.run import fork
from weave.ports.session import MutableSession

# --- the reference mutable store (subclass + truncate) -------------------------


def _seed_messages(mgr: MutableFileSessionManager, session_id: str, n: int) -> None:
    """Write n message files directly via the manager's repository methods."""
    import os

    for i in range(n):
        path = mgr._get_message_path(session_id, "default", i)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sm = SessionMessage.from_message({"role": "user", "content": [{"text": f"m{i}"}]}, i)
        mgr._write_file(path, sm.to_dict())


def test_mutable_file_session_truncate_deletes_after_keep(tmp_path):
    mgr = MutableFileSessionManager(session_id="s1", storage_dir=str(tmp_path))
    _seed_messages(mgr, "s1", 5)
    assert len(mgr.list_messages("s1", "default")) == 5

    mgr.truncate("s1", keep=2)

    remaining = [m.message_id for m in mgr.list_messages("s1", "default")]
    assert remaining == [0, 1]


def test_mutable_file_session_truncate_negative_keep(tmp_path):
    mgr = MutableFileSessionManager(session_id="s1", storage_dir=str(tmp_path))
    _seed_messages(mgr, "s1", 5)

    mgr.truncate("s1", keep=-2)  # drop the last two

    remaining = [m.message_id for m in mgr.list_messages("s1", "default")]
    assert remaining == [0, 1, 2]


def test_mutable_file_session_is_a_mutable_session():
    mgr = MutableFileSessionManager(session_id="s1", storage_dir="/tmp/x")
    assert isinstance(mgr, MutableSession)


# --- fork (Model C) orchestration ---------------------------------------------


class _FakeAgent:
    def __init__(self) -> None:
        self.cleaned_up = False
        self.invoked_with: dict[str, Any] = {}

    async def invoke_async(self, prompt, *, invocation_state=None):
        self.invoked_with = {"prompt": prompt, "invocation_state": invocation_state}
        return SimpleNamespace(
            message={"content": [{"text": "after fork"}]},
            structured_output=None,
            stop_reason="end_turn",
            interrupts=[],
            metrics=SimpleNamespace(get_summary=lambda: {}),
        )

    def cleanup(self) -> None:
        self.cleaned_up = True


class _FakeEngine:
    def __init__(self, agent: _FakeAgent) -> None:
        self.agent = agent
        self.built = 0

    def build_agent(self, name: str, session: LoomSession, *, model_params=None):
        self.built += 1
        self.last_model_params = model_params
        return self.agent


class _FakeStore:
    """A MutableSession that records the truncate call."""

    def __init__(self) -> None:
        self.truncated: dict[str, Any] | None = None

    def truncate(self, session_id: str, *, keep: int, agent_id: str = "default") -> None:
        self.truncated = {"session_id": session_id, "keep": keep, "agent_id": agent_id}


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


def test_fork_truncates_then_builds_then_runs():
    agent = _FakeAgent()
    engine = _FakeEngine(agent)
    store = _FakeStore()

    reply = asyncio.run(fork(engine, store, "assistant", "edited q", _session(), at=2))

    # 1. truncated the persisted session to keep=2; 2. built the (re-synced) agent; 3. ran.
    assert store.truncated == {"session_id": "s-1", "keep": 2, "agent_id": "default"}
    assert engine.built == 1
    assert isinstance(reply, AgentReply)
    assert reply.text == "after fork"
    assert agent.cleaned_up is True


def test_fork_forwards_extras():
    agent = _FakeAgent()
    asyncio.run(
        fork(_FakeEngine(agent), _FakeStore(), "a", "q", _session(), at=0, extras={"k": "v"})
    )
    assert agent.invoked_with["invocation_state"] == {"k": "v"}


def test_fork_rejects_non_mutable_store():
    class _PlainStore:  # no truncate
        pass

    with pytest.raises(HarnessError, match="mutable session store"):
        asyncio.run(fork(_FakeEngine(_FakeAgent()), _PlainStore(), "a", "q", _session(), at=1))
