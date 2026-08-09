import time
import threading
from flask import Flask
from app.models import db, Job, Setting
from app.worker.pipeline import execute_video_pipeline
from app.worker.deleter import execute_video_deletion

_worker_thread = None
_stop_event = threading.Event()

def worker_loop(app: Flask):
    """Background worker loop scanning for queued jobs."""
    with app.app_context():
        # Clean/recover state on startup: stuck 'processing' jobs are re-queued or checked
        stuck_jobs = Job.query.filter_by(status='processing').all()
        for j in stuck_jobs:
            j.status = 'queued'
            j.current_step = 'Re-queued after restart'
        db.session.commit()

    while not _stop_event.is_set():
        try:
            with app.app_context():
                max_concurrent = int(Setting.get('max_concurrent_jobs', app.config.get('MAX_CONCURRENT_JOBS', 1)))
                running_jobs = Job.query.filter_by(status='processing').count()

                if running_jobs < max_concurrent:
                    job = Job.query.filter_by(status='queued').order_by(Job.created_at.asc()).first()
                    if job:
                        if job.job_type == 'transcode_and_upload':
                            execute_video_pipeline(job.id)
                        elif job.job_type == 'delete_video':
                            execute_video_deletion(job.id)
        except Exception as e:
            print(f"[Worker Error] Exception in background worker: {e}")

        time.sleep(2)

def start_background_worker(app: Flask):
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _stop_event.clear()
        _worker_thread = threading.Thread(target=worker_loop, args=(app,), daemon=True)
        _worker_thread.start()
        print("[Worker] Background video worker thread started successfully.")

def stop_background_worker():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        _stop_event.set()
        _worker_thread.join(timeout=5)
        print("[Worker] Background video worker thread stopped.")
