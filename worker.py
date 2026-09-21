"""Deprecated shim — the standalone worker process has been removed.

The application is now a SINGLE process: :mod:`run` (via
:func:`app.create_app`) starts an in-process :class:`JobSupervisor` that owns
job polling, ffmpeg supervision, CDN upload and cleanup.

This file is kept only so old ``hc-cdn-worker.service`` units and scripts
fail with a clear message instead of silently doing nothing. It boots the
unified app (scheduler enabled) and blocks until SIGTERM/SIGINT, exactly
like ``python run.py`` without serving HTTP traffic.

Remove your ``hc-cdn-worker.service`` unit — see MIGRATION.md.
"""

import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("worker-shim")

log.warning(
    "worker.py is deprecated: the standalone worker process was removed. "
    "Starting the unified app scheduler in-process instead. "
    "Delete hc-cdn-worker.service; see MIGRATION.md."
)

os.environ.setdefault("ENABLE_JOB_SCHEDULER", "true")

from app import create_app  # noqa: E402

app = create_app()
supervisor = app.extensions.get("job_supervisor")


def _handle_signal(signum, frame):
    log.info("Received signal %s — stopping scheduler...", signum)
    if supervisor is not None:
        supervisor.stop(timeout=30.0)
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

try:
    log.info("Unified scheduler running in-process. Waiting (Ctrl+C to stop)...")
    while supervisor is not None and supervisor.running:
        time.sleep(1.0)
except KeyboardInterrupt:
    pass
finally:
    if supervisor is not None and supervisor.running:
        supervisor.stop(timeout=30.0)
    log.info("Scheduler exited cleanly.")
