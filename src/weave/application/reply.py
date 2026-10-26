"""weave.application.reply — The harness reply contract (AgentReply + Usage).

A *fine projection* of a Strands ``AgentResult`` into Weave's own envelope, plus a
``raw`` escape hatch to the full result. Weave owns this shape (Loom hands back a
native runnable; the harness normalizes what running it produces). The projection
is reimplemented here on purpose — Weave never reaches into ``loom.core`` — so the
~handful of mapping lines are duplicated by design, for independence.
"""

from __future__ import annotations

from typing import Any, Literal

from loom import LoomSession
from pydantic import BaseModel, ConfigDict, Field

from weave.domain.conversation import Interrupt


class Usage(BaseModel):
    """Slim token/latency/tool projection of ``result.metrics.get_summary()``.

    ``latency_ms`` is always the wall-clock time of the turn, in **milliseconds**, for
    agents and workflows alike. ``model_latency_ms`` is the summed model latency (what
    Strands reports as ``accumulated_metrics["latencyMs"]``), also in milliseconds — it
    excludes tool time and orchestration, so it is never larger than ``latency_ms`` for
    a sequential turn.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    model_latency_ms: float = 0.0
    tool_calls: list[str] = Field(default_factory=list)


class AgentReply(BaseModel):
    """Normalized result of running an agent for one turn.

    Interrupts are *reported* (the agent paused), not resumed here — resume is a
    conversation-level concern (:class:`~weave.domain.conversation.ConversationTree`).

    Wrapping this in your own response DTO? Map **every** field — ``interrupts`` and
    ``output`` are the easy ones to drop, and the loss stays invisible until an agent
    actually pauses or returns structured output. See the "Wrapping AgentReply" guide
    in ``docs/harness.md``.

    ``target``, ``user_id`` and ``is_anonymous`` say *which* agent ran and *as whom*
    (the effective :class:`~loom.LoomSession` — a routing resolver or a minting session
    policy may have decided them, so the caller cannot know them up front). Every reply
    produced by a Weave use case carries them; they are ``None`` / ``False`` only on a
    reply built by hand outside Weave (tests, doubles).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    output: BaseModel | None = None
    stop_reason: str | None = None
    interrupts: list[Interrupt] = Field(default_factory=list)
    usage: Usage
    session_id: str
    target: str | None = None
    user_id: str | None = None
    is_anonymous: bool = False
    # The full native AgentResult — escape hatch to traces/everything; never serialized.
    raw: Any = Field(default=None, exclude=True, repr=False)


def _text_of(result: Any) -> str:
    """Concatenate every text block of ``result.message`` (Loom takes only [0])."""
    message = getattr(result, "message", None)
    content = message.get("content", []) if isinstance(message, dict) else []
    return "".join(
        b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
    )


def _interrupts_of(result: Any) -> list[Interrupt]:
    """Map pending Strands interrupts (dict- or object-shaped) to domain Interrupts."""
    out: list[Interrupt] = []
    for item in getattr(result, "interrupts", None) or []:
        if isinstance(item, dict):
            out.append(
                Interrupt(
                    id=str(item.get("id", "")),
                    name=str(item.get("name", "")),
                    reason=item.get("reason"),
                )
            )
        else:
            out.append(
                Interrupt(
                    id=str(getattr(item, "id", "")),
                    name=str(getattr(item, "name", "")),
                    reason=getattr(item, "reason", None),
                )
            )
    return out


def _usage_of(result: Any) -> Usage:
    """Project ``result.metrics.get_summary()`` into the slim :class:`Usage`."""
    metrics = getattr(result, "metrics", None)
    summary = (metrics.get_summary() if metrics is not None else {}) or {}
    acc = summary.get("accumulated_usage", {}) or {}
    model_metrics = summary.get("accumulated_metrics", {}) or {}
    return Usage(
        input_tokens=acc.get("inputTokens", 0),
        output_tokens=acc.get("outputTokens", 0),
        total_tokens=acc.get("totalTokens", 0),
        # Strands reports ``total_duration`` (sum of cycle durations) in seconds.
        latency_ms=float(summary.get("total_duration", 0.0) or 0.0) * 1000.0,
        model_latency_ms=float(model_metrics.get("latencyMs", 0) or 0),
        tool_calls=list((summary.get("tool_usage", {}) or {}).keys()),
    )


def agent_reply_from_result(result: Any, session: LoomSession, *, target: str) -> AgentReply:
    """Project a native Strands ``AgentResult`` into an :class:`AgentReply`.

    ``session`` and ``target`` are what the turn ran as and against; they fill the
    reply's identity fields (``session_id``, ``user_id``, ``is_anonymous``, ``target``).
    """
    return AgentReply(
        text=_text_of(result),
        output=getattr(result, "structured_output", None),
        stop_reason=getattr(result, "stop_reason", None),
        interrupts=_interrupts_of(result),
        usage=_usage_of(result),
        session_id=session.session_id,
        target=target,
        user_id=session.user_id,
        is_anonymous=session.is_anonymous,
        raw=result,
    )


class ChatStreamEvent(BaseModel):
    """One event from a token-streamed agent turn (the streaming sibling of AgentReply).

    A fine projection of Strands' ``stream_async`` events into a small union:
    ``delta`` carries an incremental text chunk; ``done`` carries the final
    :class:`AgentReply` (usage, stop_reason, interrupts). Everything else (tool-use
    detail, lifecycle) stays in ``raw`` — same projection-plus-escape-hatch shape as
    :class:`AgentReply` and the bidi ``StreamEvent``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    type: Literal["delta", "done"]
    text: str | None = None
    reply: AgentReply | None = None
    raw: Any = Field(default=None, exclude=True, repr=False)


class WorkflowReply(BaseModel):
    """Normalized result of running a multi-agent workflow (Graph/Swarm) for one turn.

    ``status`` is prominent (COMPLETED / FAILED / INTERRUPTED): a workflow can
    finish without producing a terminal answer. ``execution_order`` is the node
    ids in the order they ran; per-node detail is reachable through ``raw``.
    ``target`` / ``user_id`` / ``is_anonymous`` mirror :class:`AgentReply`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    status: str
    usage: Usage
    execution_order: list[str] = Field(default_factory=list)
    interrupts: list[Interrupt] = Field(default_factory=list)
    session_id: str
    target: str | None = None
    user_id: str | None = None
    is_anonymous: bool = False
    raw: Any = Field(default=None, exclude=True, repr=False)


def _field(obj: Any, key: str, default: Any = 0) -> Any:
    """Read ``key`` whether ``obj`` is a dict (TypedDict) or an attr-bearing object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _agent_text(obj: Any) -> str:
    """Concatenate text blocks of an AgentResult message, or recurse into a nested result."""
    message = getattr(obj, "message", None)
    if isinstance(message, dict):
        content = message.get("content") or []
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    if getattr(obj, "results", None):
        return _final_text(obj)
    return ""


def _final_text(result: Any) -> str:
    """Terminal answer: last-executed node's agent text, recursing into nested results."""
    results = getattr(result, "results", None) or {}
    for node_result in reversed(list(results.values())):
        inner = getattr(node_result, "result", node_result)
        text = _agent_text(inner)
        if text:
            return text
    return ""


def _workflow_usage_of(result: Any) -> Usage:
    """Project a MultiAgentResult's accumulated usage/metrics into :class:`Usage`."""
    acc = getattr(result, "accumulated_usage", {}) or {}
    metrics = getattr(result, "accumulated_metrics", {}) or {}
    return Usage(
        input_tokens=_field(acc, "inputTokens", 0),
        output_tokens=_field(acc, "outputTokens", 0),
        total_tokens=_field(acc, "totalTokens", 0),
        # ``execution_time`` is the workflow's wall-clock time, already in ms.
        latency_ms=float(getattr(result, "execution_time", 0) or 0),
        model_latency_ms=float(_field(metrics, "latencyMs", 0) or 0),
    )


def workflow_reply_from_result(result: Any, session: LoomSession, *, target: str) -> WorkflowReply:
    """Project a native Strands ``MultiAgentResult`` into a :class:`WorkflowReply`."""
    status = getattr(result, "status", None)
    status_str = getattr(status, "name", None) or str(status) if status is not None else "UNKNOWN"
    return WorkflowReply(
        text=_final_text(result),
        status=status_str,
        usage=_workflow_usage_of(result),
        execution_order=list((getattr(result, "results", None) or {}).keys()),
        interrupts=_interrupts_of(result),
        session_id=session.session_id,
        target=target,
        user_id=session.user_id,
        is_anonymous=session.is_anonymous,
        raw=result,
    )
