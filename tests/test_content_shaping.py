"""Tests for input shaping (weave.application.content)."""

from __future__ import annotations

import pytest

from weave.application.content import (
    build_content,
    document_block,
    file_block,
    image_block,
    text_block,
    video_block,
)
from weave.application.errors import HarnessError


def test_text_block():
    assert text_block("hi") == {"text": "hi"}


def test_image_block():
    assert image_block(b"\x89PNG", format="png") == {
        "image": {"format": "png", "source": {"bytes": b"\x89PNG"}}
    }


def test_video_block():
    assert video_block(b"\x00", format="mp4") == {
        "video": {"format": "mp4", "source": {"bytes": b"\x00"}}
    }


def test_document_block_defaults_and_sanitizes_name():
    assert document_block(b"%PDF", format="pdf") == {
        "document": {"format": "pdf", "name": "document", "source": {"bytes": b"%PDF"}}
    }
    # Dots and other disallowed chars collapse to single spaces.
    block = document_block(b"%PDF", format="pdf", name="Q3 report!!.pdf")
    assert block["document"]["name"] == "Q3 report pdf"


def test_block_builders_reject_unknown_format():
    with pytest.raises(HarnessError, match="unsupported image format"):
        image_block(b"x", format="tiff")  # type: ignore[arg-type]
    with pytest.raises(HarnessError, match="unsupported document format"):
        document_block(b"x", format="rtf")  # type: ignore[arg-type]


def test_file_block_infers_kind_and_canonical_format():
    assert file_block("photo.JPG", b"x")["image"]["format"] == "jpeg"
    assert file_block("page.htm", b"x")["document"]["format"] == "html"
    assert file_block("clip.3gp", b"x")["video"]["format"] == "three_gp"


def test_file_block_document_name_defaults_to_stem():
    block = file_block("Q3 report.pdf", b"%PDF")
    assert block["document"]["format"] == "pdf"
    assert block["document"]["name"] == "Q3 report"
    # An explicit name still wins.
    assert file_block("r.pdf", b"%PDF", name="custom")["document"]["name"] == "custom"


def test_file_block_unknown_extension_raises():
    with pytest.raises(HarnessError, match="unknown extension"):
        file_block("archive.zip", b"x")
    with pytest.raises(HarnessError, match="unknown extension"):
        file_block("noext", b"x")


def test_build_content_orders_text_then_attachments():
    content = build_content(
        "summarize these",
        attachments=[file_block("r.pdf", b"%PDF"), image_block(b"x", format="png")],
    )
    assert [next(iter(b)) for b in content] == ["text", "document", "image"]
    assert content[0] == {"text": "summarize these"}


def test_build_content_text_only_and_attachments_only():
    assert build_content("hi") == [{"text": "hi"}]
    only = build_content(attachments=[image_block(b"x", format="png")])
    assert [next(iter(b)) for b in only] == ["image"]


def test_build_content_empty_raises():
    with pytest.raises(HarnessError, match="needs text and/or attachments"):
        build_content()
