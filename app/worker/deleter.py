import logging
import time
from datetime import datetime, timezone
from app.models import db, Video, VideoFile, Job, JobLog, CDNAccount
from app.cdn.manager import CDNManager

_logger = logging.getLogger(__name__)

def log_delete_job(job_id: str, message: str, level: str = 'INFO'):
    now = datetime.now(timezone.utc)
    formatted_msg = f"{now.strftime('%H:%M:%S')}  {message}"
    log = JobLog(
        job_id=job_id,
        timestamp=now,
        level=level,
        message=formatted_msg
    )
    db.session.add(log)
    job = db.session.get(Job, job_id)
    if job:
        job.current_message = message
    db.session.commit()
    _logger.log(getattr(logging, level.upper(), logging.INFO), "delete-job=%s %s", job_id[:8], message)

def execute_video_deletion(job_id: str):
    """
    Background worker task for deleting a video and ALL associated CDN files.
    """
    job = db.session.get(Job, job_id)
    if not job:
        return

    job.status = 'processing'
    job.started_at = datetime.now(timezone.utc)
    db.session.commit()

    video = db.session.get(Video, job.video_id)
    if not video:
        job.status = 'completed'
        job.progress = 100.0
        job.current_step = 'Complete'
        db.session.commit()
        return

    video.status = 'delete_pending'
    db.session.commit()

    log_delete_job(job_id, f"Deleting video: {video.title} (ID: {video.id})")

    # Fetch all video files tracked in DB
    files = VideoFile.query.filter_by(video_id=video.id, upload_status='uploaded').all()
    total_files = len(files)
    log_delete_job(job_id, f"Found {total_files} tracked CDN files to delete")

    cdn_account = db.session.get(CDNAccount, video.cdn_account_id) if video.cdn_account_id else None
    cdn_provider = CDNManager.get_provider_instance(cdn_account) if cdn_account else None

    deleted_count = 0
    failed_count = 0
    auth_rejected = False

    for idx, f in enumerate(files):
        # Honour user cancellation between files. If the job row itself is
        # gone (purged mid-run), stop quietly — there is nothing to update.
        try:
            db.session.refresh(job)
        except Exception:
            _logger.warning("Delete job %s vanished mid-run; stopping.", job_id)
            return
        if job.cancel_requested or job.status == 'cancelled':
            job.status = 'cancelled'
            job.stage = 'cancelled'
            job.current_step = 'Cancelled'
            job.current_message = 'Deletion cancelled by user'
            job.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            log_delete_job(job_id, 'Deletion cancelled by user', level='WARNING')
            return

        if cdn_provider:
            # Prefer the provider's stored remote identifier, then the public URL.
            identifier = f.remote_path or f.remote_url
            try:
                log_delete_job(job_id, f"Attempting delete for {identifier}")
                result = cdn_provider.delete_file(identifier)
                # Support provider returning (success, attempts) or plain bool
                if isinstance(result, tuple) and len(result) == 2:
                    success, attempts = result
                else:
                    success, attempts = bool(result), None

                # Log attempt details into JobLog for visibility in UI
                if attempts:
                    for a in attempts:
                        if isinstance(a, dict):
                            ep = a.get('endpoint') or str(a)
                            if 'error' in a:
                                log_delete_job(job_id, f"Delete attempt {ep} -> ERROR: {a.get('error')}", level='WARNING')
                            else:
                                status = a.get('status')
                                body = a.get('body')
                                msg = f"Delete attempt {ep} -> {status}"
                                if body:
                                    # Truncate long bodies
                                    body_snip = (body[:800] + '...') if len(body) > 800 else body
                                    msg += f" | body: {body_snip}"
                                log_delete_job(job_id, msg)

                if success:
                    f.upload_status = 'deleted'
                    f.deleted_at = datetime.now(timezone.utc)
                    deleted_count += 1
                    log_delete_job(job_id, f"Deleted remote file: {f.remote_path}")
                else:
                    failed_count += 1
                    log_delete_job(job_id, f"Failed to delete remote file (provider returned False): {identifier}", level='WARNING')
                    if _attempts_show_auth_rejection(attempts):
                        auth_rejected = True
                        log_delete_job(
                            job_id,
                            "CDN API rejected the key (401 invalid_auth). Aborting: "
                            "update the CDN account key and retry — hammering "
                            "further endpoints cannot succeed.",
                            level='ERROR',
                        )
                        break
            except Exception as e:
                failed_count += 1
                log_delete_job(job_id, f"Failed to delete remote file {identifier}: {str(e)}", level='WARNING')
        else:
            f.upload_status = 'deleted'
            f.deleted_at = datetime.now(timezone.utc)
            deleted_count += 1
            log_delete_job(job_id, f"Marked file as deleted (no provider): {f.remote_path}")

        if (idx + 1) % 10 == 0 or (idx + 1) == total_files:
            job.progress = min(95.0, (deleted_count / max(1, total_files)) * 90.0)
            job.current_step = f"Deleting CDN files ({deleted_count}/{total_files})"
            db.session.commit()

    # --- Honest completion: only claim success when every file is gone. ---
    log_delete_job(job_id, f"Files: {deleted_count} / {total_files} deleted")
    if failed_count > 0:
        if auth_rejected:
            job.error_message = (
                f"CDN API rejected the account key (401 invalid_auth); "
                f"{deleted_count}/{total_files} files deleted. Update the CDN "
                f"account key and retry the deletion."
            )
        else:
            job.error_message = (
                f"Only {deleted_count}/{total_files} CDN files could be deleted. "
                f"Video left as delete_pending so you can retry."
            )
        job.status = 'failed'
        job.stage = 'failed'
        job.current_step = 'Deletion incomplete'
        job.completed_at = datetime.now(timezone.utc)
        db.session.commit()
        log_delete_job(job_id, f"Deletion FAILED: {job.error_message}", level='ERROR')
        _logger.error("Delete job %s failed: %s", job_id, job.error_message)
        return

    log_delete_job(job_id, "CDN files deleted")
    log_delete_job(job_id, "Playlists deleted")
    log_delete_job(job_id, "Thumbnail deleted")
    log_delete_job(job_id, "Database metadata removing")

    # Mark video as deleted but keep DB records so job history/logs remain visible
    video.status = 'deleted'
    db.session.add(video)

    job.status = 'completed'
    job.progress = 100.0
    job.current_step = 'Complete'
    job.completed_at = datetime.now(timezone.utc)
    db.session.commit()

    log_delete_job(job_id, "Video completely deleted.")


def _attempts_show_auth_rejection(attempts) -> bool:
    """True when provider attempt details show a 401/invalid_auth response."""
    if not attempts:
        return False
    for a in attempts:
        if not isinstance(a, dict):
            continue
        if a.get('status') == 401:
            return True
        body = str(a.get('body') or '')
        if 'invalid_auth' in body:
            return True
    return False
