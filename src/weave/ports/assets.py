"""weave.ports.assets — Capability port for user asset storage.

A user's uploads land in a store and are referenced later (the reference travels
from the UI; Weave exposes the capability, swappable across backends — S3, GCS,
local FS, …). The core :class:`AssetStore` (put/get/list/delete) works anywhere;
**presigned URLs** — direct UI↔bucket transfer, the serverless-friendly path that
keeps large payloads out of the app — are an optional capability
(:class:`PresignedAssetStore`) for stores that support them.

Like :class:`~weave.ports.session.MutableSession` /
:class:`~weave.ports.registry.ConversationRegistry`, this is a capability the harness
defines and a backend wires; Weave ships reference adapters but holds no default
store. A stored :class:`AssetRef` resolves to a content block for a turn via
``store.get(ref)`` → :func:`weave.application.content.file_block`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AssetRef:
    """A stored asset as the store knows it — the handle the UI keeps and references.

    ``content_type``/``size``/``created_at`` are best-effort metadata (a presign
    handle has none until the upload lands). ``asset_id`` + ``user_id`` + ``filename``
    are enough for the store to locate the blob.
    """

    asset_id: str
    user_id: str
    filename: str
    content_type: str | None = None
    size: int | None = None
    created_at: str | None = None


@runtime_checkable
class AssetStore(Protocol):
    """Durable, user-scoped blob storage (works on any backend, incl. local FS)."""

    def put(
        self, user_id: str, filename: str, data: bytes, *, content_type: str | None = None
    ) -> AssetRef:
        """Store ``data`` for ``user_id`` and return its :class:`AssetRef`."""
        ...

    def get(self, ref: AssetRef) -> bytes:
        """Return the bytes for ``ref`` (raises ``HarnessError`` if missing)."""
        ...

    def list(self, user_id: str) -> list[AssetRef]:
        """Return the user's assets, most-recent first."""
        ...

    def delete(self, ref: AssetRef) -> None:
        """Remove the asset ``ref`` points to."""
        ...


@runtime_checkable
class PresignedAssetStore(AssetStore, Protocol):
    """An :class:`AssetStore` that can mint direct UI↔bucket URLs (S3, GCS, …)."""

    def presign_upload(
        self,
        user_id: str,
        filename: str,
        *,
        content_type: str | None = None,
        expires_in: int = 3600,
    ) -> tuple[str, AssetRef]:
        """Return ``(upload_url, ref)``: the UI PUTs the bytes to ``upload_url``.

        The ``ref`` is reserved now (its ``size`` is unknown until the upload lands).
        """
        ...

    def presign_download(self, ref: AssetRef, *, expires_in: int = 3600) -> str:
        """Return a time-limited URL the UI can GET to download ``ref``."""
        ...
