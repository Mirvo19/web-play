"""HC CDN Player — In-process job supervisor.

Replaces the old standalone ``worker.py`` process. The supervisor lives in
the SAME process as the Flask web app: it is started from the app factory
(:func:`app.create_app`), polls the ``jobs`` table for queued work, executes
jobs on a bounded :class:`~concurrent.futures.ThreadPoolExecutor`, and shuts
down gracefully on SIGTERM/SIGINT.

Design notes
------------
* **Single process.** No second systemd unit, no cross-process polling.
  Under gunicorn the scheduler runs inside the web worker process(es); the
  atomic claim below keeps concurrent pollers from double-executing a job.
* **Atomic claim.** Jobs are claimed with a compare-and-swap
  ``UPDATE ... WHERE status='queued'`` guarded by ``SELECT ... FOR UPDATE
  SKIP LOCKED`` on PostgreSQL. Exactly one claimer wins even if several
  pollers (threads, gunicorn workers, or a future second node) race.
* **Graceful shutdown.** :meth:`JobSupervisor.stop` sets an event that the
  poll loop honours, cancels queued-but-unstarted futures, signals every
  in-flight FFmpeg controller to terminate, and waits for the executor.
  The pipeline also polls :func:`shutdown_requested` at stage boundaries.
* **Crash recovery.** On start, jobs left ``processing`` by an unclean
  shutdown are re-queued and jobs left ``receiving`` (interrupted upload)
  are marked failed, mirroring the old worker behaviour.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# Process-wide shutdown flag. Set by JobSupervisor.stop(); read by the
# pipeline at stage boundaries so in-flight jobs abort promptly even if
# they are between FFmpeg progress callbacks.
_shutdown_event = threading.Event()


def shutdown_requested() -> bool:
    """Return True once the supervisor has begun graceful shutdown."""
    return _shutdown_event.is_set()


def _parse_max_concurrent(app, default: int = 1) -> int:
    try:
        from app.models import Setting

        raw = Setting.get("max_concurrent_jobs", str(app.config.get("MAX_CONCURRENT_JOBS", default)))
        val = int(raw)
        return min(max(1, val), 8)
    except (ValueError, TypeError):
        return default


def recover_interrupted_jobs(app) -> None:
    """Re-queue jobs orphaned by an unclean shutdown (single-process model)."""
    from app.models import db, Job

    with app.app_context():
        processing = Job.query.filter_by(status="processing").all()
        for job in processing:
            job.status = "queued"
            job.stage = "queued"
            job.current_step = "Re-queued after restart"
            job.current_message = "Job interrupted by restart and re-queued"
            job.cancel_requested = False
            log.warning("Re-queued interrupted job %s", job.id)

        receiving = Job.query.filter_by(status="receiving").all()
        for job in receiving:
            job.status = "failed"
            job.stage = "failed"
            job.current_step = "Upload interrupted"
            job.current_message = "Upload was interrupted by application restart"
            job.completed_at = datetime.now(timezone.utc)
            log.warning("Marked interrupted upload %s as failed", job.id)

        if processing or receiving:
            db.session.commit()


def cleanup_orphan_workspaces(app) -> None:
    """Remove job-scoped work dirs with no matching active job."""
    from app.models import db, Job

    upload_folder = app.config.get("UPLOAD_FOLDER", "/tmp/video-processing")
    if not os.path.isdir(upload_folder):
        return
    with app.app_context():
        active_ids = {
            j.id
            for j in Job.query.filter(
                Job.status.in_(["queued", "processing", "receiving"])
            ).all()
        }
    for entry in os.listdir(upload_folder):
        path = os.path.join(upload_folder, entry)
        if not os.path.isdir(path) or entry in active_ids:
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            log.info("Cleaned orphan workspace: %s", path)
        except OSError as exc:
            log.warning("Could not clean orphan workspace %s: %s", path, exc)


def claim_next_job(app) -> Optional[Tuple[str, str]]:
    """Atomically claim the oldest queued job.

    Returns ``(job_id, job_type)`` for the winner, or ``None`` when the
    queue is empty, all executor slots are busy, or another claimer won
    the race. Safe to call concurrently from multiple threads/processes.
    """
    from sqlalchemy import func

    from app.models import Job, db

    with app.app_context():
        max_concurrent = _parse_max_concurrent(app)
        running = (
            db.session.query(func.count(Job.id)).filter(Job.status == "processing").scalar() or 0
        )
        if running >= max_concurrent:
            return None

        query = Job.query.filter_by(status="queued").order_by(Job.created_at.asc())
        dialect = db.session.bind.dialect.name if db.session.bind is not None else ""
        if dialect == "postgresql":
            # Row-level lock: concurrent claimers skip rows already locked.
            query = query.with_for_update(skip_locked=True)
        candidate = query.first()
        if candidate is None:
            return None
        job_id, job_type = candidate.id, candidate.job_type

        # Compare-and-swap: only one claimer can flip queued -> processing.
        rows = (
            db.session.query(Job)
            .filter(Job.id == job_id, Job.status == "queued")
            .update(
                {
                    Job.status: "processing",
                    Job.stage: "inspecting_media",
                    Job.started_at: datetime.now(timezone.utc),
                    Job.current_step: "Processing",
                    Job.current_message: "Job picked up by in-process supervisor",
                },
                synchronize_session=False,
            )
        )
        if rows == 1:
            db.session.commit()
            return job_id, job_type
        db.session.rollback()
        return None


def _execute_job(app, job_id: str, job_type: str) -> None:
    """Run one job inside a fresh app context (executor thread)."""
    from app.models import Job, db

    with app.app_context():
        try:
            if job_type == "transcode_and_upload":
                from app.worker.pipeline import execute_video_pipeline

                execute_video_pipeline(job_id)
            elif job_type == "delete_video":
                from app.worker.deleter import execute_video_deletion

                execute_video_deletion(job_id)
            else:
                job = db.session.get(Job, job_id)
                if job:
                    job.status = "failed"
                    job.stage = "failed"
                    job.error_message = f"Unknown job type: {job_type}"
                    job.completed_at = datetime.now(timezone.utc)
                    db.session.commit()
                log.error("Unknown job type %r for job %s", job_type, job_id)
        except Exception:
            log.exception("Unhandled error in job %s", job_id)
            try:
                job = db.session.get(Job, job_id)
                if job and job.status == "processing":
                    job.status = "failed"
                    job.stage = "failed"
                    job.error_message = "Unhandled worker error (see server logs)"
                    job.completed_at = datetime.now(timezone.utc)
                    db.session.commit()
            except Exception:
                log.exception("Could not mark job %s as failed", job_id)


class JobSupervisor:
    """Owns the poll loop + executor for background jobs in-process."""

    def __init__(self, app, poll_interval: float = 2.0, max_workers: Optional[int] = None):
        self._app = app
        self._poll_interval = max(0.5, float(poll_interval))
        self._max_workers = max_workers or _parse_max_concurrent(app)
        self._max_workers = min(max(1, self._max_workers), 8)
        self._stop = threading.Event()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._started = False

    # -- lifecycle ------------------------------------------------------
    def start(self) -> "JobSupervisor":
        with self._lock:
            if self._started:
                return self
            _shutdown_event.clear()
            self._stop.clear()
            log.info("Recovering interrupted jobs from previous run...")
            recover_interrupted_jobs(self._app)
            log.info("Cleaning orphaned workspaces...")
            cleanup_orphan_workspaces(self._app)
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="hc-job",
            )
            self._thread = threading.Thread(
                target=self._poll_loop, name="hc-supervisor", daemon=True
            )
            self._started = True
            self._thread.start()
            log.info(
                "In-process job supervisor started (poll=%.1fs, workers=%d)",
                self._poll_interval,
                self._max_workers,
            )
            return self

    def stop(self, timeout: float = 30.0) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
        log.info("Supervisor stopping: signalling shutdown...")
        _shutdown_event.set()
        self._stop.set()
        # Ask in-flight FFmpeg controllers to terminate promptly.
        try:
            from app.worker import pipeline as _pipeline

            _pipeline.cancel_all_active("server shutdown")
        except Exception:
            log.exception("Error while cancelling active jobs on shutdown")
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("Supervisor poll thread did not exit within %.0fs", timeout)
            self._thread = None
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        log.info("Supervisor stopped cleanly.")

    @property
    def running(self) -> bool:
        with self._lock:
            return self._started

    # -- poll loop ------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                log.exception("Error in supervisor poll loop")
            self._stop.wait(self._poll_interval)

    def _poll_once(self) -> None:
        assert self._executor is not None
        # Count in-flight jobs via the DB so a restart/resubmit stays accurate.
        from app.models import Job, db

        with self._app.app_context():
            from sqlalchemy import func

            running = (
                db.session.query(func.count(Job.id)).filter(Job.status == "processing").scalar()
                or 0
            )
        if running >= self._max_workers:
            return
        claimed = claim_next_job(self._app)
        if claimed is None:
            return
        job_id, job_type = claimed
        log.info("Supervisor claimed job %s (%s)", job_id, job_type)
        self._executor.submit(_execute_job, self._app, job_id, job_type)


# Backwards-compatible helpers (the old tests / worker.py import surface).
def start_job_thread(app, job):
    """Submit a job to a one-off thread (legacy helper kept for tests)."""
    thread = threading.Thread(
        target=_execute_job, args=(app, job.id, job.job_type), daemon=True
    )
    thread.start()
    return thread


def has_active_job_for_video(job, active_jobs) -> bool:
    statuses = {getattr(j, "status", None) for j in active_jobs}
    return bool(statuses & {"queued", "processing", "receiving"}) and getattr(job, "status", None) in {
        "queued",
        "processing",
        "receiving",
    }
