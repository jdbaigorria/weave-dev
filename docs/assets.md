---
title: Asset store
description: User uploads with a swappable backend and presigned URLs.
---

# Asset store

A user's uploads land in a store and are referenced later. The store is **swappable**
(S3, local FS, …) and user-scoped. Like the other capabilities, Weave defines the port
and ships reference adapters but holds **no default store** — the backend provides the
bucket/dir.

## Two layers

- **`AssetStore`** — the core, works on any backend (including local FS):
  `put` / `get` / `list` / `delete`.
- **`PresignedAssetStore`** — an optional capability that adds **direct UI↔bucket**
  URLs (`presign_upload` / `presign_download`) — the serverless-friendly path that
  keeps large payloads out of the app. S3 supports it; the local store doesn't.

## Store and reference

```python
from weave import S3AssetStore           # or LocalAssetStore() for dev

store = S3AssetStore("my-bucket")

ref = store.put("u1", "report.pdf", pdf_bytes, content_type="application/pdf")
data = store.get(ref)
for r in store.list("u1"):               # most-recent first
    print(r.filename, r.size)
store.delete(ref)
```

### `AssetRef`

```python
@dataclass(frozen=True)
class AssetRef:
    asset_id: str
    user_id: str
    filename: str
    content_type: str | None = None
    size: int | None = None
    created_at: str | None = None
```

`asset_id` + `user_id` + `filename` are enough for the store to locate the blob. The
UI keeps the ref and sends it back to reference the asset.

## Presigned uploads (S3)

The serverless-friendly flow — the browser PUTs straight to the bucket:

```python
url, ref = store.presign_upload("u1", "photo.png", content_type="image/png")
# → UI does: PUT {url}  with the bytes; then keeps `ref`

download_url = store.presign_download(ref)   # time-limited GET URL
```

`S3AssetStore` keys objects as `<prefix>/<user_id>/<asset_id>/<filename>`, so `list`
rebuilds refs straight from object keys (no per-object HEAD). The boto3 client is
injectable for tests; otherwise it defaults to `boto3.client("s3")`. Install with
`weave[aws]`.

## Mount the asset router (FastAPI)

```python
from fastapi import FastAPI
from weave import S3AssetStore, asset_router

app = FastAPI()
app.include_router(asset_router(S3AssetStore("my-bucket")))
# → POST /assets/uploads    {user_id, filename, content_type?} → {upload_url, ref}
# → GET  /assets?user_id=…                                     → {assets: [AssetRef]}
# → POST /assets/downloads  {user_id, asset_id, filename}      → {download_url}
```

The router requires a `PresignedAssetStore` (else `HarnessError`); FastAPI is imported
lazily (`weave[fastapi]`). Use `asset_router(store, prefix="/files")` to mount the
same routes under a different prefix. Deleting is a direct `store.delete(ref)` call
(no route, to keep the surface small).

!!! note "Identity is the backend's job"
    `user_id` arrives in the request — enforce auth at the mount point (same caveat as
    the [chat router](chat-channel.md)).

## Reference an asset in a chat

Assets compose with [input shaping](content.md): fetch the bytes, shape a block, run.

```python
from weave import build_content, file_block

ref = store.list("u1")[0]
content = build_content(
    "Summarise this.",
    attachments=[file_block(ref.filename, store.get(ref))],
)
reply = await run_agent("assistant", content, session)
```

## Adapters

| Adapter | Capability | Extra |
| --- | --- | --- |
| `LocalAssetStore` | core (`put/get/list/delete`) | — |
| `S3AssetStore` | presigned | `weave[aws]` |

A GCS or other backend implements the same port (a future addition).
