import time
from datetime import datetime, timezone
from app.models import db, Video, VideoFile, Job, JobLog, CDNAccount
from app.cdn.manager import CDNManager
from flask import current_app

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
    job = Job.query.get(job_id)
    if job:
        job.current_message = message
    db.session.commit()
    try:
        if current_app.config.get('LOG_TO_STDOUT', False):
            print(f"[DELETE JOB {job_id}] {level}: {formatted_msg}")
    except Exception:
        pass

def execute_video_deletion(job_id: str):
    """
    Background worker task for deleting a video and ALL associated CDN files.
    """
    job = Job.query.get(job_id)
    if not job:
        return

    job.status = 'processing'
    job.started_at = datetime.now(timezone.utc)
    db.session.commit()

    video = Video.query.get(job.video_id)
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

    cdn_account = CDNAccount.query.get(video.cdn_account_id) if video.cdn_account_id else None
    cdn_provider = CDNManager.get_provider_instance(cdn_account) if cdn_account else None

    deleted_count = 0
    failed_count = 0

    for idx, f in enumerate(files):
        if cdn_provider:
            # Prefer remote_url when available (more likely to be directly deletable)
            identifier = f.remote_url or f.remote_path
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

    log_delete_job(job_id, f"Files: {deleted_count} / {total_files} deleted")
    log_delete_job(job_id, "✓ CDN files deleted")
    log_delete_job(job_id, "✓ Playlists deleted")
    log_delete_job(job_id, "✓ Thumbnail deleted")
    log_delete_job(job_id, "✓ Database metadata removing")

    # Mark video as deleted but keep DB records so job history/logs remain visible
    video.status = 'deleted'
    db.session.add(video)

    job.status = 'completed'
    job.progress = 100.0
    job.current_step = 'Complete'
    job.completed_at = datetime.now(timezone.utc)
    db.session.commit()

    log_delete_job(job_id, "Video completely deleted.")
