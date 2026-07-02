"""weave.adapters.driven.s3_assets — AssetStore over Amazon S3 (``weave[aws]``).

Implements the presigned capability: ``presign_upload`` / ``presign_download`` hand
the UI a direct PUT/GET URL to the bucket, so large payloads never pass through the
app. Key layout ``<prefix>/<user_id>/<asset_id>/<filename>`` lets ``list`` rebuild
refs straight from object keys (no per-object HEAD). The boto3 client is injectable
for tests; otherwise it defaults to ``boto3.client("s3")``.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from weave.application.errors import HarnessError
from weave.ports.assets import AssetRef


class S3AssetStore:
    """``PresignedAssetStore`` backed by an S3 bucket."""

    def __init__(self, bucket: str, *, client: Any = None, prefix: str = "assets") -> None:
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - depends on the optional extra
                raise HarnessError(
                    "S3AssetStore needs boto3; install the extra: weave[aws]."
                ) from exc
            client = boto3.client("s3")
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def put(
        self, user_id: str, filename: str, data: bytes, *, content_type: str | None = None
    ) -> AssetRef:
        asset_id = uuid.uuid4().hex
        extra = {"ContentType": content_type} if content_type else {}
        self._client.put_object(
            Bucket=self._bucket, Key=self._key(user_id, asset_id, filename), Body=data, **extra
        )
        return AssetRef(
            asset_id=asset_id,
            user_id=user_id,
            filename=filename,
            content_type=content_type,
            size=len(data),
        )

    def get(self, ref: AssetRef) -> bytes:
        try:
            resp = self._client.get_object(
                Bucket=self._bucket, Key=self._key(ref.user_id, ref.asset_id, ref.filename)
            )
        except Exception as exc:  # noqa: BLE001 — any client/404 error → typed harness error
            raise HarnessError(
                f"asset {ref.asset_id!r} not found for user {ref.user_id!r}."
            ) from exc
        return cast("bytes", resp["Body"].read())

    def list(self, user_id: str) -> list[AssetRef]:
        prefix = self._user_prefix(user_id)
        resp = self._client.list_objects_v2(Bucket=self._bucket, Prefix=prefix)
        refs: list[AssetRef] = []
        for obj in resp.get("Contents", []) or []:
            remainder = obj["Key"][len(prefix) :]
            asset_id, _, filename = remainder.partition("/")
            if not filename:
                continue
            last_modified = obj.get("LastModified")
            refs.append(
                AssetRef(
                    asset_id=asset_id,
                    user_id=user_id,
                    filename=filename,
                    size=obj.get("Size"),
                    created_at=last_modified.isoformat() if last_modified else None,
                )
            )
        refs.sort(key=lambda ref: ref.created_at or "", reverse=True)
        return refs

    def delete(self, ref: AssetRef) -> None:
        self._client.delete_object(
            Bucket=self._bucket, Key=self._key(ref.user_id, ref.asset_id, ref.filename)
        )

    def presign_upload(
        self,
        user_id: str,
        filename: str,
        *,
        content_type: str | None = None,
        expires_in: int = 3600,
    ) -> tuple[str, AssetRef]:
        asset_id = uuid.uuid4().hex
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": self._key(user_id, asset_id, filename),
        }
        if content_type:
            params["ContentType"] = content_type
        url = self._client.generate_presigned_url("put_object", Params=params, ExpiresIn=expires_in)
        ref = AssetRef(
            asset_id=asset_id, user_id=user_id, filename=filename, content_type=content_type
        )
        return cast("str", url), ref

    def presign_download(self, ref: AssetRef, *, expires_in: int = 3600) -> str:
        url = self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": self._key(ref.user_id, ref.asset_id, ref.filename),
            },
            ExpiresIn=expires_in,
        )
        return cast("str", url)

    def _key(self, user_id: str, asset_id: str, filename: str) -> str:
        return f"{self._prefix}/{user_id}/{asset_id}/{filename}"

    def _user_prefix(self, user_id: str) -> str:
        return f"{self._prefix}/{user_id}/"
