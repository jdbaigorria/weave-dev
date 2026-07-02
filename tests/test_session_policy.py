"""Tests for session policies — strict require() (default) and mint_if_absent().

Session lifetime is Weave's domain, so the channel owns minting; the product injects
the policy. The default is strict: a request without a session_id is a 400, not a
silently-new conversation.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from weave.adapters.driving.chat import ChatChannel, ChatRequest, chat_router
from weave.adapters.driving.identity import mint_if_absent, require
from weave.application.errors import RequestError
from weave.application.reply import AgentReply, Usage


class _Runner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, name, input, session, *, kind=None, extras=None):
        self.calls.append({"session": session})
        return AgentReply(text="ok", usage=Usage(), session_id=session.session_id)


# --- strict require() (the default) ---------------------------------------------


def test_default_policy_rejects_missing_session_id():
    channel = ChatChannel("assistant", _Runner())  # default strict
    with pytest.raises(RequestError, match="session_id is required"):
        asyncio.run(channel.handle(ChatRequest(message="hi")))  # no session_id


def test_require_is_the_explicit_default():
    channel = ChatChannel("assistant", _Runner(), session_policy=require())
    with pytest.raises(RequestError):
        asyncio.run(channel.handle(ChatRequest(message="hi")))


def test_strict_keys_anonymous_by_session_id():
    runner = _Runner()
    channel = ChatChannel("assistant", runner)
    asyncio.run(channel.handle(ChatRequest(message="hi", session_id="s-1")))

    session = runner.calls[0]["session"]
    assert session.is_anonymous is True
    assert session.user_id == "s-1"


# --- mint_if_absent() -----------------------------------------------------------


def test_mint_generates_session_id_when_absent():
    runner = _Runner()
    channel = ChatChannel("assistant", runner, session_policy=mint_if_absent())
    reply = asyncio.run(channel.handle(ChatRequest(message="hi")))  # no ids at all

    session = runner.calls[0]["session"]
    assert session.session_id  # a uuid was minted
    assert reply.session_id == session.session_id  # returned to the caller
    assert session.is_anonymous is True


def test_mint_prefixes_anonymous_user_id():
    runner = _Runner()
    channel = ChatChannel("assistant", runner, session_policy=mint_if_absent(anon_prefix="anon_"))
    asyncio.run(channel.handle(ChatRequest(message="hi", session_id="s-1")))

    session = runner.calls[0]["session"]
    assert session.session_id == "s-1"  # supplied id is kept
    assert session.user_id.startswith("anon_")
    assert session.is_anonymous is True


def test_mint_keeps_identified_user():
    runner = _Runner()
    channel = ChatChannel("assistant", runner, session_policy=mint_if_absent(anon_prefix="anon_"))
    asyncio.run(channel.handle(ChatRequest(message="hi", session_id="s-1", user_id="u-1")))

    session = runner.calls[0]["session"]
    assert session.user_id == "u-1"
    assert session.is_anonymous is False


# --- HTTP status mapping --------------------------------------------------------


def test_chat_router_maps_request_error_to_400():
    channel = ChatChannel("assistant", _Runner())  # strict
    app = FastAPI()
    app.include_router(chat_router(channel))

    resp = TestClient(app).post("/chat", json={"message": "hi"})  # no session_id

    assert resp.status_code == 400
    assert "session_id is required" in resp.json()["detail"]


def test_chat_router_mint_policy_accepts_missing_session_id():
    channel = ChatChannel("assistant", _Runner(), session_policy=mint_if_absent())
    app = FastAPI()
    app.include_router(chat_router(channel))

    resp = TestClient(app).post("/chat", json={"message": "hi"})

    assert resp.status_code == 200
    assert resp.json()["session_id"]  # minted id echoed back


def test_stream_path_rejects_missing_session_id_preflight():
    # The session policy runs before the SSE stream opens, so a strict-policy miss is a
    # real 400 — not a 200 with an error frame (which is reserved for execution failures).
    async def _streamer(name, input, session, *, extras=None):  # pragma: no cover
        yield None

    channel = ChatChannel("assistant", _Runner(), streamer=_streamer)  # strict
    app = FastAPI()
    app.include_router(chat_router(channel))

    resp = TestClient(app).post("/chat/stream", json={"message": "hi"})  # no session_id

    assert resp.status_code == 400
    assert "session_id is required" in resp.json()["detail"]
