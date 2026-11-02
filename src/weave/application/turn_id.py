"""weave.application.turn_id — The id of one turn, end to end.

A turn's id is what ties a reply, its metrics and the persisted history together —
the key a product hangs user feedback on. It is the caller's (e.g. the frontend's
message id) when given, or minted here.

The id is stamped on the **user message that opens the turn**, in Strands'
``metadata.custom`` (persisted with the session, stripped before model calls, so it
costs no tokens). The turn is that message and everything after it up to the next
stamped message; :func:`turn_id_of` reads the stamp back.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Any

from weave.application.errors import RequestError

#: The ``metadata.custom`` key Weave owns on a persisted message.
TURN_ID_KEY = "weave.turn_id"

_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def new_turn_id() -> str:
    """A fresh turn id (32 hex chars)."""
    return uuid.uuid4().hex


def valid_turn_id(value: Any) -> bool:
    """Whether ``value`` can be a turn id: 1–64 of ``[A-Za-z0-9_-]`` (a UUID fits)."""
    return isinstance(value, str) and _PATTERN.fullmatch(value) is not None


def resolve_turn_id(value: str | None) -> str:
    """The caller's id, validated, or a new one when absent.

    It ends up in S3 keys, logs and metrics and comes from a client, so a malformed
    one is a :class:`~weave.application.errors.RequestError` (→ 400), never sanitised.
    """
    if value is None:
        return new_turn_id()
    if not valid_turn_id(value):
        raise RequestError(f"invalid turn_id {value!r}: expected 1-64 of [A-Za-z0-9_-].")
    return value


def mark_input(input: Any, turn_id: str) -> Any:
    """Return ``input`` as messages whose opening user message carries ``turn_id``.

    - ``str`` or a list of content blocks → one user message, stamped.
    - a list of messages → a copy, with the first ``user`` message stamped.
    - anything else (interrupt responses, ``None``) → unchanged: the turn stays
      unstamped in the history.

    The input is never mutated.
    """
    if isinstance(input, str):
        return [_stamped({"role": "user", "content": [{"text": input}]}, turn_id)]
    if not isinstance(input, list) or not input or not all(isinstance(i, dict) for i in input):
        return input
    if all("role" in item and "content" in item for item in input):
        marked = list(input)
        for index, message in enumerate(marked):
            if message.get("role") == "user":
                marked[index] = _stamped(message, turn_id)
                break
        return marked
    if any("interruptResponse" in item for item in input):
        return input
    return [_stamped({"role": "user", "content": list(input)}, turn_id)]


def turn_id_of(message: Any) -> str | None:
    """The turn id stamped on ``message`` (a Strands message dict), if any."""
    if not isinstance(message, dict):
        return None
    custom = (message.get("metadata") or {}).get("custom") or {}
    value = custom.get(TURN_ID_KEY)
    return value if isinstance(value, str) else None


def _stamped(message: dict[str, Any], turn_id: str) -> dict[str, Any]:
    metadata = dict(message.get("metadata") or {})
    metadata["custom"] = {**(metadata.get("custom") or {}), TURN_ID_KEY: turn_id}
    return {**message, "metadata": metadata}


def last_turn_start(messages_from_the_end: Iterable[Any], turn_id: str) -> int | None:
    """``message_id`` of the message opening ``turn_id``, if it is the last stamped turn.

    ``messages_from_the_end`` are Strands ``SessionMessage`` objects, newest first; it
    stops at the first stamped one, so a lazy iterable reads only what it needs.
    """
    for session_message in messages_from_the_end:
        stamped = turn_id_of(session_message.message)
        if stamped is not None:
            return session_message.message_id if stamped == turn_id else None
    return None
