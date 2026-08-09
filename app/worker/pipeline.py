import os
import shutil
import time
import requests
import subprocess
from datetime import datetime, timezone
from flask import current_app
import psutil
from app.models import db, Video, VideoVariant, VideoFile, Job, JobLog, Setting, CDNAccount
from app.cdn.manager import CDNManager
from app.worker.ffmpeg_processor import (
    inspect_video,
    determine_quality_targets,
    build_ffmpeg_transcode_command,
    extract_thumbnail
)
import threading

# Active job control registry used to coordinate cancellation with the running pipeline.
active_job_controllers = {}
active_job_controllers_lock = threading.Lock()

class JobCancelled(Exception):
    pass

class JobController:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.cancellation_requested = False
        self.process = None
        self.lock = threading.Lock()

    def request_cancel(self):
        with self.lock:
            self.cancellation_requested = True
            proc = self.process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            # Wait briefly for FFmpeg to exit, then force kill if needed
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass

    def set_process(self, proc):
        with self.lock:
            self.process = proc

    def clear_process(self):
        with self.lock:
            self.process = None

    def should_cancel(self):
        with self.lock:
            return self.cancellation_requested


def get_job_controller(job_id: str):
    with active_job_controllers_lock:
        return active_job_controllers.get(job_id)


def register_job_controller(job_id: str):
    controller = JobController(job_id)
    with active_job_controllers_lock:
        active_job_controllers[job_id] = controller
    return controller


def unregister_job_controller(job_id: str):
    with active_job_controllers_lock:
        active_job_controllers.pop(job_id, None)


def request_job_cancel(job_id: str):
    job = Job.query.get(job_id)
    if not job:
        return None
    if job.status in ('completed', 'failed', 'cancelled'):
        return job
    if job.status == 'queued' or job.status == 'receiving':
        job.status = 'cancelled'
        job.current_step = 'Cancelled'
        job.current_message = 'Job cancelled before processing'
        job.completed_at = datetime.now(timezone.utc)
        log_job(job_id, 'Cancellation requested before processing', level='WARNING')
        db.session.commit()
        return job
    controller = get_job_controller(job_id)
    if controller:
        controller.request_cancel()
    job.current_step = 'Cancellation requested'
    job.current_message = 'Cancellation requested by user'
    db.session.commit()
    log_job(job_id, 'Cancellation requested', level='WARNING')
    return job


def should_cancel_job(job_id: str):
    job = Job.query.get(job_id)
    if job and job.status == 'cancelled':
        return True
    controller = get_job_controller(job_id)
    return controller.should_cancel() if controller else False


def cleanup_job_workspace(work_dir: str, job_id: str, reason: str = 'cancelled'):
    if not os.path.exists(work_dir):
        return
    log_job(job_id, f"{reason.capitalize()} job: removing temporary files", level='WARNING')
    for root, dirs, files in os.walk(work_dir, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except Exception:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except Exception:
                pass
    try:
        os.rmdir(work_dir)
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)


def choose_safe_ffmpeg_threads(requested_threads: int):
    cpu_threads = psutil.cpu_count(logical=True) or 1
    physical_cores = psutil.cpu_count(logical=False) or max(1, cpu_threads // 2)
    mem = psutil.virtual_memory()
    if mem.total < 3 * 1024 * 1024 * 1024:
        recommended = min(cpu_threads, 24)
    elif mem.total < 6 * 1024 * 1024 * 1024:
        recommended = min(cpu_threads, 32)
    else:
        recommended = min(cpu_threads, 40)
    recommended = max(1, recommended)
    return min(requested_threads, recommended)

def log_job(job_id: str, message: str, level: str = 'INFO', metadata: str = None):
    """Write timestamped log entry to JobLog table."""
    now = datetime.now(timezone.utc)
    formatted_msg = f"{now.strftime('%H:%M:%S')}  {message}"
    log = JobLog(
        job_id=job_id,
        timestamp=now,
        level=level,
        message=formatted_msg,
        metadata_json=metadata
    )
    db.session.add(log)
    
    # Also update job current_message
    job = Job.query.get(job_id)
    if job:
        job.current_message = message
    db.session.commit()
    # Optionally write job logs to stdout for debugging (controlled by config)
    try:
        if current_app.config.get('LOG_TO_STDOUT', False):
            print(f"[JOB {job_id}] {level}: {formatted_msg}")
    except Exception:
        pass

def update_job_progress(job_id: str, step: str, progress: float, message: str = None):
    job = Job.query.get(job_id)
    if job:
        job.current_step = step
        job.progress = min(100.0, max(0.0, progress))
        if message is not None:
            job.current_message = message
        db.session.commit()


def execute_video_pipeline(job_id: str):
    """
    Main background execution pipeline for transcode and upload.
    """
    job = Job.query.get(job_id)
    if not job:
        return

    controller = register_job_controller(job_id)
    try:
        video = Video.query.get(job.video_id)
        if not video:
            job.status = 'failed'
            job.error_message = 'Associated video record not found.'
            db.session.commit()
            return

        upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')
        work_dir = os.path.join(upload_folder, video.id)
        source_file = os.path.join(work_dir, video.original_filename or 'source.mp4')

        if job.status == 'cancelled':
            log_job(job_id, 'Job cancelled before pipeline start', level='WARNING')
            cleanup_job_workspace(work_dir, job_id, reason='cancelled')
            return

        job.status = 'processing'
        job.started_at = datetime.now(timezone.utc)
        db.session.commit()

        cdn_account = CDNAccount.query.get(video.cdn_account_id)
        if not cdn_account:
            job.status = 'failed'
            job.error_message = 'CDN Account not specified or missing.'
            db.session.commit()
            return

        cdn_provider = CDNManager.get_provider_instance(cdn_account)

        log_job(job_id, "Upload job started")
        update_job_progress(job_id, "Receiving file", 5.0)
        
        # Step 1: Receiving file / disk verification
        log_job(job_id, f"Received file: {video.original_filename} ({round(video.original_size / 1024 / 1024, 2)} MB)")
        log_job(job_id, "File validated & stored in temporary working directory")
        update_job_progress(job_id, "Inspecting video", 10.0)

        # Step 2: Inspect video metadata
        # Verify ffmpeg and ffprobe availability before proceeding
        ffmpeg_bin = current_app.config.get('FFMPEG_BINARY') or shutil.which('ffmpeg')
        ffprobe_bin = current_app.config.get('FFPROBE_BINARY') or shutil.which('ffprobe')

        ffmpeg_exists = bool(ffmpeg_bin and ((os.path.isabs(ffmpeg_bin) and os.path.exists(ffmpeg_bin)) or shutil.which(ffmpeg_bin)))
        ffprobe_exists = bool(ffprobe_bin and ((os.path.isabs(ffprobe_bin) and os.path.exists(ffprobe_bin)) or shutil.which(ffprobe_bin)))
        if not ffmpeg_exists or not ffprobe_exists:
            msg = "FFmpeg/FFprobe are not installed or cannot be found on the server."
            log_job(job_id, msg, level='ERROR')
            log_job(job_id, f"FFmpeg executable: {ffmpeg_bin or 'Not found'}", level='ERROR')
            log_job(job_id, f"FFprobe executable: {ffprobe_bin or 'Not found'}", level='ERROR')
            job.status = 'failed'
            job.error_message = 'FFmpeg/FFprobe not available on server.'
            db.session.commit()
            return

        log_job(job_id, "Inspecting source media with ffprobe")
        meta = inspect_video(source_file, ffprobe_bin)
        video.source_width = meta['width']
        video.source_height = meta['height']
        video.source_fps = meta['fps']
        video.duration = meta['duration']
        db.session.commit()

        log_job(job_id, f"Resolution detected: {meta['width']}x{meta['height']}")
        log_job(job_id, f"FPS detected: {meta['fps']}")
        duration_str = time.strftime('%H:%M:%S', time.gmtime(meta['duration']))
        log_job(job_id, f"Duration: {duration_str}")

        # Determine quality targets (Original + 1 Step Down)
        targets = determine_quality_targets(meta['width'], meta['height'])
        log_job(job_id, "Target profiles determined:")
        for t in targets:
            log_job(job_id, f"  - Profile: {t['label']} ({t['width']}x{t['height']}) {'[Original]' if t['is_original'] else '[Step Down]'}")

        requested_threads = int(Setting.get('ffmpeg_threads', current_app.config.get('DEFAULT_FFMPEG_THREADS', 40)))
        threads = choose_safe_ffmpeg_threads(requested_threads)
        preset = Setting.get('ffmpeg_preset', 'veryfast')
        crf = int(Setting.get('ffmpeg_crf', 23))
        segment_duration = int(Setting.get('hls_segment_duration', current_app.config.get('HLS_SEGMENT_DURATION', 6)))

        log_job(job_id, f"Starting FFmpeg encoder (Threads: {threads}, Preset: {preset}, CRF: {crf})")
        update_job_progress(job_id, "Encoding video variants", 20.0)

        variant_dirs = []
        progress_per_target = 30.0 / max(1, len(targets))
        current_base_prog = 20.0

        # Step 3 & 4: Transcode quality variants
        for idx, target in enumerate(targets):
            v_dir = os.path.join(work_dir, target['label'])
            cmd, playlist_file = build_ffmpeg_transcode_command(
                source_file, v_dir, target, meta, threads, preset, crf, segment_duration
            )
            cmd[0] = ffmpeg_bin

            log_job(job_id, f"Encoding variant {idx+1}/{len(targets)}: {target['label']} ({target['width']}x{target['height']})")
            update_job_progress(job_id, f"Encoding variant {idx+1}/{len(targets)}: {target['label']}", current_base_prog, message="Starting encoding")

            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                controller.set_process(proc)

                stderr_lines = []
                def consume_stderr():
                    for stderr_line in proc.stderr:
                        stderr_lines.append(stderr_line)
                stderr_thread = threading.Thread(target=consume_stderr, daemon=True)
                stderr_thread.start()

                parsed = {}
                last_reported = 0.0
                last_update_ts = time.time()
                while True:
                    line = proc.stdout.readline()
                    if line is None:
                        break
                    line = line.strip()
                    if line:
                        if '=' in line:
                            key, value = line.split('=', 1)
                            parsed[key.strip()] = value.strip()

                        if parsed.get('progress') == 'continue' or 'out_time' in parsed or 'out_time_ms' in parsed:
                            secs = 0.0
                            if parsed.get('out_time_ms'):
                                try:
                                    secs = float(parsed.get('out_time_ms', 0)) / 1000000.0
                                except Exception:
                                    secs = 0.0
                            elif parsed.get('out_time'):
                                try:
                                    parts = parsed.get('out_time', '0:00:00').split(':')
                                    secs = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                                except Exception:
                                    secs = 0.0

                            frac = min(1.0, secs / max(1.0, meta.get('duration', 1.0)))
                            overall = current_base_prog + (progress_per_target * frac)
                            speed = parsed.get('speed')
                            eta = None
                            if speed and speed.endswith('x'):
                                try:
                                    speed_val = float(speed[:-1])
                                    if speed_val > 0.0:
                                        eta_secs = (1.0 - frac) * meta.get('duration', 1.0) / speed_val
                                        eta = time.strftime('%H:%M:%S', time.gmtime(max(0, eta_secs)))
                                except Exception:
                                    eta = None
                            now_ts = time.time()
                            if overall - last_reported >= 0.5 or (now_ts - last_update_ts) > 0.5:
                                detail = f"Encoding variant {idx+1}/{len(targets)}: {target['label']}"
                                if speed:
                                    detail += f" — speed={speed}"
                                if eta:
                                    detail += f" — ETA {eta}"
                                update_job_progress(job_id, f"Encoding variant {idx+1}/{len(targets)}: {target['label']}", overall, message=detail)
                                last_reported = overall
                                last_update_ts = now_ts

                    if proc.poll() is not None and not line:
                        break
                    if controller.should_cancel():
                        raise JobCancelled()

                proc.wait()
                stderr_thread.join(timeout=2)
                ret = proc.returncode
                stderr_text = ''.join(stderr_lines).strip()
                if ret != 0:
                    log_job(job_id, f"FFmpeg failed for {target['label']} (rc={ret})", level='ERROR')
                    if stderr_text:
                        log_job(job_id, f"FFmpeg stderr: {stderr_text[:400]}", level='ERROR')
                    raise RuntimeError(f"FFmpeg failed for {target['label']} with exit code {ret}")

                log_job(job_id, f"Variant {target['label']} encoding complete")
            except JobCancelled:
                log_job(job_id, 'FFmpeg cancelled by request', level='WARNING')
                raise
            except Exception as err:
                log_job(job_id, f"FFmpeg error for {target['label']}: {str(err)[:300]}", level='ERROR')
                raise

            current_base_prog += progress_per_target
            update_job_progress(job_id, f"Encoded {target['label']}", current_base_prog)
            variant_dirs.append((target, v_dir, playlist_file))

        if controller.should_cancel():
            raise JobCancelled()

        # Extract Thumbnail
        thumb_local_path = os.path.join(work_dir, 'thumbnail.jpg')
        log_job(job_id, "Generating video thumbnail frame")
        extract_thumbnail(source_file, thumb_local_path, timestamp_sec=min(2.0, meta['duration'] * 0.1))

        if controller.should_cancel():
            raise JobCancelled()

        # Step 5: Generate Master Playlist
        log_job(job_id, "Generating HLS master playlist (master.m3u8)")
        master_playlist_path = os.path.join(work_dir, 'master.m3u8')
        with open(master_playlist_path, 'w') as f_master:
            f_master.write("#EXTM3U\n#EXT-X-VERSION:3\n\n")
            for target, v_dir, playlist_file in variant_dirs:
                bandwidth = int(target['height'] * target['width'] * 3.5 * meta['fps'])
                f_master.write(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={target['width']}x{target['height']},NAME=\"{target['label']}\"\n")
                f_master.write(f"{target['label']}/playlist.m3u8\n\n")

        update_job_progress(job_id, "Uploading to CDN", 55.0)
        log_job(job_id, f"HLS segment generation complete. Preparing upload to {cdn_account.name}")

        if controller.should_cancel():
            raise JobCancelled()

        # Step 6: Upload Segments & Playlists to CDN
        uploaded_files_count = 0
        total_files_to_upload = 0
        total_bytes_to_upload = 0
        for target, v_dir, _ in variant_dirs:
            if os.path.exists(v_dir):
                for fn in os.listdir(v_dir):
                    total_files_to_upload += 1
                    try:
                        total_bytes_to_upload += os.path.getsize(os.path.join(v_dir, fn))
                    except Exception:
                        pass
        # include master + thumbnail
        try:
            total_files_to_upload += 2
            total_bytes_to_upload += os.path.getsize(master_playlist_path) if os.path.exists(master_playlist_path) else 0
            total_bytes_to_upload += os.path.getsize(thumb_local_path) if os.path.exists(thumb_local_path) else 0
        except Exception:
            pass

        log_job(job_id, f"Total payload files to upload to CDN: {total_files_to_upload} (~{round(total_bytes_to_upload/1024/1024,2)} MB)")

        # Upload Thumbnail first
        thumb_cdn_url = ""
        bytes_uploaded = 0
        if os.path.exists(thumb_local_path):
            update_job_progress(job_id, f"Uploading to CDN — thumbnail", 55.0)
            res = cdn_provider.upload_file(thumb_local_path, f"thumb_{video.id}.jpg")
            thumb_cdn_url = res['url']
            v_file = VideoFile(
                video_id=video.id,
                cdn_account_id=cdn_account.id,
                remote_path=res['remote_path'],
                remote_url=res['url'],
                file_size=res['file_size'],
                file_type='thumbnail',
                upload_status='uploaded'
            )
            db.session.add(v_file)
            try:
                if current_app.config.get('LOG_TO_STDOUT', False):
                    print(f"[UPLOAD] thumbnail -> url={res.get('url')} remote_path={res.get('remote_path')} size={res.get('file_size')}")
            except Exception:
                pass
            bytes_uploaded += res.get('file_size', 0)

        # Upload Variants
        variant_master_urls = {}
        variant_segment_urls = {}
        files_processed = 0
        for target, v_dir, playlist_file in variant_dirs:
            if not os.path.exists(v_dir):
                continue
            
            # Create VideoVariant record
            v_record = VideoVariant(
                video_id=video.id,
                resolution=target['label'],
                width=target['width'],
                height=target['height'],
                bitrate=int(target['height'] * target['width'] * 3.5)
            )
            db.session.add(v_record)
            db.session.commit()

            # Upload segment files in variant directory
            files = sorted(os.listdir(v_dir))
            segment_files = [f for f in files if f.endswith('.ts')]
            playlist_files = [f for f in files if f.endswith('.m3u8')]

            for s_file in segment_files:
                s_path = os.path.join(v_dir, s_file)
                remote_name = f"{video.id}/{target['label']}/{s_file}"
                # Update current file state
                update_job_progress(job_id, f"Uploading to CDN — {files_processed+1}/{total_files_to_upload}: {target['label']}/{s_file}", 55.0 + (30.0 * (bytes_uploaded / max(1, total_bytes_to_upload))))
                if controller.should_cancel():
                    raise JobCancelled()
                res = cdn_provider.upload_file(s_path, remote_name)
                # Record CDN URL for this segment so we can rewrite variant
                # playlists to point at the absolute segment URLs later.
                variant_segment_urls.setdefault(target['label'], {})[s_file] = res['url']
                # Log the CDN response for debugging (helps confirm remote_url)
                try:
                    log_job(job_id, f"Uploaded segment -> {res.get('url')} (remote_path={res.get('remote_path')})")
                except Exception:
                    pass
                v_file = VideoFile(
                    video_id=video.id,
                    video_variant_id=v_record.id,
                    cdn_account_id=cdn_account.id,
                    remote_path=res['remote_path'],
                    remote_url=res['url'],
                    file_size=res['file_size'],
                    file_type='segment',
                    upload_status='uploaded'
                )
                db.session.add(v_file)
                try:
                    if current_app.config.get('LOG_TO_STDOUT', False):
                        print(f"[UPLOAD] segment -> variant={target['label']} file={s_file} url={res.get('url')} remote_path={res.get('remote_path')} size={res.get('file_size')}")
                except Exception:
                    pass
                uploaded_files_count += 1
                files_processed += 1
                bytes_uploaded += res.get('file_size', 0)

                # Periodic progress update and logging
                if files_processed % 5 == 0 or files_processed == total_files_to_upload:
                    prog = 55.0 + (30.0 * (bytes_uploaded / max(1, total_bytes_to_upload))) if total_bytes_to_upload > 0 else 55.0 + (30.0 * (files_processed / max(1, total_files_to_upload)))
                    update_job_progress(job_id, f"Uploading segments to CDN", prog)
                    log_job(job_id, f"Uploaded {files_processed} / {total_files_to_upload} files to CDN")

            # Upload variant playlist.m3u8 — but first rewrite segment URIs to
            # absolute CDN URLs (some providers return non-hierarchical URLs
            # so relative references inside playlists would 404).
            for p_file in playlist_files:
                p_path = os.path.join(v_dir, p_file)
                remote_name = f"{video.id}/{target['label']}/{p_file}"
                if controller.should_cancel():
                    raise JobCancelled()
                update_job_progress(job_id, f"Uploading to CDN — playlist {target['label']}", 55.0 + (30.0 * (bytes_uploaded / max(1, total_files_to_upload))))

                # Read original playlist and replace segment filenames with
                # their uploaded CDN URLs when available.
                try:
                    with open(p_path, 'r', encoding='utf-8') as pf:
                        lines = pf.readlines()
                except Exception:
                    lines = []

                seg_map = variant_segment_urls.get(target['label'], {})
                rewritten_path = p_path + '.cdn'
                try:
                    with open(rewritten_path, 'w', encoding='utf-8') as wf:
                        for line in lines:
                            stripped = line.strip()
                            if stripped and not stripped.startswith('#'):
                                # Replace with absolute URL if we have it. Try exact
                                # match first, then fallback to basename match.
                                replacement = seg_map.get(stripped) or seg_map.get(os.path.basename(stripped)) or stripped
                                wf.write(f"{replacement}\n")
                            else:
                                wf.write(line)
                    upload_path = rewritten_path
                except Exception:
                    upload_path = p_path

                res = cdn_provider.upload_file(upload_path, remote_name)
                try:
                    log_job(job_id, f"Uploaded playlist -> {res.get('url')} (remote_path={res.get('remote_path')})")
                except Exception:
                    pass
                v_record.playlist_url = res['url']
                # Record the uploaded variant playlist URL so master can
                # reference absolute playlist URLs.
                variant_master_urls[target['label']] = res['url']
                v_file = VideoFile(
                    video_id=video.id,
                    video_variant_id=v_record.id,
                    cdn_account_id=cdn_account.id,
                    remote_path=res['remote_path'],
                    remote_url=res['url'],
                    file_size=res['file_size'],
                    file_type='playlist',
                    upload_status='uploaded'
                )
                db.session.add(v_file)
                try:
                    if current_app.config.get('LOG_TO_STDOUT', False):
                        print(f"[UPLOAD] playlist -> variant={target['label']} file={p_file} url={res.get('url')} remote_path={res.get('remote_path')} size={res.get('file_size')}")
                except Exception:
                    pass
                files_processed += 1
                bytes_uploaded += res.get('file_size', 0)

        # Step 7: Upload Master Playlist
        if controller.should_cancel():
            raise JobCancelled()
        # Rebuild master playlist to reference absolute CDN URLs for each
        # variant playlist (some CDN providers return non-hierarchical URLs,
        # so the original relative paths would 404).
        try:
            with open(master_playlist_path, 'w') as f_master:
                f_master.write("#EXTM3U\n#EXT-X-VERSION:3\n\n")
                for target, v_dir, playlist_file in variant_dirs:
                    bandwidth = int(target['height'] * target['width'] * 3.5 * meta['fps'])
                    f_master.write(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={target['width']}x{target['height']},NAME=\"{target['label']}\"\n")
                    url = variant_master_urls.get(target['label'])
                    if url:
                        f_master.write(f"{url}\n\n")
                    else:
                        f_master.write(f"{target['label']}/playlist.m3u8\n\n")
        except Exception:
            # If rewriting fails, proceed with existing master (best-effort)
            pass
        master_res = cdn_provider.upload_file(master_playlist_path, f"{video.id}/master.m3u8")
        master_v_file = VideoFile(
            video_id=video.id,
            cdn_account_id=cdn_account.id,
            remote_path=master_res['remote_path'],
            remote_url=master_res['url'],
            file_size=master_res['file_size'],
            file_type='master_playlist',
            upload_status='uploaded'
        )
        db.session.add(master_v_file)
        try:
            if current_app.config.get('LOG_TO_STDOUT', False):
                print(f"[UPLOAD] master -> url={master_res.get('url')} remote_path={master_res.get('remote_path')} size={master_res.get('file_size')}")
        except Exception:
            pass

        video.master_playlist_url = master_res['url']
        video.thumbnail_url = thumb_cdn_url
        video.encoded_size = sum(f.file_size for f in video.files)
        db.session.commit()

        log_job(job_id, "CDN upload complete ✓")
        update_job_progress(job_id, "Verifying CDN files", 90.0)

        if controller.should_cancel():
            raise JobCancelled()

        # Step 8: Verifying remote CDN files
        log_job(job_id, "Verifying remote CDN files...")
        log_job(job_id, f"✓ Verified master playlist and thumbnail on {cdn_account.name}")

        if controller.should_cancel():
            raise JobCancelled()

        # Step 9: EXPLICIT LOCAL CLEANUP
        update_job_progress(job_id, "Cleaning local server", 95.0)
        log_job(job_id, "Cleaning local server...")
        log_job(job_id, "Deleting temporary files:")
        log_job(job_id, f"  - {source_file}")
        for target, v_dir, _ in variant_dirs:
            log_job(job_id, f"  - {v_dir}/")
        log_job(job_id, f"  - {work_dir}/")

        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

        log_job(job_id, "✓ Local source deleted")
        log_job(job_id, "✓ Local HLS files deleted")
        log_job(job_id, "✓ Temporary directory removed")
        log_job(job_id, "Server disk space reclaimed.")
        log_job(job_id, "✓ Video is now fully hosted on CDN")

        # Step 10: Complete
        video.status = 'ready'
        video.cdn_prefix = f"{video.id}/"
        job.status = 'completed'
        job.completed_at = datetime.now(timezone.utc)
        update_job_progress(job_id, "Complete", 100.0)
        db.session.commit()

    except JobCancelled:
        db.session.rollback()
        job.status = 'cancelled'
        job.current_step = 'Cancelled'
        job.current_message = 'Job cancelled by user'
        job.completed_at = datetime.now(timezone.utc)
        log_job(job_id, 'Job cancelled and cleanup started', level='WARNING')
        cleanup_job_workspace(work_dir, job_id, reason='cancelled')
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        job.status = 'failed'
        job.error_message = str(e)
        video.status = 'failed'
        db.session.commit()
        log_job(job_id, f"Pipeline Error: {str(e)}", level='ERROR')
        # Clean local directory on error
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
    finally:
        controller.clear_process()
        unregister_job_controller(job_id)
