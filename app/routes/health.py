"""Health endpoints — no authentication required (load balancers need them).

* ``GET /up`` — fast liveness: the process is alive. No DB touch.
* ``GET /ready`` — readiness: fast DB ping + scheduler state. 200 or 503.
* ``GET /health`` — deep check: DB, ffmpeg/ffprobe, disk space, and per
  CDN-account reachability. 200 when everything passes, else 503. Each
  check reports its own status so operators see WHAT failed, not just that
  something did.
"""

from __future__ import annotations

import logging
import os
import shutil
import time

from flask import Blueprint, current_app, jsonify
from sqlalchemy import text

log = logging.getLogger(__name__)

health_bp = Blueprint("health", __name__)

# Disk is unhealthy below this many free bytes (2 GB — matches upload guard).
_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024


def _db_check() -> tuple[bool, str]:
    try:
        from app.models import db

        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "database reachable"
    except Exception as exc:
        return False, f"database unreachable: {exc}"


def _binaries_check() -> tuple[bool, str]:
    from app.startup import resolve_binary

    ffmpeg = resolve_binary(current_app.config.get("FFMPEG_BINARY"))
    ffprobe = resolve_binary(current_app.config.get("FFPROBE_BINARY"))
    if ffmpeg and ffprobe:
        return True, f"ffmpeg={ffmpeg}, ffprobe={ffprobe}"
    missing = "/".join(n for n, b in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not b)
    return False, f"missing: {missing}"


def _disk_check() -> tuple[bool, str]:
    folder = current_app.config.get("UPLOAD_FOLDER", "/tmp/video-processing")
    try:
        os.makedirs(folder, exist_ok=True)
        free = shutil.disk_usage(folder).free
    except OSError as exc:
        return False, f"upload folder unusable: {exc}"
    if free < _MIN_FREE_BYTES:
        return False, f"only {free / 1024**3:.2f} GB free (need 2 GB)"
    return True, f"{free / 1024**3:.2f} GB free"


def _cdn_checks() -> dict:
    """Per-account reachability. Never raises; failures are data."""
    results = {}
    try:
        from app.models import CDNAccount
        from app.cdn.manager import CDNManager

        accounts = CDNAccount.query.filter_by(enabled=True).all()
    except Exception as exc:
        return {"_query": {"ok": False, "detail": f"could not list accounts: {exc}"}}
    if not accounts:
        return {"_none": {"ok": True, "detail": "no enabled CDN accounts"}}
    for acc in accounts:
        try:
            ok, msg = CDNManager.test_account(acc)
            results[acc.id] = {"ok": bool(ok), "detail": str(msg)[:300], "name": acc.name}
        except Exception as exc:
            results[acc.id] = {"ok": False, "detail": f"check raised: {exc}", "name": acc.name}
    return results


@health_bp.route("/up", methods=["GET"])
def liveness():
    """Fast liveness probe — always 200 if the process serves HTTP."""
    return jsonify({"status": "ok", "service": "hc-cdn-player"}), 200


@health_bp.route("/ready", methods=["GET"])
def readiness():
    """Readiness probe — 200 only when the DB answers and jobs can run."""
    ok, detail = _db_check()
    supervisor = current_app.extensions.get("job_supervisor")
    scheduler = "disabled" if supervisor is None else ("running" if supervisor.running else "stopped")
    body = {
        "status": "ready" if ok else "not-ready",
        "database": {"ok": ok, "detail": detail},
        "scheduler": scheduler,
    }
    return jsonify(body), 200 if ok else 503


@health_bp.route("/health", methods=["GET"])
def deep_health():
    """Deep health check — DB, binaries, disk, CDN reachability."""
    started = time.time()
    db_ok, db_detail = _db_check()
    bin_ok, bin_detail = _binaries_check()
    disk_ok, disk_detail = _disk_check()
    cdn = _cdn_checks()
    cdn_ok = all(v.get("ok") for v in cdn.values())
    supervisor = current_app.extensions.get("job_supervisor")
    scheduler = "disabled" if supervisor is None else ("running" if supervisor.running else "stopped")

    overall = db_ok and bin_ok and disk_ok and cdn_ok
    body = {
        "status": "healthy" if overall else "degraded",
        "elapsed_ms": int((time.time() - started) * 1000),
        "checks": {
            "database": {"ok": db_ok, "detail": db_detail},
            "binaries": {"ok": bin_ok, "detail": bin_detail},
            "disk": {"ok": disk_ok, "detail": disk_detail},
            "cdn": cdn,
            "scheduler": {"state": scheduler},
        },
    }
    return jsonify(body), 200 if overall else 503
