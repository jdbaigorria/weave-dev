"""weave.adapters.driven.local_assets — Reference AssetStore (filesystem).

Dev/reference impl: each asset is a blob (``<asset_id>.bin``) plus a JSON sidecar of
its metadata (``<asset_id>.json``), under ``<storage_dir>/<user_id>/``. No presign —
the local FS can't mint URLs; use :class:`~weave.adapters.driven.s3_assets.S3AssetStore`
for that. The ``clock`` seam keeps ``created_at`` injectable for tests.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, cast

from weave.application.errors import HarnessError
from weave.ports.assets import AssetRef

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LocalAssetStore:
    """``AssetStore`` backed by per-user blob+sidecar files (reference impl)."""

    def __init__(
        self, storage_dir: str | None = None, *, clock: Callable[[], str] = _utc_now
    ) -> None:
        self.storage_dir = storage_dir or os.path.join(tempfile.gettempdir(), "weave", "assets")
        self._clock = clock

    def put(
        self, user_id: str, filename: str, data: bytes, *, content_type: str | None = None
    ) -> AssetRef:
        asset_id = uuid.uuid4().hex
        ref = AssetRef(
            asset_id=asset_id,
            user_id=user_id,
            filename=filename,
            content_type=content_type,
            size=len(data),
            created_at=self._clock(),
        )
        os.makedirs(self._user_dir(user_id), exist_ok=True)
        with open(self._blob(user_id, asset_id), "wb") as fh:
            fh.write(data)
        with open(self._meta(user_id, asset_id), "w", encoding="utf-8") as fh:
            json.dump(asdict(ref), fh)
        return ref

    def get(self, ref: AssetRef) -> bytes:
        try:
            with open(self._blob(ref.user_id, ref.asset_id), "rb") as fh:
                return fh.read()
        except OSError as exc:
            raise HarnessError(
                f"asset {ref.asset_id!r} not found for user {ref.user_id!r}."
            ) from exc

    def list(self, user_id: str) -> list[AssetRef]:
        user_dir = self._user_dir(user_id)
        if not os.path.isdir(user_dir):
            return []
        refs: list[AssetRef] = []
        for name in os.listdir(user_dir):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(user_dir, name), encoding="utf-8") as fh:
                    refs.append(AssetRef(**cast("dict[str, Any]", json.load(fh))))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        refs.sort(key=lambda ref: ref.created_at or "", reverse=True)
        return refs

    def delete(self, ref: AssetRef) -> None:
        for path in (self._blob(ref.user_id, ref.asset_id), self._meta(ref.user_id, ref.asset_id)):
            if os.path.exists(path):
                os.remove(path)

    def _user_dir(self, user_id: str) -> str:
        return os.path.join(self.storage_dir, _safe(user_id))

    def _blob(self, user_id: str, asset_id: str) -> str:
        return os.path.join(self._user_dir(user_id), f"{_safe(asset_id)}.bin")

    def _meta(self, user_id: str, asset_id: str) -> str:
        return os.path.join(self._user_dir(user_id), f"{_safe(asset_id)}.json")


def _safe(segment: str) -> str:
    """Collapse a path segment to a filesystem-safe form (reference-impl scope)."""
    return _UNSAFE.sub("_", segment) or "_"
