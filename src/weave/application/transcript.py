"""weave.application.transcript — The content of one turn, read from its input and messages.

Pure helpers over Strands' message dicts, used by :mod:`weave.application.observe` to
build a :class:`~weave.application.observe.TurnTranscript` (W22):

- :func:`question_of` — the text of the user message that opens the turn, read from
  the use case's ``input`` (so it exists even when the build fails). Blocks that are
  not text carry bytes and are reduced to their type (``"image/png"``).
- :func:`turn_messages` — the messages of the turn: from the one stamped with its
  ``turn_id`` (W15) to the end.
- :func:`tool_calls_of` — each ``toolUse`` of the assistant, with the ``status`` and
  content of its ``toolResult``.

The shapes are read defensively: anything unexpected is skipped, never raised.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from weave.application.turn_id import turn_id_of

# Content-block kinds that carry media: reduced to "<kind>/<format>" in a transcript.
_MEDIA_KINDS = ("image", "document", "video")


class ToolCall(BaseModel):
    """One tool call of the turn, in the order the model asked for it.

    ``status`` is ``None`` when the turn ended before the tool answered. ``result`` is the
    ``toolResult`` content; an observer that stores the transcript decides whether to
    keep it (it can be large and carry personal data).
    """

    model_config = ConfigDict(frozen=True)

    tool_use_id: str
    name: str
    input: Any = None
    status: Literal["success", "error"] | None = None
    result: list[dict[str, Any]] | None = None


def question_of(input: Any) -> tuple[str, tuple[str, ...]]:
    """``(text, attachments)`` of the user message that opens a turn.

    Same input shapes as :func:`~weave.application.turn_id.mark_input`: a string, a
    list of content blocks, or a list of messages (the first ``user`` one). Anything
    else (interrupt responses, ``None``) has no question: ``("", ())``.
    """
    if isinstance(input, str):
        return input, ()
    if not isinstance(input, list) or not all(isinstance(item, dict) for item in input):
        return "", ()
    if input and all("role" in item and "content" in item for item in input):
        for message in input:
            if message.get("role") == "user":
                return _blocks_text(message.get("content"))
        return "", ()
    if any("interruptResponse" in item for item in input):
        return "", ()
    return _blocks_text(input)


def turn_messages(messages: Sequence[Any], turn_id: str) -> list[Any]:
    """The messages from the last one stamped with ``turn_id`` to the end; ``[]`` if none is."""
    for index in range(len(messages) - 1, -1, -1):
        if turn_id_of(messages[index]) == turn_id:
            return list(messages[index:])
    return []


def tool_calls_of(messages: Iterable[Any]) -> tuple[ToolCall, ...]:
    """Every ``toolUse`` in ``messages``, completed with its ``toolResult``."""
    uses: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            use = block.get("toolUse")
            if isinstance(use, dict) and isinstance(use.get("toolUseId"), str):
                uses.append(use)
            result = block.get("toolResult")
            if isinstance(result, dict) and isinstance(result.get("toolUseId"), str):
                results[result["toolUseId"]] = result
    return tuple(_tool_call(use, results.get(use["toolUseId"])) for use in uses)


def _tool_call(use: dict[str, Any], result: dict[str, Any] | None) -> ToolCall:
    status = result.get("status") if result is not None else None
    content = result.get("content") if result is not None else None
    return ToolCall(
        tool_use_id=use["toolUseId"],
        name=str(use.get("name") or ""),
        input=use.get("input"),
        status=status if status in ("success", "error") else None,
        result=list(content) if isinstance(content, list) else None,
    )


def _blocks_text(blocks: Any) -> tuple[str, tuple[str, ...]]:
    if not isinstance(blocks, list):
        return "", ()
    texts: list[str] = []
    attachments: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            texts.append(block["text"])
            continue
        kind = next((k for k in _MEDIA_KINDS if isinstance(block.get(k), dict)), None)
        if kind is not None:
            attachments.append(f"{kind}/{block[kind].get('format', 'unknown')}")
        elif block:
            attachments.append(next(iter(block)))
    return "\n".join(texts), tuple(attachments)
