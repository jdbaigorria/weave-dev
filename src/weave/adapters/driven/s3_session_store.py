"""weave.adapters.driven.s3_session_store — Rewind sessions persisted by Strands in S3.

The :class:`~weave.ports.session.RegenerableSession` for Loom's ``session.provider: s3``.
It reuses Strands' ``S3SessionManager`` key layout and reads, but it is **not** a
session manager: Strands' constructor reads (and creates) one session, while a store
serves any ``session_id`` passed per call. So it skips that constructor and only keeps
the client, the bucket and the prefix.

Build it with the same ``bucket`` and ``prefix`` as ``session.config`` in ``app.yml``;
otherwise it rewinds folders the agent never reads.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from strands.session.s3_session_manager import MESSAGE_PREFIX, S3SessionManager
from strands.types.exceptions import SessionException
from strands.types.session import SessionMessage

from weave.application.turn_id import last_turn_start

_DELETE_BATCH = 1000
_INDEX = re.compile(rf"{MESSAGE_PREFIX}(\d+)\.json$")


class S3SessionStore(S3SessionManager):
    """``truncate`` and ``turn_start`` over Strands' S3 session layout."""

    def __init__(self, bucket: str, prefix: str = "", *, client: Any = None) -> None:
        # Deliberately not super().__init__: that would read/create a session.
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix
        self.client = client

    def truncate(self, session_id: str, *, keep: int, agent_id: str) -> None:
        """Delete the messages with ``message_id >= keep`` (negative counts from the end)."""
        indices = [index for index, _ in self._message_keys(session_id, agent_id)]
        cutoff = keep if keep >= 0 else max(0, len(indices) + keep)
        doomed = [
            {"Key": self._get_message_path(session_id, agent_id, index)}
            for index in indices
            if index >= cutoff
        ]
        for start in range(0, len(doomed), _DELETE_BATCH):
            response = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": doomed[start : start + _DELETE_BATCH], "Quiet": True},
            )
            if response.get("Errors"):
                raise SessionException(f"S3 could not delete messages: {response['Errors'][:3]}")

    def turn_start(self, session_id: str, *, agent_id: str, turn_id: str) -> int | None:
        """One list, then reads newest-first until the first stamped message."""
        return last_turn_start(self._newest_first(session_id, agent_id), turn_id)

    def _newest_first(self, session_id: str, agent_id: str) -> Iterator[SessionMessage]:
        for _, key in sorted(self._message_keys(session_id, agent_id), reverse=True):
            data = self._read_s3_object(key)
            if data is not None:
                yield SessionMessage.from_dict(data)

    def _message_keys(self, session_id: str, agent_id: str) -> list[tuple[int, str]]:
        prefix = f"{self._get_agent_path(session_id, agent_id)}messages/"
        keys: list[tuple[int, str]] = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        ):
            for obj in page.get("Contents", []):
                match = _INDEX.search(obj["Key"])
                if match:
                    keys.append((int(match.group(1)), obj["Key"]))
        return keys
