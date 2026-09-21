"""Torrent ingestion API — all endpoints require login (same pattern as /api)."""
import json
import os
import uuid

from flask import Blueprint, request, jsonify, current_app

from app.auth import login_required
from app.models import db, TorrentJob, CDNAccount, Setting
from app.torrents import engine as torrent_engine
from app.torrents.coordinator import quarantine_base, torrent_enabled
from app.utils.uploads import (
    UploadTooLarge,
    sanitized_filename,
    stream_to_file,
)

torrents_bp = Blueprint("torrents", __name__, url_prefix="/api/torrents")

TORRENT_FILE_MAX_BYTES = 5 * 1024 * 1024


def _gate():
    """Fail closed when the feature is off or the engine is missing."""
    if not torrent_enabled(current_app):
        return jsonify({"error": "Torrent ingestion is disabled (torrent_enabled=false)."}), 503
    if not torrent_engine.is_available():
        return jsonify({"error": "Torrent engine unavailable (aria2c not installed)."}), 503
    return None


def _new_token() -> str:
    return uuid.uuid4().hex


@torrents_bp.route("/submit", methods=["POST"])
@login_required
def submit_torrent():
    """Submit a magnet link (JSON {magnet}) or a .torrent file (multipart).

    Fetches metadata ONLY; payload download starts after file selection.
    Returns 202 with the torrent job (state fetching_metadata).
    """
    magnet = None
    torrent_file = None
    if request.is_json:
        data = request.get_json(silent=True) or {}
        magnet = (data.get("magnet") or "").strip()
    elif "file" in request.files:
        torrent_file = request.files["file"]
    else:
        # Tolerate form-encoded magnet posts too.
        magnet = (request.form.get("magnet") or "").strip()

    if magnet:
        if not magnet.lower().startswith("magnet:?"):
            return jsonify({"error": "Not a magnet link (must start with 'magnet:?')."}), 400
        if len(magnet) > 4096:
            return jsonify({"error": "Magnet link too long."}), 400
        kind, ref, display = "magnet", magnet, magnet[:120]
    elif torrent_file is not None:
        raw_name = torrent_file.filename or "upload.torrent"
        if not raw_name.lower().endswith(".torrent"):
            return jsonify({"error": "Only .torrent files are accepted."}), 400
        kind, ref, display = "file", sanitized_filename(raw_name), sanitized_filename(raw_name)
    else:
        return jsonify({"error": "Provide a magnet link or a .torrent file."}), 400

    gated = _gate()
    if gated:
        return gated

    row = TorrentJob(
        source_kind=kind,
        source_ref=ref if kind == "magnet" else display,
        display_name=f"fetching metadata… ({display[:80]})",
        state="fetching_metadata",
        quarantine_token=_new_token(),
    )
    db.session.add(row)
    db.session.commit()

    try:
        base = quarantine_base(current_app)
        work_dir = torrent_engine.quarantine_for(base, row.quarantine_token)
        if kind == "file":
            dest = os.path.join(work_dir, "source.torrent")
            try:
                stream_to_file(torrent_file.stream, dest, TORRENT_FILE_MAX_BYTES)
            except UploadTooLarge:
                raise ValueError("The .torrent file exceeds the 5 MB metadata limit.")
    except (OSError, ValueError) as e:
        torrent_engine.wipe_quarantine(
            os.path.join(quarantine_base(current_app),
                         "torrent_" + row.quarantine_token))
        row.state = "failed"
        row.error_message = str(e)[:300]
        from datetime import datetime, timezone
        row.completed_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({"error": str(e)[:300]}), 400

    current_app.logger.info("Torrent %s submitted (%s)", row.id, kind)
    return jsonify({"message": "Torrent submitted; fetching metadata", "torrent": row.to_dict()}), 202


@torrents_bp.route("", methods=["GET"])
@login_required
def list_torrents():
    rows = TorrentJob.query.order_by(TorrentJob.created_at.desc()).all()
    return jsonify({"torrents": [r.to_dict() for r in rows]}), 200


@torrents_bp.route("/<torrent_id>", methods=["GET"])
@login_required
def get_torrent(torrent_id):
    row = db.session.get(TorrentJob, torrent_id)
    if row is None:
        return jsonify({"error": "Torrent not found"}), 404
    return jsonify(row.to_dict()), 200


@torrents_bp.route("/<torrent_id>/select", methods=["POST"])
@login_required
def select_files(torrent_id):
    """Choose which files to download + which CDN account the handoff uses."""
    row = db.session.get(TorrentJob, torrent_id)
    if row is None:
        return jsonify({"error": "Torrent not found"}), 404
    if row.state != "awaiting_selection":
        return jsonify({"error": f"Cannot select files while torrent is '{row.state}'."}), 409

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    raw_indexes = data.get("indexes")
    cdn_account_id = (data.get("cdn_account_id") or "").strip()
    if not isinstance(raw_indexes, list) or not raw_indexes:
        return jsonify({"error": "'indexes' must be a non-empty list."}), 400
    try:
        indexes = sorted({int(i) for i in raw_indexes})
    except (TypeError, ValueError):
        return jsonify({"error": "'indexes' must contain integers."}), 400

    account = db.session.get(CDNAccount, cdn_account_id)
    if account is None or not account.enabled:
        return jsonify({"error": "Selected CDN account is invalid or disabled."}), 400

    valid = {f["index"] for f in row.files if isinstance(f, dict)}
    unknown = [i for i in indexes if i not in valid]
    if unknown:
        return jsonify({"error": f"Unknown file indexes: {unknown}."}), 400

    row.selected_json = json.dumps(indexes)
    total = row.selected_total()
    try:
        cap_mb = max(100, int(Setting.get("torrent_max_total_mb", "4096")))
    except (TypeError, ValueError):
        cap_mb = 4096
    if total > cap_mb * 1024 * 1024:
        return jsonify({
            "error": f"Selected files total {round(total / 1024 / 1024, 1)} MB "
                     f"exceeds the {cap_mb} MB per-torrent limit."
        }), 413

    row.cdn_account_id = account.id
    row.state = "downloading"
    row.error_message = None
    row.progress_json = json.dumps({str(i): 0 for i in indexes})
    row.speed_bps = 0.0
    db.session.commit()
    current_app.logger.info("Torrent %s: downloading indexes %s", torrent_id, indexes)
    return jsonify({"message": "Download started", "torrent": row.to_dict()}), 200


@torrents_bp.route("/<torrent_id>/cancel", methods=["POST"])
@login_required
def cancel_torrent(torrent_id):
    row = db.session.get(TorrentJob, torrent_id)
    if row is None:
        return jsonify({"error": "Torrent not found"}), 404
    if row.state in ("handed_off", "failed", "cancelled"):
        return jsonify({"message": f"Torrent already {row.state}", "torrent": row.to_dict()}), 200

    row.cancel_requested = True
    db.session.commit()

    # If no worker holds it yet (fetch not started, or awaiting selection),
    # finalize immediately; otherwise the worker observes the flag/event.
    coord = current_app.extensions.get("torrent_coordinator")
    claimed = False
    if coord is not None:
        try:
            claimed = coord.cancel(torrent_id)
        except Exception:
            claimed = False
    if not claimed and row.state in ("fetching_metadata", "awaiting_selection"):
        from datetime import datetime, timezone

        base = quarantine_base(current_app)
        torrent_engine.wipe_quarantine(os.path.join(base, "torrent_" + (row.quarantine_token or "")))
        row.state = "cancelled"
        row.error_message = None
        row.completed_at = datetime.now(timezone.utc)
        db.session.commit()
    return jsonify({"message": "Cancellation requested", "torrent": row.to_dict()}), 200
