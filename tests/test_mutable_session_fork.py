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
from weave.application.run import fork, run_agent
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

    mgr.truncate("s1", keep=2, agent_id="default")

    remaining = [m.message_id for m in mgr.list_messages("s1", "default")]
    assert remaining == [0, 1]


def test_mutable_file_session_truncate_negative_keep(tmp_path):
    mgr = MutableFileSessionManager(session_id="s1", storage_dir=str(tmp_path))
    _seed_messages(mgr, "s1", 5)

    mgr.truncate("s1", keep=-2, agent_id="default")  # drop the last two

    remaining = [m.message_id for m in mgr.list_messages("s1", "default")]
    assert remaining == [0, 1, 2]


def test_mutable_file_session_is_a_mutable_session():
    mgr = MutableFileSessionManager(session_id="s1", storage_dir="/tmp/x")
    assert isinstance(mgr, MutableSession)


# --- fork (Model C) orchestration ---------------------------------------------


class _FakeAgent:
    def __init__(self, agent_id: str = "assistant") -> None:
        self.agent_id = agent_id
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

    def __init__(self, *, fail: bool = False) -> None:
        self.truncated: dict[str, Any] | None = None
        self.fail = fail

    def truncate(self, session_id: str, *, keep: int, agent_id: str) -> None:
        if self.fail:
            raise OSError("disk gone")
        self.truncated = {"session_id": session_id, "keep": keep, "agent_id": agent_id}


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


def test_fork_truncates_then_builds_then_runs():
    agent = _FakeAgent()
    engine = _FakeEngine(agent)
    store = _FakeStore()

    reply = asyncio.run(fork(engine, store, "assistant", "edited q", _session(), at=2))

    # 1. truncated the persisted session to keep=2; 2. built the (re-synced) agent; 3. ran.
    assert store.truncated == {"session_id": "s-1", "keep": 2, "agent_id": "assistant"}
    assert engine.built == 1
    assert isinstance(reply, AgentReply)
    assert reply.text == "after fork"
    assert agent.cleaned_up is True


def test_fork_forwards_extras():
    agent = _FakeAgent()
    asyncio.run(
        fork(
            _FakeEngine(agent), _FakeStore(), "assistant", "q", _session(), at=0, extras={"k": "v"}
        )
    )
    assert agent.invoked_with["invocation_state"] == {"k": "v"}


def test_fork_rejects_non_mutable_store():
    class _PlainStore:  # no truncate
        pass

    with pytest.raises(HarnessError, match="mutable session store"):
        asyncio.run(fork(_FakeEngine(_FakeAgent()), _PlainStore(), "a", "q", _session(), at=1))


# --- W9: the agent_id is resolved from the name and checked against the build ---


def test_truncate_needs_an_agent_id(tmp_path):
    mgr = MutableFileSessionManager(session_id="s1", storage_dir=str(tmp_path))
    with pytest.raises(TypeError):
        mgr.truncate("s1", keep=0)  # type: ignore[call-arg]


def test_fork_explicit_agent_id_reaches_the_store():
    store = _FakeStore()
    asyncio.run(
        fork(
            _FakeEngine(_FakeAgent("custom")),
            store,
            "assistant",
            "q",
            _session(),
            at=0,
            agent_id="custom",
        )
    )
    assert store.truncated["agent_id"] == "custom"


def test_fork_fails_before_invoking_when_the_built_agent_id_differs():
    agent = _FakeAgent("legacy")
    with pytest.raises(HarnessError, match="agent_id='legacy'"):
        asyncio.run(fork(_FakeEngine(agent), _FakeStore(), "assistant", "q", _session(), at=0))
    assert agent.cleaned_up is True
    assert agent.invoked_with == {}


def test_fork_wraps_a_store_failure_and_does_not_build():
    engine = _FakeEngine(_FakeAgent())
    with pytest.raises(HarnessError, match="could not truncate") as info:
        asyncio.run(fork(engine, _FakeStore(fail=True), "assistant", "q", _session(), at=0))
    assert isinstance(info.value.__cause__, OSError)
    assert engine.built == 0


def test_fork_regenerates_the_last_exchange_with_loom(file_engine):
    engine, store_for = file_engine
    session = LoomSession(session_id="s1", user_id="u1")
    asyncio.run(run_agent(engine, "assistant", "pregunta 1", session))
    asyncio.run(run_agent(engine, "assistant", "pregunta 2", session))

    asyncio.run(fork(engine, store_for("s1"), "assistant", "pregunta 2 (regenerar)", session, at=2))

    texts = [
        m.message["content"][0]["text"] for m in store_for("s1").list_messages("s1", "assistant")
    ]
    assert texts[0] == "pregunta 1"
    assert texts[2] == "pregunta 2 (regenerar)"
    assert len(texts) == 4
