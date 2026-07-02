---
title: Input shaping
description: Turn bytes into the Strands content blocks a turn expects.
---

# Input shaping

A turn's `input` is a string **or** a list of Strands content blocks (multimodal).
Input shaping maps raw bytes to those blocks so you don't hand-build Bedrock's nested
dicts.

!!! note "Bytes in, blocks out"
    Scope is deliberately *bytes → blocks*. Fetching from S3 (or anywhere) is the
    **backend's** job, with its own credentials — pull the bytes with your store, then
    shape them. (The [asset store](assets.md) is the Weave-provided way to do that.)

## Assemble a turn

```python
from weave import build_content, file_block, run_agent

content = build_content(
    "Summarise these documents.",
    attachments=[
        file_block("Q3.pdf", pdf_bytes),
        file_block("chart.png", png_bytes),
    ],
)
reply = await run_agent("assistant", content, session)
```

`build_content(text=None, *, attachments=())` puts `text` first (if given), then the
attachments in order, and returns the `list[ContentBlock]`. Empty input raises
`HarnessError`.

## Infer from a filename (`file_block`)

```python
file_block("photo.JPG", data)   # → image  (jpeg)
file_block("page.htm",  data)   # → document (html)
file_block("clip.3gp",  data)   # → video (three_gp)
```

`file_block(filename, data, *, name=None)` infers the kind and canonical format from
the extension (handling aliases like `jpg→jpeg`, `htm→html`, `3gp→three_gp`). For
documents the block `name` defaults to the filename stem. An unknown extension raises
`HarnessError`.

## Typed block builders

When you want explicit control over the format:

```python
from weave import text_block, image_block, document_block, video_block

text_block("hello")
image_block(png_bytes, format="png")                       # png/jpeg/gif/webp
document_block(pdf_bytes, format="pdf", name="Q3 report")  # pdf/csv/doc/docx/xls/xlsx/html/txt/md
video_block(mp4_bytes, format="mp4")                       # flv/mkv/mov/mpeg/mpg/mp4/three_gp/webm/wmv
```

- An unsupported format raises `HarnessError`, naming the valid set.
- Document `name` is sanitised to Bedrock's allowed character set (alphanumerics,
  spaces, hyphens, parens, brackets) and defaults to `"document"`.

## With the asset store

A stored asset resolves to a block in one line — the seam input shaping leaves open:

```python
from weave import build_content, file_block

ref = store.list("u1")[0]
content = build_content(
    "Describe this.",
    attachments=[file_block(ref.filename, store.get(ref))],
)
```

See [Asset store](assets.md).
