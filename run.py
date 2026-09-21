"""HC CDN Player — single unified entrypoint.

One process runs BOTH the web server and the background job supervisor
(job polling, ffmpeg supervision, CDN upload, cleanup). The supervisor is
started from the app factory (:func:`app.create_app`) and supervised here:

* SIGTERM / SIGINT stop job polling, terminate in-flight ffmpeg
  subprocesses via their job controllers, await executor threads, then exit.
* ``worker.py`` no longer exists as a separate process — see MIGRATION.md.
"""

import logging
import os
import signal
import sys

from app import create_app

log = logging.getLogger(__name__)

app = create_app()


def _graceful_shutdown(signum, frame):
    log.info("Received signal %s — stopping supervisor and exiting.", signum)
    supervisor = app.extensions.get("job_supervisor")
    if supervisor is not None:
        try:
            supervisor.stop(timeout=30.0)
        except Exception:
            log.exception("Error while stopping job supervisor")
    # Under `python run.py` (dev server) exit the interpreter; under
    # gunicorn each worker runs this for its own supervisor copy.
    sys.exit(0)


# Install at import so gunicorn workers (import-time, main thread) also
# get real SIGTERM handling. Guarded: threads / exotic runtimes may refuse.
for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, _graceful_shutdown)
    except (ValueError, OSError, RuntimeError) as exc:
        log.debug("Could not install handler for signal %s: %s", _sig, exc)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    try:
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
    finally:
        supervisor = app.extensions.get("job_supervisor")
        if supervisor is not None and getattr(supervisor, "running", False):
            supervisor.stop(timeout=30.0)
