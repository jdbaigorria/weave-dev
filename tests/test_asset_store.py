"""Tests for asset storage (port + local & S3 adapters + presign router)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest

from weave.adapters.driven.local_assets import LocalAssetStore
from weave.adapters.driven.s3_assets import S3AssetStore
from weave.adapters.driving.assets import asset_router
from weave.application.errors import HarnessError
from weave.ports.assets import AssetRef, AssetStore, PresignedAssetStore


def _counter_clock():
    counter = itertools.count()
    return lambda: f"{next(counter):020d}"


# --- LocalAssetStore ------------------------------------------------------------


def test_local_put_get_roundtrip(tmp_path):
    store = LocalAssetStore(str(tmp_path))
    ref = store.put("u1", "report.pdf", b"%PDF-1.7", content_type="application/pdf")

    assert ref.user_id == "u1"
    assert ref.filename == "report.pdf"
    assert ref.content_type == "application/pdf"
    assert ref.size == 8
    assert store.get(ref) == b"%PDF-1.7"


def test_local_list_orders_recent_first_and_isolates_users(tmp_path):
    store = LocalAssetStore(str(tmp_path), clock=_counter_clock())
    a = store.put("u1", "a.txt", b"a")
    b = store.put("u1", "b.txt", b"b")
    store.put("u2", "c.txt", b"c")

    assert [r.asset_id for r in store.list("u1")] == [b.asset_id, a.asset_id]
    assert [r.filename for r in store.list("u2")] == ["c.txt"]


def test_local_delete_and_missing_get(tmp_path):
    store = LocalAssetStore(str(tmp_path))
    ref = store.put("u1", "a.txt", b"a")
    store.delete(ref)

    assert store.list("u1") == []
    with pytest.raises(HarnessError, match="not found"):
        store.get(ref)


def test_local_store_satisfies_core_but_not_presigned(tmp_path):
    store = LocalAssetStore(str(tmp_path))
    assert isinstance(store, AssetStore)
    assert not isinstance(store, PresignedAssetStore)  # local FS can't presign


# --- S3AssetStore (fake client) -------------------------------------------------


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str | None]] = {}
        self.presigned: list[tuple[str, dict[str, Any], int]] = []

    def put_object(self, *, Bucket, Key, Body, **kw) -> None:
        self.objects[Key] = (Body, kw.get("ContentType"))

    def get_object(self, *, Bucket, Key) -> dict[str, Any]:
        body, _ = self.objects[Key]  # KeyError → adapter wraps in HarnessError
        return {"Body": _Body(body)}

    def list_objects_v2(self, *, Bucket, Prefix) -> dict[str, Any]:
        now = datetime.now(UTC)
        contents = [
            {"Key": k, "Size": len(v[0]), "LastModified": now}
            for k, v in self.objects.items()
            if k.startswith(Prefix)
        ]
        return {"Contents": contents} if contents else {}

    def delete_object(self, *, Bucket, Key) -> None:
        self.objects.pop(Key, None)

    def generate_presigned_url(self, op, *, Params, ExpiresIn) -> str:
        self.presigned.append((op, Params, ExpiresIn))
        return f"https://signed.example/{op}/{Params['Key']}"


def _s3() -> tuple[S3AssetStore, _FakeS3]:
    client = _FakeS3()
    return S3AssetStore("my-bucket", client=client, prefix="assets"), client


def test_s3_put_uses_user_scoped_key_and_get_roundtrips():
    store, client = _s3()
    ref = store.put("u1", "doc.pdf", b"data", content_type="application/pdf")

    key = f"assets/u1/{ref.asset_id}/doc.pdf"
    assert client.objects[key] == (b"data", "application/pdf")
    assert store.get(ref) == b"data"


def test_s3_list_rebuilds_refs_from_keys():
    store, _ = _s3()
    r1 = store.put("u1", "a.txt", b"a")
    store.put("u2", "b.txt", b"b")  # other user, must not appear

    listed = store.list("u1")
    assert [r.filename for r in listed] == ["a.txt"]
    assert listed[0].asset_id == r1.asset_id
    assert listed[0].size == 1


def test_s3_get_missing_raises_harness_error():
    store, _ = _s3()
    ghost = AssetRef(asset_id="nope", user_id="u1", filename="x.txt")
    with pytest.raises(HarnessError, match="not found"):
        store.get(ghost)


def test_s3_presign_upload_and_download_delegate_to_client():
    store, client = _s3()
    url, ref = store.presign_upload("u1", "pic.png", content_type="image/png")

    assert url.startswith("https://signed.example/put_object/")
    op, params, _ = client.presigned[0]
    assert op == "put_object"
    assert params["Key"] == f"assets/u1/{ref.asset_id}/pic.png"
    assert params["ContentType"] == "image/png"

    dl = store.presign_download(ref)
    assert dl.startswith("https://signed.example/get_object/")
    assert client.presigned[1][0] == "get_object"


def test_s3_store_is_presigned():
    store, _ = _s3()
    assert isinstance(store, PresignedAssetStore)


# --- asset_router (presign + list over HTTP) ------------------------------------


def test_asset_router_presign_upload_list_download():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    store, _ = _s3()
    app = FastAPI()
    app.include_router(asset_router(store))
    client = TestClient(app)

    up = client.post("/assets/uploads", json={"user_id": "u1", "filename": "r.pdf"})
    assert up.status_code == 200
    body = up.json()
    assert body["upload_url"].startswith("https://signed.example/put_object/")
    asset_id = body["ref"]["asset_id"]

    # The presign reserved the ref but no object was uploaded to the fake bucket.
    listed = client.get("/assets", params={"user_id": "u1"})
    assert listed.status_code == 200
    assert listed.json()["assets"] == []

    down = client.post(
        "/assets/downloads", json={"user_id": "u1", "asset_id": asset_id, "filename": "r.pdf"}
    )
    assert down.status_code == 200
    assert down.json()["download_url"].startswith("https://signed.example/get_object/")


def test_asset_router_rejects_non_presigned_store(tmp_path):
    with pytest.raises(HarnessError, match="needs a PresignedAssetStore"):
        asset_router(LocalAssetStore(str(tmp_path)))  # type: ignore[arg-type]
