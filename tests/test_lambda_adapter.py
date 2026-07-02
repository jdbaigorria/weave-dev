"""Tests for the mountable AWS Lambda handler (weave.adapters.driving.lambda_handler)."""

from __future__ import annotations

import base64
import json
from typing import Any

from loom.errors import AgentNotFoundError

from weave.adapters.driving.chat import ChatChannel
from weave.adapters.driving.lambda_handler import lambda_handler
from weave.application.errors import HarnessError
from weave.application.reply import AgentReply, Usage


def _reply(text: str = "hi", session_id: str = "s-1") -> AgentReply:
    return AgentReply(text=text, usage=Usage(), session_id=session_id)


class _FakeRunner:
    """Records the dispatch call and returns a canned reply (or raises)."""

    def __init__(self, reply: AgentReply | None = None, *, fail: bool = False) -> None:
        self._reply = reply if reply is not None else _reply()
        self._fail = fail
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, name, input, session, *, kind=None, extras=None):
        self.calls.append({"name": name, "input": input, "extras": extras})
        if self._fail:
            raise HarnessError("agent 'x' failed during execution: boom")
        return self._reply


def _event(body: Any, *, base64_encode: bool = False) -> dict[str, Any]:
    raw = body if isinstance(body, str) else json.dumps(body)
    if base64_encode:
        return {"body": base64.b64encode(raw.encode()).decode(), "isBase64Encoded": True}
    return {"body": raw}


def test_handler_runs_turn_and_returns_200():
    runner = _FakeRunner(_reply(text="Paris", session_id="s-7"))
    handler = lambda_handler(ChatChannel("assistant", runner))

    resp = handler(_event({"message": "hi", "session_id": "s-7", "user_id": "u-1"}), None)

    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"] == "application/json"
    body = json.loads(resp["body"])
    assert body["text"] == "Paris"
    assert body["session_id"] == "s-7"
    assert "raw" not in body  # escape hatch excluded from the dump
    assert runner.calls[0]["input"] == "hi"


def test_handler_decodes_base64_body():
    runner = _FakeRunner()
    handler = lambda_handler(ChatChannel("assistant", runner))

    resp = handler(_event({"message": "hi", "session_id": "s-1"}, base64_encode=True), None)

    assert resp["statusCode"] == 200


def test_handler_maps_invalid_json_to_400():
    handler = lambda_handler(ChatChannel("assistant", _FakeRunner()))

    resp = handler({"body": "{not json"}, None)

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "invalid request"


def test_handler_maps_missing_required_field_to_400():
    handler = lambda_handler(ChatChannel("assistant", _FakeRunner()))

    resp = handler(_event({"message": "hi"}), None)  # no session_id

    assert resp["statusCode"] == 400


def test_handler_maps_harness_error_to_502():
    handler = lambda_handler(ChatChannel("assistant", _FakeRunner(fail=True)))

    resp = handler(_event({"message": "hi", "session_id": "s-1"}), None)

    assert resp["statusCode"] == 502
    assert "failed during execution" in json.loads(resp["body"])["error"]


def test_handler_maps_loom_error_to_502():
    # A build-time failure (invalid agent definition/config) propagates Loom's own
    # typed error unwrapped — the handler maps it to 502, distinct from a HarnessError.
    async def _runner(name, input, session, *, kind=None, extras=None):
        raise AgentNotFoundError("support_agent")

    handler = lambda_handler(ChatChannel("support_agent", _runner))

    resp = handler(_event({"message": "hi", "session_id": "s-1"}), None)

    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert body["error"] == "agent unavailable (invalid definition or configuration)"
    assert "not found" in body["detail"]
