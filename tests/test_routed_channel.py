"""Tests for the pre-agent entrypoint: routed channels and target resolvers."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from weave.adapters.driving.chat import ChatChannel, ChatRequest
from weave.adapters.driving.lambda_handler import lambda_handler
from weave.adapters.driving.routing import by_key, by_rules
from weave.application.errors import HarnessError, RoutingError
from weave.application.reply import AgentReply, Usage


def _reply(text: str = "hi", session_id: str = "s-1") -> AgentReply:
    return AgentReply(text=text, usage=Usage(), session_id=session_id)


class _FakeRunner:
    """Records which target name it was dispatched with."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, name, input, session, *, kind=None, extras=None):
        self.calls.append({"name": name, "input": input, "extras": extras})
        if self._fail:
            raise HarnessError("agent failed during execution: boom")
        return _reply(text=f"from {name}")


def _req(area: str) -> ChatRequest:
    return ChatRequest(message="hi", session_id="s-1", extras={"area": area})


# --- by_key ----------------------------------------------------------------


def test_by_key_routes_to_the_mapped_target():
    runner = _FakeRunner()
    channel = ChatChannel(by_key(lambda r: r.extras["area"], {"X": "support_agent"}), runner)

    reply = asyncio.run(channel.handle(_req("X")))

    assert reply.text == "from support_agent"
    assert runner.calls[0]["name"] == "support_agent"


def test_by_key_unknown_key_without_default_raises_routing_error():
    channel = ChatChannel(by_key(lambda r: r.extras["area"], {"X": "support_agent"}), _FakeRunner())
    with pytest.raises(RoutingError, match="no route for key"):
        asyncio.run(channel.handle(_req("ZZZ")))


def test_by_key_falls_back_to_default():
    runner = _FakeRunner()
    channel = ChatChannel(
        by_key(lambda r: r.extras["area"], {"X": "support_agent"}, default="fallback"), runner
    )
    asyncio.run(channel.handle(_req("ZZZ")))
    assert runner.calls[0]["name"] == "fallback"


# --- by_rules --------------------------------------------------------------


def test_by_rules_first_matching_predicate_wins():
    runner = _FakeRunner()
    resolve = by_rules(
        [
            (lambda r: r.extras["area"] == "X" and r.extras.get("vip"), "vip_agent"),
            (lambda r: r.extras["area"] == "X", "standard_agent"),
        ]
    )
    channel = ChatChannel(resolve, runner)

    asyncio.run(channel.handle(ChatRequest(message="hi", session_id="s", extras={"area": "X"})))
    assert runner.calls[0]["name"] == "standard_agent"  # vip rule didn't match


def test_by_rules_no_match_without_default_raises():
    resolve = by_rules([(lambda r: False, "never")])
    channel = ChatChannel(resolve, _FakeRunner())
    with pytest.raises(RoutingError, match="no rule matched"):
        asyncio.run(channel.handle(_req("X")))


# --- lambda_handler integration -------------------------------------------


def _event(area: str) -> dict[str, Any]:
    return {"body": json.dumps({"message": "hi", "session_id": "s-1", "extras": {"area": area}})}


def test_lambda_handler_routes_and_returns_200():
    channel = ChatChannel(by_key(lambda r: r.extras["area"], {"X": "support_agent"}), _FakeRunner())
    handler = lambda_handler(channel)

    resp = handler(_event("X"), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["text"] == "from support_agent"


def test_lambda_handler_maps_routing_error_to_404():
    channel = ChatChannel(by_key(lambda r: r.extras["area"], {"X": "support_agent"}), _FakeRunner())
    handler = lambda_handler(channel)

    resp = handler(_event("ZZZ"), None)

    assert resp["statusCode"] == 404
    assert "no route for key" in json.loads(resp["body"])["error"]


def test_lambda_handler_still_maps_harness_error_to_502():
    channel = ChatChannel(
        by_key(lambda r: r.extras["area"], {"X": "support_agent"}), _FakeRunner(fail=True)
    )
    handler = lambda_handler(channel)

    resp = handler(_event("X"), None)

    assert resp["statusCode"] == 502


def test_composition_routed_channel_wires_default_engine():
    from weave.composition import routed_channel

    channel = routed_channel(by_key(lambda r: r.extras["area"], {"X": "support_agent"}))
    assert isinstance(channel, ChatChannel)
    assert channel._resolve(_req("X")) == "support_agent"


def test_chat_router_maps_routing_error_to_404():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    channel = ChatChannel(by_key(lambda r: r.extras["area"], {"X": "support_agent"}), _FakeRunner())
    from weave.adapters.driving.chat import chat_router

    app = FastAPI()
    app.include_router(chat_router(channel))

    resp = TestClient(app).post(
        "/chat", json={"message": "hi", "session_id": "s-1", "extras": {"area": "ZZZ"}}
    )

    assert resp.status_code == 404
