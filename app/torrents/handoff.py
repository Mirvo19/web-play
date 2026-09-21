"""Handoff: validated torrent files -> the EXISTING ingestion pipeline.

Each validated file becomes a normal Video + transcode_and_upload Job via
the exact same steps (and limits) as a direct upload: sanitized filename,
jailed job-scoped work dir, running byte-count cap, queued state. No
shortcut, no parallel pipeline — after this returns, the files are
indistinguishable from uploaded ones.
"""

from __future__ import annotations

import logging
import os

from app.models import db, Video, Job
from app.torrents.engine import wipe_quarantine
from app.utils.uploads import (
    UploadTooLarge,
    ensure_job_dir,
    jailed_path,
    sanitized_filename,
    stream_to_file,
)

_log = logging.getLogger(__name__)


def _max_upload_bytes(app) -> int:
    try:
        return int(float(app.config.get("MAX_UPLOAD_SIZE_GB", 4)) * 1024 ** 3)
    except (TypeError, ValueError):
        return 4 * 1024 ** 3


def _title_for(filename: str, index: int) -> str:
    base = os.path.splitext(filename)[0].strip() or f"torrent file {index}"
    return base[:200]


def _drop_failed_pair(video_id: str, job_id: str) -> None:
    """Remove a half-created Video+Job pair (mirrors upload failure cleanup)."""
    try:
        job = db.session.get(Job, job_id)
        if job is not None:
            db.session.delete(job)
        video = db.session.get(Video, video_id)
        if video is not None:
            db.session.delete(video)
        db.session.commit()
    except Exception:
        db.session.rollback()


def hand_off(app, trow, landed: list, cdn_account_id: str, work_dir: str) -> list:
    """Create Video+Job rows for each validated file. Returns handed list.

    landed: [(index, path, size, sanitized_name)] — quarantine paths already
    magic-byte + size validated. A per-file failure (e.g. over the pipeline
    upload cap) drops just that pair and continues; raises RuntimeError only
    when nothing could be handed off. The quarantine is wiped in all cases.
    """
    max_bytes = _max_upload_bytes(app)
    upload_folder = app.config.get("UPLOAD_FOLDER", "/tmp/video-processing")
    handed = []
    failures = []
    try:
        for index, path, size, name in landed:
            # Sanitize again at the last gate: only a basename ever reaches
            # the pipeline, even if a caller passes something raw.
            name = sanitized_filename(name, default=f"file_{index}.mp4")
            video = Video(
                title=_title_for(name, index),
                description=f"Imported from torrent: {(trow.display_name or '')[:200]}",
                original_filename=name,
                cdn_account_id=cdn_account_id,
                status="processing",
            )
            db.session.add(video)
            db.session.commit()
            job = Job(
                video_id=video.id,
                job_type="transcode_and_upload",
                status="receiving",
                stage="receiving_upload",
                current_step="Receiving torrent file",
                current_message="Copying validated file into pipeline",
                progress=0.0,
                bytes_total=size,
            )
            db.session.add(job)
            db.session.commit()
            try:
                dest = jailed_path(ensure_job_dir(upload_folder, job.id), name)
                with open(path, "rb") as src:
                    written = stream_to_file(src, dest, max_bytes)
                video.original_size = written
                job.status = "queued"
                job.stage = "queued"
                job.current_step = "Queued for processing"
                job.current_message = "Torrent file handed to transcode pipeline"
                job.progress = 100.0
                job.bytes_received = written
                job.bytes_total = written
                db.session.commit()
                handed.append({"video_id": video.id, "job_id": job.id, "filename": name})
            except (OSError, UploadTooLarge, ValueError) as e:
                _log.warning("Handoff copy failed for %s: %s", name, e)
                failures.append(f"{name}: {e}")
                _drop_failed_pair(video.id, job.id)
        if not handed:
            raise RuntimeError(
                "handoff failed: " + ("; ".join(failures) if failures else "no files"))
        if failures:
            _log.warning("Partial torrent handoff (%d ok, %d failed): %s",
                         len(handed), len(failures), "; ".join(failures))
        return handed
    finally:
        wipe_quarantine(work_dir)
