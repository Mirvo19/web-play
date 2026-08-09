"""
HC CDN Player — Standalone Background Job Worker
=================================================
This script is the entry point for hc-cdn-worker.service.

It creates a Flask app context (for database access) and then runs the
job-processing loop in the foreground forever.

Do NOT import or start this from the web application. The web process
(Gunicorn) must never run this loop — only this dedicated process should.

Usage:
    python worker.py

Managed by systemd as hc-cdn-worker.service.
"""
import os
import sys
import signal

# Allow the project root to be on sys.path even when run directly
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.worker.background import run_worker_forever

# Handle SIGTERM gracefully so systemd can stop the service cleanly
_shutdown_requested = False

def _handle_signal(signum, frame):
    global _shutdown_requested
    print(f"[Worker] Received signal {signum}, shutting down gracefully...")
    _shutdown_requested = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

if __name__ == '__main__':
    print("[Worker] Creating Flask app context...")
    app = create_app()
    print("[Worker] Starting job processing loop...")
    run_worker_forever(app)
    print("[Worker] Worker exited cleanly.")
