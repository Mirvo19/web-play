"""Single canonical helper for every upload write path.

All upload endpoints MUST route through here so that:
* filenames are sanitized (no path traversal, no absolute-path overwrite),
* every write stays jailed inside the configured upload folder,
* running byte counts are enforced even when Content-Length is missing
  or spoofed.
"""

from __future__ import annotations

import os

from werkzeug.utils import secure_filename

_CHUNK_SIZE = 4 * 1024 * 1024


def sanitized_filename(name: str | None, default: str = "source.mp4") -> str:
    """Return a safe basename for an uploaded file name."""
    raw = (name or "").strip().replace("\\", "/")
    # Drop any directory components first (defence in depth — secure_filename
    # already strips separators, but an explicit basename makes intent clear).
    raw = os.path.basename(raw)
    safe = secure_filename(raw)
    if not safe or safe in {".", ".."}:
        return default
    return safe


def jailed_path(base_dir: str, *parts: str) -> str:
    """Join *parts* onto *base_dir* and refuse path escapes.

    Raises:
        ValueError: if the canonical path is outside *base_dir*.
    """
    base = os.path.abspath(base_dir)
    full = os.path.abspath(os.path.join(base, *[p for p in parts if p]))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError(f"Refusing path outside upload directory: {full!r}")
    return full


def ensure_job_dir(upload_folder: str, job_id: str) -> str:
    """Create (if needed) and return the jailed per-job work directory."""
    safe_job_id = sanitized_filename(job_id, default="job")
    work_dir = jailed_path(upload_folder, safe_job_id)
    os.makedirs(work_dir, exist_ok=True)
    return work_dir


class UploadTooLarge(Exception):
    """Raised when an upload exceeds the configured byte budget."""


def stream_to_file(source_stream, dest_path: str, max_bytes: int,
                   chunk_size: int = _CHUNK_SIZE, on_progress=None) -> int:
    """Copy *source_stream* to *dest_path* in chunks, enforcing *max_bytes*.

    The limit is enforced on ACTUAL bytes written, so a missing or spoofed
    Content-Length cannot bypass it. Raises :class:`UploadTooLarge`
    mid-stream (leaving a partial file the caller must clean up).
    *on_progress*, when given, is called with the running byte count after
    each chunk (use it for throttled DB progress updates).
    Returns the total bytes written.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    bytes_written = 0
    with open(dest_path, "wb", buffering=8 * 1024 * 1024) as out_f:
        while True:
            chunk = source_stream.read(chunk_size)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            bytes_written += len(chunk)
            if bytes_written > max_bytes:
                raise UploadTooLarge(
                    f"Upload exceeds maximum allowed size of {max_bytes} bytes."
                )
            out_f.write(chunk)
            if on_progress is not None:
                on_progress(bytes_written)
    return bytes_written
