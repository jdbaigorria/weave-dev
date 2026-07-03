"""weave.application.run — Minimal one-shot executor over a built agent.

The seam Weave adds over Loom: it collapses the repeated
``build → invoke → shape → teardown`` boilerplate and *guarantees* the teardown.
A single MCP-backed agent already justifies it — something has to run the
``finally: agent.cleanup()``. It does not reimplement Strands' execution; it wraps
it.

Async-first: voice/streaming and concurrency are first-class, so the executor uses
``agent.invoke_async``. ``extras`` flow through as native Strands
``invocation_state`` (tools read ``tool_context.invocation_state``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from loom import LoomSession, TargetInfo

from weave.application.errors import HarnessError
from weave.application.reply import (
    AgentReply,
    ChatStreamEvent,
    WorkflowReply,
    agent_reply_from_result,
    workflow_reply_from_result,
)
from weave.ports.agent_engine import AgentEngine
from weave.ports.session import MutableSession

logger = logging.getLogger(__name__)


async def run_agent(
    engine: AgentEngine,
    name: str,
    input: Any,
    session: LoomSession,
    *,
    extras: dict[str, Any] | None = None,
    model_params: dict[str, Any] | None = None,
) -> AgentReply:
    """Build agent ``name``, run one turn, and return a shaped :class:`AgentReply`.

    Build-time failures propagate Loom's own typed errors; execution failures are
    wrapped in :class:`~weave.application.errors.HarnessError`. The agent (and any
    MCP connections it opened) is always torn down via ``agent.cleanup()``.

    Args:
        engine: The :class:`~weave.ports.agent_engine.AgentEngine` (Loom by default).
        name: Target agent name.
        input: The input Strands expects — a string or a list of content blocks.
        session: Identity (``session_id`` / ``user_id``) for this run.
        extras: Free-form context forwarded as native ``invocation_state``.
        model_params: Optional per-run overlay of model constructor params (merged on
            top of the declarative config). The build-time channel for per-run secrets
            — e.g. a Bedrock ``boto_session`` carrying per-client credentials.
    """
    logger.debug("building agent %r (session=%s)", name, session.session_id)
    agent = engine.build_agent(name, session, model_params=model_params)
    try:
        result = await agent.invoke_async(input, invocation_state=extras or {})
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed harness error
        raise HarnessError(f"agent '{name}' failed during execution: {exc}") from exc
    finally:
        agent.cleanup()
        logger.debug("tore down agent %r (session=%s)", name, session.session_id)

    logger.debug("agent %r completed turn (session=%s)", name, session.session_id)
    return agent_reply_from_result(result, session.session_id)


async def stream_agent(
    engine: AgentEngine,
    name: str,
    input: Any,
    session: LoomSession,
    *,
    extras: dict[str, Any] | None = None,
    model_params: dict[str, Any] | None = None,
) -> AsyncIterator[ChatStreamEvent]:
    """Build agent ``name`` and stream one turn as :class:`ChatStreamEvent`s.

    The token-streaming sibling of :func:`run_agent`: instead of awaiting the whole
    turn it yields ``delta`` events as text is generated, then a final ``done`` event
    carrying the projected :class:`~weave.application.reply.AgentReply`. Same
    lifecycle discipline — the agent (and any MCP connections) is always torn down via
    ``agent.cleanup()``, including when the consumer abandons the stream early
    (``aclose`` runs the ``finally``).

    Strands' ``stream_async`` emits dicts: ``{"data": str}`` for a text delta and a
    terminal ``{"result": AgentResult}``; tool-use and lifecycle detail ride along in
    each event's ``raw``.
    """
    logger.debug("building agent %r to stream (session=%s)", name, session.session_id)
    agent = engine.build_agent(name, session, model_params=model_params)
    try:
        async for event in agent.stream_async(input, invocation_state=extras or {}):
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("data"), str):
                yield ChatStreamEvent(type="delta", text=event["data"], raw=event)
            if "result" in event:
                yield ChatStreamEvent(
                    type="done",
                    reply=agent_reply_from_result(event["result"], session.session_id),
                    raw=event,
                )
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed harness error
        raise HarnessError(f"agent '{name}' failed during streaming: {exc}") from exc
    finally:
        agent.cleanup()
        logger.debug("tore down streamed agent %r (session=%s)", name, session.session_id)


async def run_workflow(
    engine: AgentEngine,
    name: str,
    input: Any,
    session: LoomSession,
    *,
    extras: dict[str, Any] | None = None,
    model_params: dict[str, Any] | None = None,
) -> WorkflowReply:
    """Build multi-agent workflow ``name``, run it once, return a :class:`WorkflowReply`.

    Same lifecycle discipline as :func:`run_agent`, but a Graph/Swarm has no
    ``cleanup()`` of its own, so each node agent is torn down individually (closing
    its MCP connections). ``extras`` propagate to every node as ``invocation_state``.
    ``model_params`` is applied to every node's model (one overlay per run).
    """
    logger.debug("building workflow %r (session=%s)", name, session.session_id)
    workflow, node_agents = engine.build_workflow(name, session, model_params=model_params)
    try:
        result = await workflow.invoke_async(input, invocation_state=extras or {})
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed harness error
        raise HarnessError(f"workflow '{name}' failed during execution: {exc}") from exc
    finally:
        for node_agent in node_agents:
            node_agent.cleanup()
        logger.debug(
            "tore down workflow %r (%d node agents, session=%s)",
            name,
            len(node_agents),
            session.session_id,
        )

    logger.debug("workflow %r completed turn (session=%s)", name, session.session_id)
    return workflow_reply_from_result(result, session.session_id)


async def fork(
    engine: AgentEngine,
    store: MutableSession,
    name: str,
    input: Any,
    session: LoomSession,
    *,
    at: int,
    extras: dict[str, Any] | None = None,
    agent_id: str = "default",
    model_params: dict[str, Any] | None = None,
) -> AgentReply:
    """Rewind a **persisted** conversation to ``at`` and continue with ``input``.

    The one primitive behind regenerate / edit / explore (overwrite style, no
    compare) when Strands owns the conversation. It truncates the persisted session
    to its first ``at`` messages via the ``store`` (Strands sessions are append-only,
    so the store — a :class:`~weave.ports.session.MutableSession` provided by the
    backend — supplies the truncation), then builds the agent, which **re-initialises
    cleanly from the now-truncated session** (re-syncing messages and the next-id
    cursor), and runs ``input``. The new turn is persisted by Strands.

    - regenerate: ``at`` before the last exchange, ``input`` = the same user message.
    - edit K:     ``at`` = K, ``input`` = the edited message.
    - explore K:  ``at`` = K, ``input`` = a different message.

    For stateless agents (no session persistence) use :func:`fork_stateless`.

    .. warning::

        **Serialize calls per ``session_id``.** Strands enforces sequentiality only
        *per agent instance* (a second ``invoke`` on one instance raises
        ``ConcurrencyException``). Under the rebuild-per-request model each call here
        builds its *own* instance, so that guard does **not** span a session: a fork
        racing a concurrent ``run_agent`` / ``fork`` for the same ``session_id`` would
        truncate the append-only session out from under the other turn, corrupting it.
        Weave does not impose locking — the backend must serialize concurrent turns on
        a conversation (e.g. a per-``session_id`` lock or queue).
    """
    if not isinstance(store, MutableSession):
        raise HarnessError(
            "fork needs a mutable session store (implementing truncate); "
            "use fork_stateless for stateless agents."
        )
    store.truncate(session.session_id, keep=at, agent_id=agent_id)

    # re-inits from the truncated session
    agent = engine.build_agent(name, session, model_params=model_params)
    try:
        result = await agent.invoke_async(input, invocation_state=extras or {})
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed harness error
        raise HarnessError(f"fork of '{name}' failed during execution: {exc}") from exc
    finally:
        agent.cleanup()

    return agent_reply_from_result(result, session.session_id)


async def fork_stateless(
    engine: AgentEngine,
    name: str,
    history: list[Any],
    input: Any,
    session: LoomSession,
    *,
    at: int | None = None,
    extras: dict[str, Any] | None = None,
    model_params: dict[str, Any] | None = None,
) -> tuple[AgentReply, list[Any]]:
    """Like :func:`fork`, but for stateless agents — the caller owns the transcript.

    Seeds the agent with ``history[:at]`` and runs ``input``; returns the reply
    **and the new full transcript** for the caller to persist. Use when session
    persistence is off and your app holds the conversation itself.
    """
    agent = engine.build_agent(name, session, model_params=model_params)
    agent.messages = list(history if at is None else history[:at])
    try:
        result = await agent.invoke_async(input, invocation_state=extras or {})
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed harness error
        raise HarnessError(f"fork of '{name}' failed during execution: {exc}") from exc
    finally:
        agent.cleanup()

    return agent_reply_from_result(result, session.session_id), list(agent.messages)


def catalog(engine: AgentEngine) -> list[TargetInfo]:
    """Return the catalog of buildable targets (name + kind + description).

    A thin passthrough of the engine's discovery — the entry point a backend uses
    to learn what it can run without importing anything.
    """
    return engine.discover()


def _kind_of(engine: AgentEngine, name: str) -> str:
    """Resolve a target's kind from the catalog (the harness branches on it)."""
    for target in engine.discover():
        if target.name == name:
            return str(target.kind)
    raise HarnessError(f"unknown target '{name}'")


async def run(
    engine: AgentEngine,
    name: str,
    input: Any,
    session: LoomSession,
    *,
    kind: str | None = None,
    extras: dict[str, Any] | None = None,
    model_params: dict[str, Any] | None = None,
) -> AgentReply | WorkflowReply:
    """Run ``name`` one-shot, dispatching by kind (discovered unless ``kind`` is given).

    Agents and workflows return their reply; ``bidi`` targets are full-duplex and
    have no one-shot reply — use :func:`~weave.application.stream.open_stream`.
    """
    resolved = kind or _kind_of(engine, name)
    logger.debug(
        "dispatching target %r as kind=%s (session=%s)", name, resolved, session.session_id
    )
    if resolved == "agent":
        return await run_agent(engine, name, input, session, extras=extras, model_params=model_params)
    if resolved == "workflow":
        return await run_workflow(engine, name, input, session, extras=extras, model_params=model_params)
    if resolved == "bidi":
        raise HarnessError(f"'{name}' is a bidi (full-duplex) target — use open_stream, not run.")
    raise HarnessError(f"unknown kind '{resolved}' for target '{name}'.")
