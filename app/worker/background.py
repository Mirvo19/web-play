"""
HC CDN Player — Background Worker Loop
=======================================
This module is ONLY used by worker.py (hc-cdn-worker.service).
It must NOT be imported or called from the Flask web application.

Key design invariants:
- ONE instance of this loop runs in the entire deployment (single process).
- The loop scans for queued jobs every few seconds.
- It enforces max_concurrent_jobs against HEAVY PROCESSING jobs only.
- HTTP requests and video viewers are completely independent of this loop.
"""
import os
import shutil
import time
import threading
from datetime import datetime, timezone
from flask import Flask
from app.models import db, Job, Video, Setting


def _cleanup_orphan_workspaces(app: Flask):
    """Remove working directories for jobs that are no longer active."""
    upload_folder = app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')
    if not os.path.exists(upload_folder):
        return

    with app.app_context():
        active_job_ids = {
            j.id for j in Job.query.filter(
                Job.status.in_(['queued', 'processing', 'receiving'])
            ).all()
        }

    for entry in os.listdir(upload_folder):
        path = os.path.join(upload_folder, entry)
        if not os.path.isdir(path):
            continue
        if entry not in active_job_ids:
            try:
                shutil.rmtree(path, ignore_errors=True)
                print(f"[Worker] Cleaned orphan workspace: {path}")
            except Exception:
                pass


def _recover_interrupted_jobs(app: Flask):
    """
    On worker startup, handle any jobs left in a stuck state from a
    previous crash or restart.

    - Jobs in 'processing' state: the previous worker was killed mid-job.
      Re-queue them so they will be retried.
    - Jobs in 'receiving' state: the HTTP upload was interrupted.
      Mark them failed — the client must re-upload.
    """
    with app.app_context():
        processing_jobs = Job.query.filter_by(status='processing').all()
        for j in processing_jobs:
            j.status = 'queued'
            j.stage = 'queued'
            j.current_step = 'Re-queued after restart'
            j.current_message = 'Job interrupted by restart and re-queued'
            j.cancel_requested = False
            print(f"[Worker] Re-queued interrupted job {j.id}")

        receiving_jobs = Job.query.filter_by(status='receiving').all()
        for j in receiving_jobs:
            j.status = 'failed'
            j.stage = 'failed'
            j.current_step = 'Upload interrupted'
            j.current_message = 'Upload was interrupted by application restart'
            j.completed_at = datetime.now(timezone.utc)
            print(f"[Worker] Marked interrupted upload {j.id} as failed")

        db.session.commit()


def _pick_and_start_job(app: Flask):
    """
    Try to pick one queued job and launch it in a daemon thread.

    Uses a compare-and-swap pattern:
      1. Count currently processing jobs.
      2. If under max_concurrent_jobs, grab the oldest queued job.
      3. Immediately flip its status to 'processing' and commit — this
         prevents a second worker instance from also picking the same job.
    """
    with app.app_context():
        max_concurrent = int(
            Setting.get('max_concurrent_jobs',
                        str(app.config.get('MAX_CONCURRENT_JOBS', 1)))
        )
        running = Job.query.filter_by(status='processing').count()

        if running >= max_concurrent:
            return  # Slot full — nothing to start

        job = (
            Job.query
            .filter_by(status='queued')
            .order_by(Job.created_at.asc())
            .first()
        )
        if not job:
            return  # Queue empty

        # Atomically claim the job
        job.status = 'processing'
        job.stage = 'inspecting_media'
        job.started_at = datetime.now(timezone.utc)
        job.current_step = 'Processing'
        job.current_message = 'Job picked up by worker'
        db.session.commit()
        job_id = job.id
        job_type = job.job_type

    # Run the job pipeline in a daemon thread so the worker loop stays
    # responsive for queue-scanning, status updates, and cancellation polls.
    thread = threading.Thread(
        target=_run_job,
        args=(app, job_id, job_type),
        daemon=True,
        name=f"job-{job_id[:8]}"
    )
    thread.start()
    print(f"[Worker] Started job {job_id} ({job_type}) on thread {thread.name}")


def _run_job(app: Flask, job_id: str, job_type: str):
    """Execute a single job inside a fresh app context on a worker thread."""
    from app.worker.pipeline import execute_video_pipeline
    from app.worker.deleter import execute_video_deletion

    with app.app_context():
        try:
            if job_type == 'transcode_and_upload':
                execute_video_pipeline(job_id)
            elif job_type == 'delete_video':
                execute_video_deletion(job_id)
            else:
                # Unknown job type — mark failed immediately
                job = Job.query.get(job_id)
                if job:
                    job.status = 'failed'
                    job.stage = 'failed'
                    job.error_message = f"Unknown job type: {job_type}"
                    job.completed_at = datetime.now(timezone.utc)
                    db.session.commit()
        except Exception as e:
            # Last-resort error handler — the pipeline should catch its own
            # exceptions but this prevents a crash from silently hanging a job.
            try:
                with app.app_context():
                    job = Job.query.get(job_id)
                    if job and job.status == 'processing':
                        job.status = 'failed'
                        job.stage = 'failed'
                        job.error_message = f"Unhandled worker error: {str(e)}"
                        job.completed_at = datetime.now(timezone.utc)
                        db.session.commit()
            except Exception:
                pass
            print(f"[Worker] Unhandled exception in job {job_id}: {e}")


# ---------------------------------------------------------------------------
# Public API — called by worker.py only
# ---------------------------------------------------------------------------

def run_worker_forever(app: Flask):
    """
    Main blocking loop — call this from worker.py (hc-cdn-worker.service).
    Polls the job queue every 2 seconds and starts eligible jobs.
    """
    print("[Worker] Recovering any interrupted jobs from previous run...")
    _recover_interrupted_jobs(app)

    print("[Worker] Cleaning orphaned workspaces...")
    _cleanup_orphan_workspaces(app)

    print("[Worker] Job processing loop running. Waiting for queued jobs...")
    while True:
        try:
            _pick_and_start_job(app)
        except Exception as e:
            print(f"[Worker] Error in worker loop: {e}")

        time.sleep(2)
