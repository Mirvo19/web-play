# Worker Package — unified single-process model.
#
# Historical note: jobs used to be executed by a SEPARATE ``worker.py``
# process polling the DB every 2s. That process is gone. The code below is
# the package's public surface for the in-process supervisor.

from app.worker.supervisor import (
    JobSupervisor,
    claim_next_job,
    cleanup_orphan_workspaces,
    has_active_job_for_video,
    recover_interrupted_jobs,
    shutdown_requested,
    start_job_thread,
)

__all__ = [
    "JobSupervisor",
    "claim_next_job",
    "cleanup_orphan_workspaces",
    "has_active_job_for_video",
    "recover_interrupted_jobs",
    "shutdown_requested",
    "start_job_thread",
]
