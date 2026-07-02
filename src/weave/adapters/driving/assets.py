"""weave.adapters.driving.assets — Mountable asset endpoints (presign + list).

An opt-in FastAPI ``APIRouter`` over a
:class:`~weave.ports.assets.PresignedAssetStore`: mint a direct-to-bucket upload URL,
list a user's assets, mint a download URL. Like
:func:`~weave.adapters.driving.chat.chat_router`, FastAPI is imported lazily (gated
behind ``weave[fastapi]``) and the store is the backend's. Identity (``user_id``)
arrives in the request — the backend should enforce auth (same caveat as the chat
router); the mount point is the authorization boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from weave.application.errors import HarnessError
from weave.ports.assets import AssetRef, PresignedAssetStore

if TYPE_CHECKING:
    from fastapi import APIRouter


class UploadRequest(BaseModel):
    """Ask for an upload URL for one file."""

    user_id: str
    filename: str
    content_type: str | None = None


class DownloadRequest(BaseModel):
    """Ask for a download URL for one stored asset (identified like an AssetRef)."""

    user_id: str
    asset_id: str
    filename: str


def asset_router(store: PresignedAssetStore, *, prefix: str = "/assets") -> APIRouter:
    """Return a FastAPI ``APIRouter`` exposing presign + list over ``store``.

    Routes: ``POST {prefix}/uploads`` → ``{upload_url, ref}``; ``GET {prefix}?user_id=``
    → ``{assets: [AssetRef]}``; ``POST {prefix}/downloads`` → ``{download_url}``.
    Mount it: ``app.include_router(asset_router(store))``. Deleting is a direct
    ``store.delete(ref)`` call (no route, to keep the surface small).
    """
    if not isinstance(store, PresignedAssetStore):
        raise HarnessError(
            "asset_router needs a PresignedAssetStore (implementing presign_upload/"
            "presign_download) — e.g. S3AssetStore, not the local reference store."
        )
    try:
        from fastapi import APIRouter, HTTPException
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise HarnessError(
            "asset_router needs FastAPI; install the extra: weave[fastapi]."
        ) from exc

    router = APIRouter()

    @router.post(f"{prefix}/uploads")
    async def create_upload(request: UploadRequest) -> dict[str, object]:
        url, ref = store.presign_upload(
            request.user_id, request.filename, content_type=request.content_type
        )
        return {"upload_url": url, "ref": ref}

    @router.get(prefix)
    async def list_assets(user_id: str) -> dict[str, list[AssetRef]]:
        return {"assets": store.list(user_id)}

    @router.post(f"{prefix}/downloads")
    async def create_download(request: DownloadRequest) -> dict[str, str]:
        ref = AssetRef(
            asset_id=request.asset_id, user_id=request.user_id, filename=request.filename
        )
        try:
            url = store.presign_download(ref)
        except HarnessError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"download_url": url}

    return router
