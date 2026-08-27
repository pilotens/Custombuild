from __future__ import annotations

import pytest
from app.security import validate_upload


@pytest.mark.parametrize(
    ("content_type", "filename", "content"),
    (
        ("image/png", "reference.PNG", b"\x89PNG\r\n\x1a\ncontent"),
        ("image/jpeg", "reference.jpg", b"\xff\xd8\xffcontent"),
        ("image/jpeg", "reference.JPEG", b"\xff\xd8\xffcontent"),
        ("image/webp", "reference.webp", b"RIFF\x10\x00\x00\x00WEBPVP8 content"),
        ("application/pdf", "drawing.pdf", b"%PDF-1.7\ncontent"),
        ("image/vnd.dxf", "drawing.dxf", b"0\nSECTION\ncontent"),
        ("application/dxf", "drawing.DXF", b"  0\r\nSECTION\r\ncontent"),
    ),
)
def test_supported_uploads_require_matching_signature_mime_and_extension(
    content_type: str,
    filename: str,
    content: bytes,
) -> None:
    validate_upload(content, content_type, filename)


@pytest.mark.parametrize(
    "filename",
    (
        "",
        ".hidden.pdf",
        "../drawing.pdf",
        "folder/drawing.pdf",
        "folder\\drawing.pdf",
        " drawing.pdf",
        "drawing.pdf ",
        "drawing\x00.pdf",
        "drawing\n.pdf",
        "a" * 252 + ".pdf",
    ),
)
def test_unsafe_upload_filenames_are_rejected(filename: str) -> None:
    with pytest.raises(ValueError, match="Unsafe filename"):
        validate_upload(b"%PDF-1.7", "application/pdf", filename)


@pytest.mark.parametrize(
    ("content_type", "filename", "content", "message"),
    (
        ("application/zip", "drawing.zip", b"PK\x03\x04", "signature"),
        ("image/png", "drawing.png", b"%PDF-1.7", "signature"),
        ("application/pdf", "drawing.png", b"%PDF-1.7", "extension"),
        ("image/jpeg", "drawing.gif", b"\xff\xd8\xff", "extension"),
        ("image/webp", "drawing.webp", b"RIFF\x10\x00\x00\x00NOPEcontent", "signature"),
    ),
)
def test_mime_signature_or_extension_mismatch_is_rejected(
    content_type: str,
    filename: str,
    content: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_upload(content, content_type, filename)


def test_upload_size_is_bounded_before_signature_processing() -> None:
    with pytest.raises(ValueError, match="20 MiB"):
        validate_upload(b"%PDF-" + b"0" * (20 * 1024 * 1024), "application/pdf", "large.pdf")
