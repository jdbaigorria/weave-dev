"""weave.adapters.driving.chat — Mountable chat channel (request/response).

A *driving* adapter: it translates an inbound chat request into a harness ``run``
call and returns the harness reply. Two layers, like bidi:

- :class:`ChatChannel` is the transport-agnostic core — parse request, build the
  :class:`~loom.LoomSession`, dispatch by kind, map failures to typed errors. It
  knows nothing about HTTP.
- :func:`chat_router` is a thin **mountable** FastAPI ``APIRouter`` over it (FastAPI
  imported lazily, gated behind the ``fastapi`` extra). A backend *mounts* this on
  its own route rather than running a sealed Weave server — for the
  conversation-without-domain quadrant the mounted channel *is* the backend.

One channel serves one target ``name`` (the mount point is the authorization
boundary). Mount several channels for several agents.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from loom import LoomSession
from loom.errors import LoomError
from pydantic import BaseModel, Field

from weave.application.errors import HarnessError, RequestError, RoutingError
from weave.application.reply import AgentReply, ChatStreamEvent, WorkflowReply

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastapi import APIRouter

    from weave.ports.registry import ConversationRegistry

# A use case already bound to an engine: ``run(name, input, session, *, kind, extras)``.
Runner = Callable[..., Awaitable[AgentReply | WorkflowReply]]
# Its token-streaming sibling: ``stream_agent(name, input, session, *, extras)``.
Streamer = Callable[..., AsyncIterator[ChatStreamEvent]]


class ChatRequest(BaseModel):
    """One inbound chat turn.

    ``message`` is what the agent expects — a plain string or a list of Strands
    content blocks (multimodal). ``user_id`` is optional: omit it for an anonymous
    visitor. ``session_id`` is optional *at the wire level* — what happens when it is
    absent is the channel's :data:`SessionPolicy`: the default (:func:`require`) is
    strict and raises :class:`~weave.application.errors.RequestError` (→ 400), while
    :func:`~weave.adapters.driving.identity.mint_if_absent` generates one.
    """

    message: str | list[Any]
    session_id: str | None = None
    user_id: str | None = None
    extras: dict[str, Any] | None = Field(default=None)


# A pre-agent entrypoint: pick which target a request runs against. The product
# injects the policy (where the key comes from, the table); see ``routing.py`` for
# the ``by_key`` / ``by_rules`` helpers. A plain ``str`` target is the degenerate
# case — a constant resolver — so single- and multi-agent share one channel.
TargetResolver = Callable[[ChatRequest], str]

# How a request's identity fields become the run's ``LoomSession``. Session lifetime
# is the harness's domain (the Weave/Loom line), so Weave owns the seam; the product
# injects the policy — see ``identity.py`` for ``require`` / ``mint_if_absent``. The
# default is strict (below): a request must carry its own ``session_id``.
SessionPolicy = Callable[[ChatRequest], LoomSession]


def _strict_session(request: ChatRequest) -> LoomSession:
    """Default :data:`SessionPolicy`: require a caller-supplied ``session_id``.

    Raises :class:`~weave.application.errors.RequestError` (→ 400) when it is absent —
    a runtime library should not silently mint a session and turn a client's "I forgot
    to persist my session_id" bug into a fresh conversation every turn. Opt into
    minting with :func:`~weave.adapters.driving.identity.mint_if_absent`. An anonymous
    visitor (no ``user_id``) is keyed by ``session_id``.
    """
    if request.session_id is None:
        raise RequestError(
            "session_id is required; pass one, or build the channel with a minting "
            "policy (mint_if_absent) to generate it."
        )
    return LoomSession(
        user_id=request.user_id or request.session_id,
        session_id=request.session_id,
        is_anonymous=request.user_id is None,
    )


class ChatChannel:
    """Transport-agnostic chat handler over one resolved target.

    ``target`` is either a fixed name (single agent) or a
    :data:`TargetResolver` that picks the target per request (a router) — the
    backend injects the routing policy. ``str`` is treated as a constant resolver,
    so single- and multi-agent are the same channel.

    The ``runner`` is the engine-bound use case (``weave.composition.run`` by
    default — see :func:`weave.composition.chat_channel`). Injecting it keeps this
    adapter independent of any concrete engine and trivially testable.

    If a ``registry`` is provided, each turn upserts the conversation into it
    (skipping anonymous requests), so a backend can later list a user's chats via
    :func:`weave.application.conversations.list_conversations`. Without one it is a
    no-op — the conversation index is opt-in.

    ``session_policy`` decides how a request's identity becomes the run's
    ``LoomSession``; it defaults to the strict :func:`require` (a request must carry
    its own ``session_id``). Pass
    :func:`~weave.adapters.driving.identity.mint_if_absent` to generate one instead.
    """

    def __init__(
        self,
        target: str | TargetResolver,
        runner: Runner,
        *,
        streamer: Streamer | None = None,
        registry: ConversationRegistry | None = None,
        kind: str | None = None,
        session_policy: SessionPolicy | None = None,
    ) -> None:
        # A bare name becomes a constant resolver; a callable routes per request.
        self._resolve: TargetResolver = target if callable(target) else (lambda _req: target)
        self._runner = runner
        self._streamer = streamer
        self._registry = registry
        self._kind = kind
        # Strict by default: a missing session_id is a 400, not a silent new session.
        self._session_policy: SessionPolicy = session_policy or _strict_session

    async def handle(self, request: ChatRequest) -> AgentReply | WorkflowReply:
        """Run one turn for ``request`` and return the harness reply.

        The target is resolved from the request first (raising
        :class:`~weave.application.errors.RoutingError` if it maps to none), then the
        session policy builds the identity (raising
        :class:`~weave.application.errors.RequestError` under the strict default if no
        ``session_id`` was supplied). Execution failures surface as the harness's own
        typed :class:`~weave.application.errors.HarnessError` (raised by the runner);
        this adapter adds no error field of its own.
        """
        name = self._resolve(request)
        session = self._session(request)
        logger.debug("chat turn → target %r (session=%s)", name, session.session_id)
        self._record(session)
        return await self._runner(
            name,
            request.message,
            session,
            kind=self._kind,
            extras=request.extras,
        )

    def handle_sync(self, request: ChatRequest) -> AgentReply | WorkflowReply:
        """Synchronous :meth:`handle` — for a caller without an event loop (Lambda).

        A backend with its own response contract (it maps the reply to its own DTO and
        runs in a sync handler) can route + dispatch in one blocking call, instead of
        ``asyncio.run(channel.handle(...))`` by hand. Routing and execution failures
        surface the same as :meth:`handle` (``RoutingError`` / ``HarnessError``).
        """
        from weave.composition import _block_on  # lazy: composition imports this module

        return _block_on(self.handle(request))

    def stream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        """Stream one turn for ``request`` as :class:`ChatStreamEvent`s (token streaming).

        Returns the event async-iterator (agent-only; built via
        :func:`weave.composition.chat_channel`). Raises
        :class:`~weave.application.errors.RoutingError` if the request maps to no
        target, or :class:`~weave.application.errors.HarnessError` if this channel
        was built without a streamer, or, during iteration, if the turn fails.
        """
        if self._streamer is None:
            raise HarnessError(
                "this channel has no streamer; build it via chat_channel() to stream, "
                "or use handle() for a one-shot reply."
            )
        name = self._resolve(request)
        session = self._session(request)
        logger.debug("chat stream → target %r (session=%s)", name, session.session_id)
        self._record(session)
        return self._streamer(name, request.message, session, extras=request.extras)

    def _session(self, request: ChatRequest) -> LoomSession:
        """Build the run's identity by applying the channel's :data:`SessionPolicy`."""
        return self._session_policy(request)

    def _record(self, session: LoomSession) -> None:
        """Upsert the (effective) conversation into the registry (skip anonymous)."""
        if self._registry is not None and not session.is_anonymous:
            self._registry.record(session.user_id, session.session_id)


def chat_router(
    channel: ChatChannel, *, path: str = "/chat", stream_path: str = "/chat/stream"
) -> APIRouter:
    """Return a FastAPI ``APIRouter`` exposing ``channel`` over HTTP.

    Two routes, both POST: ``path`` returns the one-shot harness reply
    (:class:`~weave.application.reply.AgentReply` / ``WorkflowReply``, serialized by
    FastAPI — the ``raw`` escape hatch is excluded by the model); ``stream_path``
    token-streams the turn as Server-Sent Events, one ``ChatStreamEvent`` per
    ``data:`` frame. Mount it: ``app.include_router(chat_router(channel))``.

    A request missing what the session policy requires → HTTP 400 (``RequestError``,
    e.g. no ``session_id`` under the strict default); a request that maps to no target
    → HTTP 404 (``RoutingError``); an execution failure → HTTP 502 (``HarnessError``),
    as does an invalid agent definition/config → HTTP 502 (:class:`loom.errors.LoomError`,
    the build failed before it ran). Streaming can't change the status once the response
    has started, so a failure mid-stream is delivered as a final
    ``data: {"type": "error", "detail": ...}`` frame instead.
    """
    try:
        from fastapi import APIRouter, HTTPException
        from fastapi.responses import StreamingResponse
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise HarnessError("chat_router needs FastAPI; install the extra: weave[fastapi].") from exc

    router = APIRouter()

    @router.post(path)
    async def chat(request: ChatRequest) -> AgentReply | WorkflowReply:
        try:
            return await channel.handle(request)
        except RequestError as exc:
            logger.info("rejected request (session=%s): %s", request.session_id, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RoutingError as exc:
            logger.info("request mapped to no target (session=%s): %s", request.session_id, exc)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except HarnessError as exc:
            logger.warning("execution failed (session=%s): %s", request.session_id, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except LoomError as exc:
            # Build-time failure (invalid agent definition/config): it never ran.
            # A distinct 502 from HarnessError's execution failure.
            logger.warning("agent unavailable (session=%s): %s", request.session_id, exc)
            raise HTTPException(
                status_code=502,
                detail=f"agent unavailable (invalid definition or configuration): {exc}",
            ) from exc

    @router.post(stream_path)
    async def chat_stream(request: ChatRequest) -> StreamingResponse:
        # Pre-flight: target resolution + session policy run synchronously here, so a
        # bad request still gets a real status (400/404) before the 200 stream opens.
        # Only the agent build/execution is lazy — its failures ride as an error frame.
        try:
            events = channel.stream(request)
        except RequestError as exc:
            logger.info("rejected stream request (session=%s): %s", request.session_id, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RoutingError as exc:
            logger.info("stream mapped to no target (session=%s): %s", request.session_id, exc)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except HarnessError as exc:  # channel built without a streamer
            logger.warning("stream unavailable (session=%s): %s", request.session_id, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        async def frames() -> AsyncIterator[str]:
            try:
                async for event in events:
                    yield f"data: {event.model_dump_json()}\n\n"
            except (HarnessError, LoomError) as exc:
                yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    return router
