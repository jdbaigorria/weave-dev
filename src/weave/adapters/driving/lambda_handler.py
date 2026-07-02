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
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from typing import Any

from loom.errors import LoomError
from pydantic import ValidationError

from weave.adapters.driving.chat import ChatChannel, ChatRequest
from weave.application.errors import HarnessError, RequestError, RoutingError

logger = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/json"}


def _response(status: int, body: str) -> dict[str, Any]:
    """Shape an API Gateway proxy response (``body`` is already a JSON string)."""
    return {"statusCode": status, "headers": _HEADERS, "body": body}


def _parse_body(event: dict[str, Any]) -> Any:
    """Decode the request body of an API Gateway event (REST v1 / HTTP API v2).

    Both put the payload in ``event["body"]`` as a string (or ``None``), optionally
    base64-encoded. Returns the parsed JSON (expected to be an object; a non-object
    makes ``ChatRequest(**...)`` raise ``TypeError`` → mapped to 400 by the caller).
    """
    raw = event.get("body")
    if raw is None:
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


def lambda_handler(channel: ChatChannel) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
    """Return an AWS Lambda handler exposing ``channel`` over API Gateway.

    The returned ``handler(event, context)`` parses the API Gateway event into a
    :class:`~weave.adapters.driving.chat.ChatRequest`, runs one turn synchronously,
    and returns an API Gateway proxy response whose ``body`` is the serialized
    :class:`~weave.application.reply.AgentReply` / ``WorkflowReply`` (the ``raw``
    escape hatch is excluded by the model). Wire the channel with
    :func:`weave.composition.chat_channel`, then::

        handler = lambda_handler(chat_channel("assistant"))

    Status mapping: a malformed/invalid body, or one missing what the session policy
    requires (:class:`~weave.application.errors.RequestError`, e.g. no ``session_id``
    under the strict default) → **400**, a request that maps to no target
    (:class:`~weave.application.errors.RoutingError`, from a routed channel) →
    **404**, a :class:`~weave.application.errors.HarnessError` (execution failure) or a
    :class:`loom.errors.LoomError` (the agent's *definition/config* is invalid, so the
    build failed before it ran) → **502**, any other unexpected error → **500**. A
    successful turn → **200**.
    """
    # Imported here (not at module top) so this adapter does not force composition
    # to import at module load; ``_block_on`` is the harness's canonical sync bridge.
    from weave.composition import _block_on

    def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
        try:
            request = ChatRequest(**_parse_body(event))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.info("rejected malformed request: %s", exc)
            return _response(400, json.dumps({"error": "invalid request", "detail": str(exc)}))

        try:
            reply = _block_on(channel.handle(request))
        except RequestError as exc:
            logger.info("rejected request (session=%s): %s", request.session_id, exc)
            return _response(400, json.dumps({"error": "invalid request", "detail": str(exc)}))
        except RoutingError as exc:
            logger.info("request mapped to no target (session=%s): %s", request.session_id, exc)
            return _response(404, json.dumps({"error": str(exc)}))
        except HarnessError as exc:
            logger.warning("execution failed (session=%s): %s", request.session_id, exc)
            return _response(502, json.dumps({"error": str(exc)}))
        except LoomError as exc:
            # Build-time failure: the agent's definition/config is invalid (unknown
            # agent, bad YAML, a tool/model that won't resolve), so it never ran. A
            # distinct 502 from HarnessError (which is an *execution* failure).
            logger.warning("agent unavailable (session=%s): %s", request.session_id, exc)
            return _response(
                502,
                json.dumps(
                    {
                        "error": "agent unavailable (invalid definition or configuration)",
                        "detail": str(exc),
                    }
                ),
            )
        except Exception as exc:  # noqa: BLE001 — last-resort guard; never leak a 500 traceback
            # The 500 body hides the cause, so this is the only place the real traceback
            # survives — log it with exc_info before mapping to an opaque response.
            logger.exception("unhandled error in lambda handler (session=%s)", request.session_id)
            return _response(500, json.dumps({"error": "internal error", "detail": str(exc)}))

        return _response(200, reply.model_dump_json())

    return handler
