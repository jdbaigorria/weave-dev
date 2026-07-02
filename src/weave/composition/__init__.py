"""weave.composition — Composition root: wire concrete adapters to ports.

The one place that knows both ports and adapters. It assembles a runnable Weave
(use cases + chosen adapters) for a given target/deployment. This is also what a
bespoke backend imports when it wants to *mount* Weave rather than run it
standalone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from typing import TYPE_CHECKING, Any, TypeVar

from loom import LoomSession, TargetInfo

from weave.adapters.driven.loom_engine import LoomAgentEngine
from weave.adapters.driving.chat import ChatChannel, SessionPolicy, TargetResolver
from weave.application import run as _run
from weave.application import stream as _stream
from weave.application.errors import HarnessError
from weave.application.reply import AgentReply, ChatStreamEvent, WorkflowReply
from weave.application.stream import BidiStream
from weave.ports.registry import ConversationRegistry
from weave.ports.session import MutableSession

if TYPE_CHECKING:
    from loom.ports import ConfigSource

    from weave.ports.agent_engine import AgentEngine

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

# Default engine: Loom over the filesystem workspace. The wired top-level functions
# read this at call time, so configure() (below) can swap it once at startup.
_engine: AgentEngine = LoomAgentEngine()


def configure(*, source: ConfigSource | None = None, engine: AgentEngine | None = None) -> None:
    """Set the engine the wired top-level functions use — the composition root.

    Call once at startup, before serving traffic. The common case is choosing where
    declarative config comes from::

        import weave
        from weave.adapters.driven.dynamo_config_source import DynamoConfigSource

        weave.configure(source=DynamoConfigSource("my-agents-table"))

    After this, :func:`run_agent`, :func:`run`, :func:`catalog`, :func:`chat_channel`
    and every other wired entry point build from that source (DynamoDB here). Pass
    ``engine`` instead to install a fully custom :class:`~weave.ports.agent_engine.AgentEngine`
    (e.g. a test double). ``source`` and ``engine`` are mutually exclusive; with
    neither, the engine resets to the Loom filesystem default.
    """
    global _engine
    if source is not None and engine is not None:
        raise HarnessError("configure() takes either source or engine, not both.")
    _engine = engine if engine is not None else LoomAgentEngine(source=source)
    logger.info("engine configured: %s", type(_engine).__name__)


async def run_agent(
    name: str,
    input: Any,
    session: LoomSession,
    *,
    extras: dict[str, Any] | None = None,
) -> AgentReply:
    """Run agent ``name`` for one turn using the default (Loom) engine.

    The wired, batteries-included entry point — see
    :func:`weave.application.run.run_agent` for the dependency-injected core.
    """
    return await _run.run_agent(_engine, name, input, session, extras=extras)


def stream_agent(
    name: str,
    input: Any,
    session: LoomSession,
    *,
    extras: dict[str, Any] | None = None,
) -> AsyncIterator[ChatStreamEvent]:
    """Token-stream agent ``name`` for one turn using the default (Loom) engine.

    Returns the :class:`~weave.application.reply.ChatStreamEvent` async-iterator
    (consume with ``async for``); see :func:`weave.application.run.stream_agent`.
    """
    return _run.stream_agent(_engine, name, input, session, extras=extras)


async def run_workflow(
    name: str,
    input: Any,
    session: LoomSession,
    *,
    extras: dict[str, Any] | None = None,
) -> WorkflowReply:
    """Run multi-agent workflow ``name`` for one turn using the default (Loom) engine."""
    return await _run.run_workflow(_engine, name, input, session, extras=extras)


def open_stream(
    name: str,
    session: LoomSession,
    *,
    extras: dict[str, Any] | None = None,
) -> BidiStream:
    """Open a full-duplex stream over bidi target ``name`` (default Loom engine).

    Use as ``async with open_stream("voice", session) as stream: ...``.
    """
    return _stream.open_stream(_engine, name, session, extras=extras)


async def fork(
    store: MutableSession,
    name: str,
    input: Any,
    session: LoomSession,
    *,
    at: int,
    extras: dict[str, Any] | None = None,
    agent_id: str = "default",
) -> AgentReply:
    """Rewind a persisted conversation to ``at`` and continue (regenerate/edit/explore).

    The backend passes its mutable session ``store`` (e.g. a
    :class:`~weave.adapters.driven.mutable_file_session.MutableFileSessionManager`).
    Default Loom engine.
    """
    return await _run.fork(
        _engine, store, name, input, session, at=at, extras=extras, agent_id=agent_id
    )


async def fork_stateless(
    name: str,
    history: list[Any],
    input: Any,
    session: LoomSession,
    *,
    at: int | None = None,
    extras: dict[str, Any] | None = None,
) -> tuple[AgentReply, list[Any]]:
    """Stateless fork: caller owns the transcript; returns ``(reply, new_transcript)``."""
    return await _run.fork_stateless(_engine, name, history, input, session, at=at, extras=extras)


def catalog() -> list[TargetInfo]:
    """Return the catalog of buildable targets (default Loom engine)."""
    return _run.catalog(_engine)


async def run(
    name: str,
    input: Any,
    session: LoomSession,
    *,
    kind: str | None = None,
    extras: dict[str, Any] | None = None,
) -> AgentReply | WorkflowReply:
    """Run ``name`` one-shot, dispatching by kind (default Loom engine)."""
    return await _run.run(_engine, name, input, session, kind=kind, extras=extras)


def chat_channel(
    name: str,
    *,
    kind: str | None = None,
    registry: ConversationRegistry | None = None,
    session_policy: SessionPolicy | None = None,
) -> ChatChannel:
    """Build a :class:`~weave.adapters.driving.chat.ChatChannel` for ``name``.

    Wires the channel to the default (Loom) engine via :func:`run` (one-shot) and
    :func:`stream_agent` (token streaming). Mount it with
    :func:`weave.adapters.driving.chat.chat_router`, or call ``channel.handle(...)`` /
    ``channel.stream(...)`` from any transport. ``kind`` is inferred from the catalog
    when omitted.

    Pass a ``registry`` to record each (identified) turn into the conversation index,
    so :func:`weave.application.conversations.list_conversations` can later list the
    user's chats. Omit it and the channel records nothing — the index is opt-in.

    ``session_policy`` controls how a request's identity becomes the run's session;
    it defaults to the strict :func:`~weave.adapters.driving.identity.require` (a
    request must carry its own ``session_id``). Pass
    :func:`~weave.adapters.driving.identity.mint_if_absent` to generate one.
    """
    return ChatChannel(
        name,
        run,
        streamer=stream_agent,
        registry=registry,
        kind=kind,
        session_policy=session_policy,
    )


def routed_channel(
    resolve: TargetResolver,
    *,
    registry: ConversationRegistry | None = None,
    session_policy: SessionPolicy | None = None,
) -> ChatChannel:
    """Build a multi-target :class:`~weave.adapters.driving.chat.ChatChannel` (a router).

    ``resolve`` picks the target per request — pass
    :func:`weave.adapters.driving.routing.by_key` /
    :func:`~weave.adapters.driving.routing.by_rules`, or any
    ``(ChatRequest) -> str``. The backend owns the policy (which key, which table);
    Weave owns the dispatch. The target's ``kind`` is inferred per request from the
    catalog, so a router can mix agents and workflows. A request that maps to no
    target raises :class:`~weave.application.errors.RoutingError`.

    This is the same channel as :func:`chat_channel` — the single-target case is just
    a constant resolver. Mount it the same way (:func:`weave.adapters.driving.chat.chat_router`
    or :func:`weave.adapters.driving.lambda_handler.lambda_handler`). ``session_policy``
    behaves as in :func:`chat_channel` (strict :func:`~weave.adapters.driving.identity.require`
    by default).
    """
    return ChatChannel(
        resolve, run, streamer=stream_agent, registry=registry, session_policy=session_policy
    )


# ---------------------------------------------------------------------------
# Synchronous wrappers
#
# The harness is async-first, but the most common serverless target — an AWS
# Lambda ``def handler(event, context)`` — runs no event loop of its own and
# would otherwise wrap every call in ``asyncio.run(...)`` by hand. These absorb
# that boilerplate once. Only the one-shot coroutines get a ``_sync`` sibling;
# the streaming entry points (``stream_agent``, ``open_stream``) are inherently
# async and have none.
# ---------------------------------------------------------------------------


def _block_on(coro: Awaitable[_T]) -> _T:
    """Run a harness coroutine to completion from a synchronous caller.

    For a caller with no event loop of its own (a Lambda handler, a CLI). Raises
    :class:`~weave.application.errors.HarnessError` if a loop is already running —
    that caller is already async and should ``await`` the coroutine directly
    instead of blocking the loop it lives on.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    raise HarnessError(
        "a *_sync wrapper was called from within a running event loop; await the "
        "async function (e.g. run_agent) directly instead of blocking the loop."
    )


def run_agent_sync(
    name: str,
    input: Any,
    session: LoomSession,
    *,
    extras: dict[str, Any] | None = None,
) -> AgentReply:
    """Synchronous :func:`run_agent` — for a caller without an event loop (Lambda)."""
    return _block_on(run_agent(name, input, session, extras=extras))


def run_workflow_sync(
    name: str,
    input: Any,
    session: LoomSession,
    *,
    extras: dict[str, Any] | None = None,
) -> WorkflowReply:
    """Synchronous :func:`run_workflow` — for a caller without an event loop (Lambda)."""
    return _block_on(run_workflow(name, input, session, extras=extras))


def run_sync(
    name: str,
    input: Any,
    session: LoomSession,
    *,
    kind: str | None = None,
    extras: dict[str, Any] | None = None,
) -> AgentReply | WorkflowReply:
    """Synchronous :func:`run` — one-shot dispatch by kind for a sync caller (Lambda)."""
    return _block_on(run(name, input, session, kind=kind, extras=extras))


def fork_sync(
    store: MutableSession,
    name: str,
    input: Any,
    session: LoomSession,
    *,
    at: int,
    extras: dict[str, Any] | None = None,
    agent_id: str = "default",
) -> AgentReply:
    """Synchronous :func:`fork` — for a caller without an event loop (Lambda)."""
    return _block_on(fork(store, name, input, session, at=at, extras=extras, agent_id=agent_id))


def fork_stateless_sync(
    name: str,
    history: list[Any],
    input: Any,
    session: LoomSession,
    *,
    at: int | None = None,
    extras: dict[str, Any] | None = None,
) -> tuple[AgentReply, list[Any]]:
    """Synchronous :func:`fork_stateless` — for a caller without an event loop (Lambda)."""
    return _block_on(fork_stateless(name, history, input, session, at=at, extras=extras))
