"""Tests for Weave's library logging — namespace, levels, and the NullHandler default.

Weave logs under the ``weave.*`` namespace (mirroring Loom's ``loom.*``) so an
application can capture both with one handler bridge (e.g. AWS Lambda Powertools).
It never configures a sink itself; the only handler it attaches is a NullHandler.
"""

from __future__ import annotations

import asyncio
import logging

from loom.errors import AgentNotFoundError

from weave.adapters.driving.chat import ChatChannel, chat_router
from weave.application import run as run_module
from weave.application.reply import AgentReply, Usage


def _reply() -> AgentReply:
    return AgentReply(text="hi", usage=Usage(), session_id="s-1")


class _Agent:
    """Minimal stand-in for a built Strands agent (invoke + cleanup)."""

    def __init__(self) -> None:
        self.cleaned = False

    async def invoke_async(self, input, invocation_state=None):
        return object()  # shaped by a patched reply factory below

    def cleanup(self) -> None:
        self.cleaned = True


class _Engine:
    def build_agent(self, name, session):
        return _Agent()

    def discover(self):
        from loom import TargetInfo

        return [TargetInfo(name="assistant", kind="agent", description="")]


def test_weave_root_has_only_a_null_handler():
    # Importing Weave must not configure a sink — the one handler is the NullHandler.
    handlers = logging.getLogger("weave").handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.NullHandler)


def test_run_agent_emits_lifecycle_under_weave_namespace(monkeypatch, caplog):
    # The shaped reply is irrelevant here; stub the factory so _Agent can stay tiny.
    monkeypatch.setattr(run_module, "agent_reply_from_result", lambda result, sid: _reply())

    with caplog.at_level(logging.DEBUG, logger="weave"):
        asyncio.run(run_module.run(_Engine(), "assistant", "hi", _session()))

    records = [r for r in caplog.records if r.name.startswith("weave.")]
    messages = " ".join(r.getMessage() for r in records)
    assert "building agent 'assistant'" in messages
    assert "completed turn" in messages
    assert "tore down agent 'assistant'" in messages  # teardown is always logged
    assert all(r.name.startswith("weave.") for r in records)


def test_lambda_500_path_logs_the_traceback(caplog):
    from weave.adapters.driving.lambda_handler import lambda_handler

    async def _boom(name, input, session, *, kind=None, extras=None):
        raise RuntimeError("unexpected")

    handler = lambda_handler(ChatChannel("assistant", _boom))

    with caplog.at_level(logging.ERROR, logger="weave"):
        resp = handler({"body": '{"message": "hi", "session_id": "s-1"}'}, None)

    assert resp["statusCode"] == 500
    # The swallowed traceback must survive in the log (exc_info attached).
    err = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert err and err[0].exc_info is not None


def test_chat_router_logs_loom_error_as_warning(caplog):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def _runner(name, input, session, *, kind=None, extras=None):
        raise AgentNotFoundError("support_agent")

    app = FastAPI()
    app.include_router(chat_router(ChatChannel("support_agent", _runner)))

    with caplog.at_level(logging.WARNING, logger="weave"):
        TestClient(app).post("/chat", json={"message": "hi", "session_id": "s-1"})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("agent unavailable" in r.getMessage() for r in warnings)


def _session():
    from loom import LoomSession

    return LoomSession(user_id="u-1", session_id="s-1", is_anonymous=False)
