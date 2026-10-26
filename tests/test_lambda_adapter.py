"""Tests for the mountable AWS Lambda handler (weave.adapters.driving.lambda_handler)."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from loom.errors import AgentNotFoundError

from weave.adapters.driving.chat import ChatChannel, ChatRequest
from weave.adapters.driving.lambda_handler import decode_body, header, lambda_handler
from weave.application.errors import HarnessError, RequestError
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


# --- extensible handler: parse / render / errors / headers ----------------------


class _OverflowError(Exception):
    """Stands in for Strands' ContextWindowOverflowException."""


def _wrapped_overflow_runner():
    async def _runner(name, input, session, *, kind=None, extras=None):
        try:
            raise _OverflowError("too long")
        except _OverflowError as exc:
            raise HarnessError("agent 'x' failed during execution: too long") from exc

    return _runner


def _ok_event() -> dict[str, Any]:
    return _event({"message": "hi", "session_id": "s-1"})


def test_handler_maps_invalid_base64_to_400():
    handler = lambda_handler(ChatChannel("assistant", _FakeRunner()))

    resp = handler({"body": "%%%not-base64%%%", "isBase64Encoded": True}, None)

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "invalid request"


def test_custom_parse_reads_other_field_names_and_headers():
    runner = _FakeRunner()

    def parse(event):
        body = decode_body(event)
        return ChatRequest(
            message=body["input"],
            session_id=body["sid"],
            extras={"tenant": header(event, "X-Tenant")},
        )

    handler = lambda_handler(ChatChannel("assistant", runner), parse=parse)

    for headers in ({"X-Tenant": "acme"}, {"x-tenant": "acme"}):  # REST v1 / HTTP API v2
        event = {"body": json.dumps({"input": "hola", "sid": "s-1"}), "headers": headers}
        assert handler(event, None)["statusCode"] == 200
        assert runner.calls[-1]["input"] == "hola"
        assert runner.calls[-1]["extras"] == {"tenant": "acme"}


def test_header_is_none_when_absent_or_headers_missing():
    assert header({"headers": {"A": "1"}}, "b") is None
    assert header({"headers": None}, "a") is None
    assert header({}, "a") is None


def test_decode_body_defaults_to_empty_object():
    assert decode_body({}) == {}
    assert decode_body({"body": None}) == {}


@pytest.mark.parametrize("exc", [ValueError("bad"), RequestError("bad")])
def test_parse_that_rejects_gives_400_and_mapper_sees_no_request(exc):
    seen = []

    def parse(event):
        raise exc

    def mapper(error, request):
        seen.append(request)
        return 422, {"custom": True}

    # default mapping first: both rejections are 400
    assert (
        lambda_handler(ChatChannel("a", _FakeRunner()), parse=parse)({}, None)["statusCode"] == 400
    )
    # a custom mapper for the same error receives request=None
    handler = lambda_handler(
        ChatChannel("a", _FakeRunner()), parse=parse, errors={type(exc): mapper}
    )
    assert handler({}, None)["statusCode"] == 422
    assert seen == [None]


def test_render_dict_becomes_json_and_str_is_used_as_is():
    as_dict = lambda_handler(
        ChatChannel("a", _FakeRunner()), render=lambda reply, req: {"out": reply.text}
    )
    as_str = lambda_handler(ChatChannel("a", _FakeRunner()), render=lambda reply, req: "plain")

    assert json.loads(as_dict(_ok_event(), None)["body"]) == {"out": "hi"}
    assert as_str(_ok_event(), None)["body"] == "plain"


def test_render_receives_the_parsed_request():
    seen = []
    handler = lambda_handler(
        ChatChannel("a", _FakeRunner()), render=lambda reply, req: seen.append(req) or "ok"
    )
    handler(_ok_event(), None)
    assert seen[0].session_id == "s-1"


def test_render_that_raises_gives_500():
    def render(reply, request):
        raise RuntimeError("nope")

    resp = lambda_handler(ChatChannel("a", _FakeRunner()), render=render)(_ok_event(), None)

    assert resp["statusCode"] == 500


def test_errors_match_a_wrapped_cause():
    handler = lambda_handler(
        ChatChannel("a", _wrapped_overflow_runner()),
        errors={
            _OverflowError: lambda exc, req: (413, {"error": "too_long", "session": req.session_id})
        },
    )

    resp = handler(_ok_event(), None)

    assert resp["statusCode"] == 413
    assert json.loads(resp["body"]) == {"error": "too_long", "session": "s-1"}


def test_errors_are_tried_in_insertion_order():
    runner = _wrapped_overflow_runner()
    specific_first = lambda_handler(
        ChatChannel("a", runner),
        errors={_OverflowError: lambda e, r: (413, "a"), Exception: lambda e, r: (418, "b")},
    )
    catch_all_first = lambda_handler(
        ChatChannel("a", runner),
        errors={Exception: lambda e, r: (418, "b"), _OverflowError: lambda e, r: (413, "a")},
    )

    assert specific_first(_ok_event(), None)["statusCode"] == 413
    assert catch_all_first(_ok_event(), None)["statusCode"] == 418


def test_errors_can_override_the_default_502():
    handler = lambda_handler(
        ChatChannel("a", _FakeRunner(fail=True)),
        errors={HarnessError: lambda e, r: (503, {"retry": True})},
    )

    resp = handler(_ok_event(), None)

    assert resp["statusCode"] == 503
    assert json.loads(resp["body"]) == {"retry": True}


def test_errors_match_context_chain_without_explicit_cause():
    async def _runner(name, input, session, *, kind=None, extras=None):
        try:
            raise _OverflowError("ctx")
        except _OverflowError:
            raise HarnessError("wrapped")  # noqa: B904 — implicit __context__ on purpose

    handler = lambda_handler(
        ChatChannel("a", _runner), errors={_OverflowError: lambda e, r: (413, "x")}
    )

    assert handler(_ok_event(), None)["statusCode"] == 413


def test_errors_ignore_a_suppressed_context():
    async def _runner(name, input, session, *, kind=None, extras=None):
        try:
            raise _OverflowError("ctx")
        except _OverflowError:
            raise HarnessError("wrapped") from None

    handler = lambda_handler(
        ChatChannel("a", _runner), errors={_OverflowError: lambda e, r: (413, "x")}
    )

    assert handler(_ok_event(), None)["statusCode"] == 502


def test_a_cyclic_cause_chain_does_not_hang():
    a, b = ValueError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a

    async def _runner(name, input, session, *, kind=None, extras=None):
        raise a

    handler = lambda_handler(
        ChatChannel("a", _runner), errors={_OverflowError: lambda e, r: (413, "x")}
    )

    assert handler(_ok_event(), None)["statusCode"] == 500


def test_errors_mapper_that_raises_gives_500():
    def mapper(exc, request):
        raise RuntimeError("mapper bug")

    handler = lambda_handler(
        ChatChannel("a", _FakeRunner(fail=True)), errors={HarnessError: mapper}
    )

    assert handler(_ok_event(), None)["statusCode"] == 500


def test_headers_ride_on_200_400_and_500():
    cors = {"Access-Control-Allow-Origin": "*"}

    def parse(event):
        if event.get("bad"):
            raise ValueError("bad")
        return ChatRequest(message="hi", session_id="s-1")

    ok = lambda_handler(ChatChannel("a", _FakeRunner()), parse=parse, headers=cors)
    boom = lambda_handler(ChatChannel("a", _FakeRunner(fail=True)), parse=parse, headers=cors)

    for resp, status in (
        (ok({}, None), 200),
        (ok({"bad": True}, None), 400),
        (boom({}, None), 502),
    ):
        assert resp["statusCode"] == status
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
        assert resp["headers"]["Content-Type"] == "application/json"

    async def _crash(name, input, session, *, kind=None, extras=None):
        raise RuntimeError("x")

    resp = lambda_handler(ChatChannel("a", _crash), headers=cors)(_ok_event(), None)
    assert resp["statusCode"] == 500
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


def test_own_header_overrides_the_default_content_type_case_insensitively():
    handler = lambda_handler(
        ChatChannel("a", _FakeRunner()), headers={"content-type": "text/plain"}
    )

    headers = handler(_ok_event(), None)["headers"]

    assert headers == {"content-type": "text/plain"}
