"""Deprecated compat shim over :mod:`app.worker.supervisor`.

The old ``run_worker_forever`` blocking loop (separate worker process) is
superseded by :class:`~app.worker.supervisor.JobSupervisor`, which runs
in-process and is started from the app factory. This module keeps the old
names working for any lingering imports/tests by delegating to the new code.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

# Re-exported so `from app.worker.background import X` keeps working.
from app.worker.supervisor import (  # noqa: F401
    _execute_job,
    claim_next_job,
    cleanup_orphan_workspaces as _cleanup_orphan_workspaces,
    has_active_job_for_video,
    recover_interrupted_jobs as _recover_interrupted_jobs,
    shutdown_requested,
)


def start_job_thread(app, job):
    """Submit a job to a one-off thread (resolves _execute_job lazily so tests
    can patch ``background._execute_job``)."""
    import app.worker.background as _self

    thread = threading.Thread(
        target=_self._execute_job, args=(app, job.id, job.job_type), daemon=True
    )
    thread.start()
    return thread


def _pick_and_start_job(app):
    from app.worker.supervisor import _execute_job as _exec

    claimed = claim_next_job(app)
    if claimed is None:
        return
    job_id, job_type = claimed
    thread = threading.Thread(
        target=_exec, args=(app, job_id, job_type), daemon=True, name=f"job-{job_id[:8]}"
    )
    thread.start()
    log.info("Started job %s (%s) on thread %s", job_id, job_type, thread.name)


def run_worker_forever(app, poll_interval: float = 2.0):
    """Legacy blocking loop — prefer JobSupervisor; kept for compatibility."""
    from app.worker.supervisor import _shutdown_event

    log.warning(
        "run_worker_forever is deprecated; the app factory now starts "
        "JobSupervisor in-process. This loop still works but stop migrating."
    )
    _shutdown_event.clear()
    _recover_interrupted_jobs(app)
    _cleanup_orphan_workspaces(app)
    log.info("Legacy job processing loop running. Waiting for queued jobs...")
    while not _shutdown_event.is_set():
        try:
            _pick_and_start_job(app)
        except Exception:
            log.exception("Error in legacy worker loop")
        time.sleep(poll_interval)
