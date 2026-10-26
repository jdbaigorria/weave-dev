"""weave.adapters.driving.lambda_handler — Mountable AWS Lambda handler.

A *driving* adapter, the synchronous mirror of
:func:`weave.adapters.driving.chat.chat_router`: it translates an inbound API
Gateway event into a :class:`~weave.adapters.driving.chat.ChatChannel` call and
returns an API Gateway response. Where ``chat_router`` is an async FastAPI router,
this is a plain ``def handler(event, context)`` — the shape AWS Lambda invokes,
with no event loop of its own (it blocks on the turn via ``composition._block_on``).

It is **0-dependency**: API Gateway events are parsed with the standard library, so
no extra is needed. A product that wants structured logging (`aws-lambda-powertools`)
wraps this handler in its own — Weave stays agnostic to that choice.

One handler serves one target ``name`` (the channel is the authorization boundary),
exactly like ``chat_router``. The handler is transport-only: any product-specific
routing (e.g. choosing an agent by request attributes) belongs in the backend, not here.

A product with its own HTTP contract plugs into the three seams instead of rewriting the
handler: ``parse`` (event → :class:`~weave.adapters.driving.chat.ChatRequest`), ``render``
(reply → body) and ``errors`` (exception type → ``(status, body)``), plus ``headers``
(e.g. CORS) on every response. :func:`decode_body` and :func:`header` are the two
helpers a custom ``parse`` usually needs.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from loom.errors import LoomError
from pydantic import BaseModel

from weave.adapters.driving.chat import ChatChannel, ChatRequest
from weave.application.errors import HarnessError, RequestError, RoutingError
from weave.application.reply import AgentReply, WorkflowReply

logger = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/json"}

Parser = Callable[[dict[str, Any]], ChatRequest]
Renderer = Callable[[AgentReply | WorkflowReply, ChatRequest], Any]
ErrorMapper = Callable[[BaseException, ChatRequest | None], tuple[int, Any]]


def decode_body(event: dict[str, Any]) -> Any:
    """Decode the request body of an API Gateway event (REST v1 / HTTP API v2).

    Both put the payload in ``event["body"]`` as a string (or ``None``), optionally
    base64-encoded. Returns the parsed JSON — an absent body gives ``{}``. Malformed
    input raises a :class:`ValueError` subclass (``json.JSONDecodeError``,
    ``binascii.Error``, ``UnicodeDecodeError``), which the handler maps to 400.
    """
    raw = event.get("body")
    if raw is None:
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


def header(event: dict[str, Any], name: str) -> str | None:
    """Read a request header from an API Gateway event, ignoring case.

    REST (v1) events keep the header's original case and HTTP API (v2) lowercases it,
    so a plain ``event["headers"][name]`` works on one and misses on the other.
    """
    wanted = name.lower()
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == wanted:
            return str(value)
    return None


def _default_parse(event: dict[str, Any]) -> ChatRequest:
    # A non-object body makes ``**`` raise TypeError → 400, like a missing field does.
    return ChatRequest(**decode_body(event))


def _serialize(value: Any) -> str:
    """Body text of a ``render`` / ``errors`` result: ``str`` as-is, model or JSON-able."""
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value)


def _causes(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` then its causes, the way :mod:`traceback` walks them (cycle-safe)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None


def _custom_mapper(
    errors: Mapping[type[BaseException], ErrorMapper], exc: BaseException
) -> ErrorMapper | None:
    """First registered type (insertion order, like ``except`` clauses) matching ``exc``.

    A type matches when ``exc`` *or any of its causes* is an instance of it — a context
    overflow reaches the handler wrapped in :class:`~weave.application.errors.HarnessError`.
    """
    for exc_type, mapper in errors.items():
        if any(isinstance(cause, exc_type) for cause in _causes(exc)):
            return mapper
    return None


def _default_error(exc: Exception, *, parsing: bool, session_id: str | None) -> tuple[int, Any]:
    """Weave's own status mapping, applied to the raised exception only."""
    if parsing:
        if isinstance(exc, (ValueError, TypeError, RequestError)):
            logger.info("rejected malformed request: %s", exc)
            return 400, {"error": "invalid request", "detail": str(exc)}
    elif isinstance(exc, RequestError):
        logger.info("rejected request (session=%s): %s", session_id, exc)
        return 400, {"error": "invalid request", "detail": str(exc)}
    elif isinstance(exc, RoutingError):
        logger.info("request mapped to no target (session=%s): %s", session_id, exc)
        return 404, {"error": str(exc)}
    elif isinstance(exc, HarnessError):
        logger.warning("execution failed (session=%s): %s", session_id, exc)
        return 502, {"error": str(exc)}
    elif isinstance(exc, LoomError):
        # Build-time failure: the agent's definition/config is invalid (unknown
        # agent, bad YAML, a tool/model that won't resolve), so it never ran. A
        # distinct 502 from HarnessError (which is an *execution* failure).
        logger.warning("agent unavailable (session=%s): %s", session_id, exc)
        return 502, {
            "error": "agent unavailable (invalid definition or configuration)",
            "detail": str(exc),
        }
    # The 500 body hides the cause, so this is the only place the real traceback
    # survives — log it with exc_info before mapping to an opaque response.
    logger.error("unhandled error in lambda handler (session=%s)", session_id, exc_info=exc)
    return 500, {"error": "internal error", "detail": str(exc)}


def lambda_handler(
    channel: ChatChannel,
    *,
    parse: Parser | None = None,
    render: Renderer | None = None,
    errors: Mapping[type[BaseException], ErrorMapper] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
    """Return an AWS Lambda handler exposing ``channel`` over API Gateway.

    The returned ``handler(event, context)`` parses the API Gateway event into a
    :class:`~weave.adapters.driving.chat.ChatRequest`, runs one turn synchronously,
    and returns an API Gateway proxy response whose ``body`` is the serialized
    :class:`~weave.application.reply.AgentReply` / ``WorkflowReply`` (the ``raw``
    escape hatch is excluded by the model). Wire the channel with
    :func:`weave.composition.chat_channel`, then::

        handler = lambda_handler(chat_channel("assistant"))

    A product with its own contract customizes the edges (all keyword-only, optional):

    - ``parse(event) -> ChatRequest`` — read the body with your own field names, put
      what you take from headers into ``extras`` (see :func:`decode_body` /
      :func:`header`). Raise :class:`ValueError` or
      :class:`~weave.application.errors.RequestError` to reject the request (400).
    - ``render(reply, request) -> body`` — shape the 200 body. A ``str`` is used as-is,
      a pydantic model via ``model_dump_json()``, anything else via ``json.dumps``.
      The reply carries ``target`` / ``user_id`` / ``is_anonymous`` for per-agent policy.
    - ``errors={ExcType: mapper}`` — ``mapper(exc, request) -> (status, body)``
      (``request`` is ``None`` if the error came while parsing). Evaluated **before**
      Weave's own mapping, in insertion order like ``except`` clauses, and an
      entry matches when the raised exception *or any of its causes* is an instance
      — so ``{ContextWindowOverflowException: ...}`` works although the turn raises
      :class:`~weave.application.errors.HarnessError` ``from`` it. A mapper that itself
      raises answers 500.
    - ``headers`` — added to ``Content-Type: application/json`` on **every** response
      (errors included): where CORS headers go. A same-named entry replaces the default.

    Default status mapping (after your ``errors``): a malformed/invalid body, or one
    missing what the session policy requires
    (:class:`~weave.application.errors.RequestError`, e.g. no ``session_id`` under the
    strict default) → **400**, a request that maps to no target
    (:class:`~weave.application.errors.RoutingError`, from a routed channel) →
    **404**, a :class:`~weave.application.errors.HarnessError` (execution failure) or a
    :class:`loom.errors.LoomError` (the agent's *definition/config* is invalid, so the
    build failed before it ran) → **502**, any other unexpected error → **500**. A
    successful turn → **200**. ``render`` runs inside the same guard as the turn.
    """
    # Imported here (not at module top) so this adapter does not force composition
    # to import at module load; ``_block_on`` is the harness's canonical sync bridge.
    from weave.composition import _block_on

    parse_event: Parser = parse or _default_parse
    custom = dict(errors or {})
    extra_headers = dict(headers or {})
    overridden = {name.lower() for name in extra_headers}
    response_headers = {
        **{k: v for k, v in _HEADERS.items() if k.lower() not in overridden},
        **extra_headers,
    }

    def respond(status: int, body: str) -> dict[str, Any]:
        return {"statusCode": status, "headers": dict(response_headers), "body": body}

    def fail(exc: Exception, request: ChatRequest | None, *, parsing: bool) -> dict[str, Any]:
        session_id = request.session_id if request is not None else None
        try:
            mapper = _custom_mapper(custom, exc)
            if mapper is not None:
                status, body = mapper(exc, request)
            else:
                status, body = _default_error(exc, parsing=parsing, session_id=session_id)
            return respond(status, _serialize(body))
        except Exception:  # noqa: BLE001 — a broken error mapper must not crash the Lambda
            logger.exception("error mapper failed (session=%s)", session_id)
            return respond(500, json.dumps({"error": "internal error"}))

    def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
        try:
            request = parse_event(event)
        except Exception as exc:  # noqa: BLE001 — mapped by ``fail``
            return fail(exc, None, parsing=True)

        try:
            reply = _block_on(channel.handle(request))
            body = reply.model_dump_json() if render is None else _serialize(render(reply, request))
        except Exception as exc:  # noqa: BLE001 — mapped by ``fail``; never leak a traceback
            return fail(exc, request, parsing=False)

        return respond(200, body)

    return handler
