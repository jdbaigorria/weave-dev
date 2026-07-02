"""weave.application.content — Input shaping: bytes → Strands content blocks.

Where ``AgentComplexRequest`` lands. The harness accepts a raw payload (bytes +
format, or a named file) and maps it to the ``list[ContentBlock]`` that
``run_agent`` / ``stream_agent`` pass to the agent — so a caller with a PDF or an
image doesn't hand-build Bedrock's nested dicts.

Scope is deliberately *bytes in, blocks out*: fetching from S3 (or anywhere) is the
**backend's** job, with its own credentials — Weave never reaches for storage here.
The format tables are reimplemented (not imported from Strands) on purpose, for the
same independence reason the reply projection is: a handful of literals duplicated
to keep the harness decoupled from Strands' type module layout.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

from weave.application.errors import HarnessError

if TYPE_CHECKING:
    from strands.types.content import ContentBlock
    from strands.types.media import DocumentFormat, ImageFormat, VideoFormat

# Strands/Bedrock-supported formats per media kind (mirror of strands.types.media).
_IMAGE_FORMATS = frozenset({"png", "jpeg", "gif", "webp"})
_DOCUMENT_FORMATS = frozenset({"pdf", "csv", "doc", "docx", "xls", "xlsx", "html", "txt", "md"})
_VIDEO_FORMATS = frozenset({"flv", "mkv", "mov", "mpeg", "mpg", "mp4", "three_gp", "webm", "wmv"})

# File-extension → (kind, canonical format). Covers the common aliases (jpg, htm, 3gp).
_EXTENSIONS: dict[str, tuple[str, str]] = {
    "png": ("image", "png"),
    "jpg": ("image", "jpeg"),
    "jpeg": ("image", "jpeg"),
    "gif": ("image", "gif"),
    "webp": ("image", "webp"),
    "pdf": ("document", "pdf"),
    "csv": ("document", "csv"),
    "doc": ("document", "doc"),
    "docx": ("document", "docx"),
    "xls": ("document", "xls"),
    "xlsx": ("document", "xlsx"),
    "html": ("document", "html"),
    "htm": ("document", "html"),
    "txt": ("document", "txt"),
    "md": ("document", "md"),
    "flv": ("video", "flv"),
    "mkv": ("video", "mkv"),
    "mov": ("video", "mov"),
    "mpeg": ("video", "mpeg"),
    "mpg": ("video", "mpg"),
    "mp4": ("video", "mp4"),
    "3gp": ("video", "three_gp"),
    "webm": ("video", "webm"),
    "wmv": ("video", "wmv"),
}


def text_block(text: str) -> ContentBlock:
    """A plain text content block."""
    return cast("ContentBlock", {"text": text})


def image_block(data: bytes, *, format: ImageFormat) -> ContentBlock:
    """An image content block from raw ``data`` (``format``: png/jpeg/gif/webp)."""
    _require(format, _IMAGE_FORMATS, "image")
    return cast("ContentBlock", {"image": {"format": format, "source": {"bytes": data}}})


def document_block(data: bytes, *, format: DocumentFormat, name: str | None = None) -> ContentBlock:
    """A document content block (``format``: pdf/csv/doc/docx/xls/xlsx/html/txt/md).

    Bedrock requires a document ``name`` and restricts its characters, so it is
    sanitized (and defaulted to ``"document"`` when omitted).
    """
    _require(format, _DOCUMENT_FORMATS, "document")
    return cast(
        "ContentBlock",
        {"document": {"format": format, "name": _safe_name(name), "source": {"bytes": data}}},
    )


def video_block(data: bytes, *, format: VideoFormat) -> ContentBlock:
    """A video content block from raw ``data`` (``format``: mp4/mov/webm/…)."""
    _require(format, _VIDEO_FORMATS, "video")
    return cast("ContentBlock", {"video": {"format": format, "source": {"bytes": data}}})


def file_block(filename: str, data: bytes, *, name: str | None = None) -> ContentBlock:
    """Build the right block for an uploaded file, inferring kind+format from its extension.

    ``photo.jpg`` → image (jpeg); ``report.pdf`` → document; ``clip.3gp`` → video
    (three_gp). For documents the block ``name`` defaults to the filename stem.
    Raises :class:`~weave.application.errors.HarnessError` for an unknown extension.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mapping = _EXTENSIONS.get(ext)
    if mapping is None:
        raise HarnessError(
            f"can't infer a content block for {filename!r}: unknown extension "
            f"{ext or '(none)'!r}. Use the typed block builders for an explicit format."
        )
    kind, fmt = mapping
    if kind == "image":
        return image_block(data, format=cast("ImageFormat", fmt))
    if kind == "video":
        return video_block(data, format=cast("VideoFormat", fmt))
    stem = filename.rsplit(".", 1)[0]
    return document_block(data, format=cast("DocumentFormat", fmt), name=name or stem)


def build_content(
    text: str | None = None, *, attachments: Iterable[ContentBlock] = ()
) -> list[ContentBlock]:
    """Assemble the ``list[ContentBlock]`` to pass as a turn's ``input``.

    ``text`` (if given) leads, followed by ``attachments`` in order — build those
    with :func:`file_block` or the typed block builders. Raises
    :class:`~weave.application.errors.HarnessError` if both are empty.
    """
    blocks: list[ContentBlock] = []
    if text is not None:
        blocks.append(text_block(text))
    blocks.extend(attachments)
    if not blocks:
        raise HarnessError("build_content needs text and/or attachments — both were empty.")
    return blocks


# Bedrock document names allow alphanumerics, single spaces, hyphens, parens, brackets.
_NAME_DISALLOWED = re.compile(r"[^A-Za-z0-9 \-()\[\]]")
_NAME_SPACES = re.compile(r"\s+")


def _safe_name(name: str | None) -> str:
    """Coerce ``name`` into a Bedrock-legal document name (fallback ``"document"``)."""
    cleaned = _NAME_SPACES.sub(" ", _NAME_DISALLOWED.sub(" ", name or "")).strip()
    return cleaned or "document"


def _require(fmt: str, allowed: frozenset[str], kind: str) -> None:
    """Reject a format the model won't accept, naming the valid set."""
    if fmt not in allowed:
        raise HarnessError(f"unsupported {kind} format {fmt!r}; expected one of {sorted(allowed)}.")
