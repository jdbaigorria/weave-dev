"""weave.adapters.driven.file_registry — Reference ConversationRegistry (filesystem).

Dev/reference implementation of the metadata-only conversation index: one JSON file
per ``(user, session)`` under ``<storage_dir>/<user_id>/<session_id>.json``. The
per-user directory makes ``list(user_id)`` a directory scan and keeps users isolated.

A production store implements the same :class:`~weave.ports.registry.ConversationRegistry`
port directly: e.g. DynamoDB partitioned on ``user_id`` with ``session_id`` as the
sort key gives list/record/delete with no scanning. The ``clock`` seam keeps the
timestamps injectable for tests.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from weave.ports.registry import ConversationRef

# Filesystem-safe path segment for a reference impl; production stores key directly.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class FileConversationRegistry:
    """``ConversationRegistry`` backed by per-user JSON files (reference impl)."""

    def __init__(
        self, storage_dir: str | None = None, *, clock: Callable[[], str] = _utc_now
    ) -> None:
        self.storage_dir = storage_dir or os.path.join(
            tempfile.gettempdir(), "weave", "conversations"
        )
        self._clock = clock

    def record(
        self,
        user_id: str,
        session_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        path = self._path(user_id, session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = self._clock()
        existing = self._read(path) or {}
        data = {
            "session_id": session_id,
            "user_id": user_id,
            "created_at": existing.get("created_at", now),
            "updated_at": now,
            "title": title if title is not None else existing.get("title"),
            "metadata": metadata if metadata is not None else existing.get("metadata") or {},
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def list(self, user_id: str) -> list[ConversationRef]:
        user_dir = self._user_dir(user_id)
        if not os.path.isdir(user_dir):
            return []
        refs: list[ConversationRef] = []
        for name in os.listdir(user_dir):
            if not name.endswith(".json"):
                continue
            data = self._read(os.path.join(user_dir, name))
            if data is None:
                continue
            refs.append(
                ConversationRef(
                    session_id=data["session_id"],
                    user_id=data["user_id"],
                    created_at=data["created_at"],
                    updated_at=data["updated_at"],
                    title=data.get("title"),
                    metadata=data.get("metadata") or {},
                )
            )
        refs.sort(key=lambda ref: ref.updated_at, reverse=True)
        return refs

    def delete(self, user_id: str, session_id: str) -> None:
        path = self._path(user_id, session_id)
        if os.path.exists(path):
            os.remove(path)

    def _user_dir(self, user_id: str) -> str:
        return os.path.join(self.storage_dir, _safe(user_id))

    def _path(self, user_id: str, session_id: str) -> str:
        return os.path.join(self._user_dir(user_id), f"{_safe(session_id)}.json")

    @staticmethod
    def _read(path: str) -> dict[str, Any] | None:
        try:
            with open(path, encoding="utf-8") as fh:
                return cast("dict[str, Any]", json.load(fh))
        except (OSError, json.JSONDecodeError):
            return None


def _safe(segment: str) -> str:
    """Collapse a path segment to a filesystem-safe form (reference-impl scope)."""
    return _UNSAFE.sub("_", segment) or "_"
