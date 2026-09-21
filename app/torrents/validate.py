"""Downloaded-file validation gate for torrent handoff.

A file reaches the transcode pipeline ONLY if:
  1. its extension is in the pipeline-processable media allowlist,
  2. its magic bytes match that extension family (no masquerading),
  3. its on-disk size equals the size the torrent metadata promised.

Pure functions, no I/O beyond reading the file head.
"""

from __future__ import annotations

import os

# Extension -> acceptable magic-byte signatures (offset, bytes).
# Covers containers/codecs ffmpeg in this pipeline can actually process.
_SIGNATURES = {
    ".mp4": [(4, b"ftyp")],
    ".m4v": [(4, b"ftyp")],
    ".m4a": [(4, b"ftyp")],
    ".mov": [(4, b"ftyp")],
    ".mkv": [(0, b"\x1a\x45\xdf\xa3")],
    ".webm": [(0, b"\x1a\x45\xdf\xa3")],
    ".avi": [(0, b"RIFF")],
    ".wmv": [(0, b"\x30\x26\xb2\x75\x8e\x66\xcf\x11")],
    ".mpg": [(0, b"\x00\x00\x01\xba"), (0, b"\x00\x00\x01\xb3")],
    ".mpeg": [(0, b"\x00\x00\x01\xba"), (0, b"\x00\x00\x01\xb3")],
    ".ts": [(0, b"\x47")],
    ".m2ts": [(0, b"\x47")],
    ".flv": [(0, b"FLV")],
    ".ogv": [(0, b"OggS")],
    ".ogg": [(0, b"OggS")],
    ".mp3": [(0, b"ID3"), (0, b"\xff\xfb"), (0, b"\xff\xf3"), (0, b"\xff\xf2")],
    ".flac": [(0, b"fLaC")],
    ".wav": [(0, b"RIFF")],
    ".aac": [(0, b"\xff\xf1"), (0, b"\xff\xf9")],
}

ALLOWED_EXTENSIONS = frozenset(_SIGNATURES)

HEAD_READ_BYTES = 64
MIN_MEDIA_BYTES = 1024  # anything smaller cannot be a real media file


class ValidationError(ValueError):
    pass


def extension_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def validate_media_file(path: str, expected_size: int, display_name: str = "") -> dict:
    """Validate a downloaded file. Returns {extension, size} or raises."""
    label = display_name or os.path.basename(path)
    ext = extension_of(label)
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"{label!r}: extension {ext or '(none)'} is not a processable media type "
            f"(allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))})"
        )
    try:
        actual_size = os.path.getsize(path)
    except OSError as e:
        raise ValidationError(f"{label!r}: cannot stat downloaded file: {e}")
    if expected_size is not None and actual_size != expected_size:
        raise ValidationError(
            f"{label!r}: size mismatch (metadata promised {expected_size} bytes, "
            f"got {actual_size}) — refusing truncated or substituted file"
        )
    if actual_size < MIN_MEDIA_BYTES:
        raise ValidationError(f"{label!r}: file too small to be media ({actual_size} bytes)")
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_READ_BYTES)
    except OSError as e:
        raise ValidationError(f"{label!r}: cannot read downloaded file: {e}")
    if not any(head[off:off + len(sig)] == sig for off, sig in _SIGNATURES[ext]):
        raise ValidationError(
            f"{label!r}: content does not match its {ext} extension (magic-byte check failed)"
        )
    return {"extension": ext, "size": actual_size}
