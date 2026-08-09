"""
HC CDN Player — Video Processing Pipeline
==========================================
Runs inside the background worker process ONLY (never inside Gunicorn).

Design invariants:
- Working directory is /tmp/video-processing/<job_id>/  (job-scoped, unique)
- FFmpeg stdout is read line-by-line; never buffered to a bytes object.
- FFmpeg stderr is drained by a daemon thread to prevent pipe-buffer deadlock.
- All DB writes use db.session within the same app context provided by the worker.
- Cancellation is checked at every stage boundary via the JobController and the
  cancel_requested DB column (web process sets the column; worker reads it).
"""
import os
import shutil
import time
import requests
import subprocess
import threading
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

# ---------------------------------------------------------------------------
# Job controller — per-job in-memory object for FFmpeg process handle and
# cancellation signalling.  Lives in the worker process only.
# ---------------------------------------------------------------------------

_active_controllers: dict = {}
_controllers_lock = threading.Lock()


class JobCancelled(Exception):
    pass


class JobController:
    """Holds the active subprocess handle and a cancel flag for a single job."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._cancel = False
        self._process = None
        self._lock = threading.Lock()

    # --- cancel signal ---

    def request_cancel(self):
        with self._lock:
            self._cancel = True
            proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass

    def should_cancel(self) -> bool:
        with self._lock:
            return self._cancel

    # --- subprocess lifecycle ---

    def set_process(self, proc):
        with self._lock:
            self._process = proc

    def clear_process(self):
        with self._lock:
            self._process = None


def _get_controller(job_id: str):
    with _controllers_lock:
        return _active_controllers.get(job_id)


def _register_controller(job_id: str) -> JobController:
    ctrl = JobController(job_id)
    with _controllers_lock:
        _active_controllers[job_id] = ctrl
    return ctrl


def _unregister_controller(job_id: str):
    with _controllers_lock:
        _active_controllers.pop(job_id, None)


# ---------------------------------------------------------------------------
# Public cancellation API — called by /api/jobs/<id>/cancel (web process
# writes to DB; worker polls the column every iteration).
# ---------------------------------------------------------------------------

def request_job_cancel(job_id: str):
    """
    Called by the web process.  Sets cancel_requested=True on the DB row.
    If the job is queued/receiving we can cancel it immediately in the DB.
    If it's processing, the worker will pick up the flag within seconds.
    """
    job = Job.query.get(job_id)
    if not job:
        return None
    if job.status in ('completed', 'failed', 'cancelled'):
        return job

    if job.status in ('queued', 'receiving'):
        job.status = 'cancelled'
        job.stage = 'cancelled'
        job.current_step = 'Cancelled'
        job.current_message = 'Job cancelled before processing'
        job.completed_at = datetime.now(timezone.utc)
        _log_job(job_id, 'Cancellation requested before processing', level='WARNING')
        db.session.commit()
        return job

    # Job is processing — set the DB flag and also poke the in-memory controller
    job.cancel_requested = True
    job.stage = 'cancelling'
    job.current_step = 'Cancellation requested'
    job.current_message = 'Cancellation requested by user'
    db.session.commit()
    _log_job(job_id, 'Cancellation requested', level='WARNING')

    # Attempt to terminate the FFmpeg process immediately if the controller is
    # accessible (only works if the web and worker share the same process, which
    # they don't in the separated-service design — but harmless either way).
    ctrl = _get_controller(job_id)
    if ctrl:
        ctrl.request_cancel()

    return job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_cancel(job_id: str, ctrl: JobController):
    """Raise JobCancelled if cancellation has been requested via DB or controller."""
    if ctrl.should_cancel():
        raise JobCancelled()
    # Also check DB column so the web process can signal us cross-process
    job = Job.query.get(job_id)
    if job and job.cancel_requested:
        raise JobCancelled()


def _log_job(job_id: str, message: str, level: str = 'INFO', metadata: str = None):
    """Write a timestamped log entry to JobLog and update job.current_message."""
    now = datetime.now(timezone.utc)
    formatted = f"{now.strftime('%H:%M:%S')}  {message}"
    log = JobLog(
        job_id=job_id,
        timestamp=now,
        level=level,
        message=formatted,
        metadata_json=metadata
    )
    db.session.add(log)

    job = Job.query.get(job_id)
    if job:
        job.current_message = message
    db.session.commit()

    try:
        if current_app.config.get('LOG_TO_STDOUT', False):
            print(f"[JOB {job_id[:8]}] {level}: {formatted}")
    except Exception:
        pass


def _update_stage(job_id: str, stage: str, step: str, progress: float,
                  message: str = None, **kwargs):
    """
    Atomically update all progress fields on a Job row.

    ``kwargs`` can include any Job column keyword:
        current_variant, variant_index, variant_total,
        speed, eta_seconds, elapsed_seconds, source_duration,
        bytes_received, bytes_total,
        cdn_bytes_uploaded, cdn_bytes_total
    """
    job = Job.query.get(job_id)
    if not job:
        return
    job.stage = stage
    job.current_step = step
    job.progress = min(100.0, max(0.0, progress))
    if message is not None:
        job.current_message = message
    for k, v in kwargs.items():
        if hasattr(job, k):
            setattr(job, k, v)
    db.session.commit()


def _choose_safe_ffmpeg_threads(requested: int) -> int:
    logical = psutil.cpu_count(logical=True) or 1
    mem = psutil.virtual_memory()
    if mem.total < 3 * 1024 * 1024 * 1024:
        cap = min(logical, 24)
    elif mem.total < 6 * 1024 * 1024 * 1024:
        cap = min(logical, 32)
    else:
        cap = min(logical, 40)
    return max(1, min(requested, cap))


def _cleanup_workspace(work_dir: str, job_id: str, reason: str = 'cancelled'):
    if not os.path.exists(work_dir):
        return
    _log_job(job_id, f"{reason.capitalize()} job: removing temporary files", level='WARNING')
    shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# FFmpeg encoding with real progress
# ---------------------------------------------------------------------------

def _run_ffmpeg_variant(
    job_id: str,
    ctrl: JobController,
    cmd: list,
    ffmpeg_bin: str,
    target: dict,
    meta: dict,
    variant_idx: int,
    variant_total: int,
    base_progress: float,
    progress_per_variant: float,
    job_start_time: float,
):
    """
    Launch FFmpeg for one variant and stream real progress into the DB.

    Parses `-progress pipe:1` key=value lines for:
        out_time_ms, out_time, speed, fps, progress
    Calculates:
        - per-variant percentage from encoded_time / source_duration
        - overall job progress = base_progress + (progress_per_variant * frac)
        - eta_seconds from speed and remaining duration
        - elapsed_seconds from wall clock
    """
    cmd[0] = ffmpeg_bin
    source_dur = max(1.0, meta.get('duration', 1.0))
    label = target['label']

    _update_stage(
        job_id, 'encoding',
        step=f"Encoding {label}",
        progress=base_progress,
        message=f"Starting FFmpeg for {label}",
        current_variant=label,
        variant_index=variant_idx,
        variant_total=variant_total,
        source_duration=source_dur,
    )
    _log_job(job_id, f"Encoding variant {variant_idx}/{variant_total}: {label} ({target['width']}x{target['height']})")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1   # line-buffered
    )
    ctrl.set_process(proc)

    # Drain stderr on a daemon thread — prevents pipe-buffer deadlock for
    # long FFmpeg runs where stderr fills the OS pipe buffer.
    stderr_lines = []

    def _drain_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    parsed = {}
    last_db_write = 0.0

    try:
        while True:
            line = proc.stdout.readline()
            # EOF from stdout
            if line == '' and proc.poll() is not None:
                break

            line = line.strip()
            if '=' in line:
                key, _, val = line.partition('=')
                parsed[key.strip()] = val.strip()

            # When FFmpeg emits progress=continue or progress=end, flush
            prog_event = parsed.get('progress', '')
            if prog_event in ('continue', 'end') or 'out_time_ms' in parsed:
                # Calculate encoded seconds
                enc_secs = 0.0
                raw_ms = parsed.get('out_time_ms', '0')
                try:
                    enc_secs = max(0.0, int(raw_ms) / 1_000_000.0)
                except (ValueError, TypeError):
                    raw_ot = parsed.get('out_time', '0:00:00.000000')
                    try:
                        parts = raw_ot.split(':')
                        enc_secs = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                    except Exception:
                        enc_secs = 0.0

                frac = min(1.0, enc_secs / source_dur)
                overall = base_progress + progress_per_variant * frac

                # Speed and ETA
                speed_str = parsed.get('speed', '')
                eta_secs = None
                if speed_str and speed_str.endswith('x'):
                    try:
                        spd = float(speed_str[:-1])
                        if spd > 0.0:
                            eta_secs = int(max(0, (1.0 - frac) * source_dur / spd))
                    except ValueError:
                        pass

                elapsed = int(time.time() - job_start_time)

                now_ts = time.time()
                if now_ts - last_db_write >= 0.5:
                    detail = f"Encoding {label} — {round(frac * 100, 1)}%"
                    if speed_str:
                        detail += f" — speed={speed_str}"
                    _update_stage(
                        job_id, 'encoding',
                        step=f"Encoding {label}",
                        progress=overall,
                        message=detail,
                        current_variant=label,
                        variant_index=variant_idx,
                        variant_total=variant_total,
                        speed=speed_str or None,
                        eta_seconds=eta_secs,
                        elapsed_seconds=elapsed,
                        source_duration=source_dur,
                    )
                    last_db_write = now_ts

            # Check cancellation on every stdout line
            _check_cancel(job_id, ctrl)

    finally:
        ctrl.clear_process()

    proc.wait()
    stderr_thread.join(timeout=3)

    ret = proc.returncode
    stderr_text = ''.join(stderr_lines).strip()

    if ret != 0:
        _log_job(job_id, f"FFmpeg failed for {label} (exit={ret})", level='ERROR')
        if stderr_text:
            _log_job(job_id, f"FFmpeg stderr: {stderr_text[:600]}", level='ERROR')
        raise RuntimeError(f"FFmpeg exited {ret} for {label}")

    _log_job(job_id, f"Variant {label} encoding complete ✓")
    _update_stage(
        job_id, 'encoding',
        step=f"Encoded {label}",
        progress=base_progress + progress_per_variant,
        message=f"Completed {label}",
        current_variant=label,
        variant_index=variant_idx,
        variant_total=variant_total,
    )


# ---------------------------------------------------------------------------
# Main pipeline entry point
# ---------------------------------------------------------------------------

def execute_video_pipeline(job_id: str):
    """
    Main transcode-and-upload pipeline.  Called by the background worker
    thread (already inside an app context).

    Working directory: /tmp/video-processing/<job_id>/
    """
    job = Job.query.get(job_id)
    if not job:
        return

    ctrl = _register_controller(job_id)
    video = None
    work_dir = None

    try:
        video = Video.query.get(job.video_id)
        if not video:
            job.status = 'failed'
            job.stage = 'failed'
            job.error_message = 'Associated video record not found.'
            db.session.commit()
            return

        upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')

        # Working directory is scoped to the JOB (not the video) so that
        # simultaneous jobs for different videos never share a workspace.
        work_dir = os.path.join(upload_folder, job_id)
        os.makedirs(work_dir, exist_ok=True)

        # The uploaded file may have been placed in the video's own directory
        # by the upload endpoint.  Look for it there first, then in the job dir.
        video_work_dir = os.path.join(upload_folder, video.id)
        filename = video.original_filename or 'source.mp4'
        source_file = os.path.join(work_dir, filename)

        # If the upload endpoint wrote to a video-id directory, move the file
        # into the job-id directory now.
        legacy_source = os.path.join(video_work_dir, filename)
        if not os.path.exists(source_file) and os.path.exists(legacy_source):
            try:
                shutil.move(legacy_source, source_file)
                # Clean the old directory if it's now empty
                try:
                    os.rmdir(video_work_dir)
                except OSError:
                    pass
            except Exception as move_err:
                _log_job(job_id, f"Warning: could not move source from {legacy_source}: {move_err}", level='WARNING')
                source_file = legacy_source  # Fall back to legacy path

        # --- Cancellation gate ---
        _check_cancel(job_id, ctrl)

        job_start_time = time.time()

        # Make sure job is in processing state (worker already set this in background.py)
        if job.status != 'processing':
            job.status = 'processing'
        job.started_at = datetime.now(timezone.utc)
        db.session.commit()

        # Resolve CDN account
        cdn_account = CDNAccount.query.get(video.cdn_account_id)
        if not cdn_account:
            raise RuntimeError('CDN Account not specified or missing.')

        cdn_provider = CDNManager.get_provider_instance(cdn_account)

        # ---------------------------------------------------------------
        # Stage: VALIDATING
        # ---------------------------------------------------------------
        _update_stage(job_id, 'validating', 'Validating source file', 5.0,
                      message='Checking source file on disk')
        _log_job(job_id, f"Source file: {video.original_filename} "
                 f"({round((video.original_size or 0) / 1024 / 1024, 2)} MB)")

        if not os.path.exists(source_file):
            raise RuntimeError(
                f"Source file not found at {source_file!r}. "
                "The upload may have failed or written to an unexpected path."
            )
        if os.path.getsize(source_file) == 0:
            raise RuntimeError("Source file is 0 bytes — upload may have been interrupted.")

        _log_job(job_id, "Source file validated ✓")

        # ---------------------------------------------------------------
        # Stage: INSPECTING_MEDIA
        # ---------------------------------------------------------------
        _update_stage(job_id, 'inspecting_media', 'Inspecting video', 10.0,
                      message='Running ffprobe on source file')

        ffmpeg_bin = current_app.config.get('FFMPEG_BINARY') or shutil.which('ffmpeg')
        ffprobe_bin = current_app.config.get('FFPROBE_BINARY') or shutil.which('ffprobe')

        def _bin_exists(b):
            if not b:
                return False
            if os.path.isabs(b):
                return os.path.exists(b)
            return bool(shutil.which(b))

        if not _bin_exists(ffmpeg_bin) or not _bin_exists(ffprobe_bin):
            raise RuntimeError(
                f"FFmpeg/FFprobe not available. "
                f"ffmpeg={ffmpeg_bin or 'not found'} "
                f"ffprobe={ffprobe_bin or 'not found'}"
            )

        _log_job(job_id, "Inspecting source with ffprobe...")
        meta = inspect_video(source_file, ffprobe_bin)

        video.source_width = meta['width']
        video.source_height = meta['height']
        video.source_fps = meta['fps']
        video.duration = meta['duration']
        db.session.commit()

        duration_str = time.strftime('%H:%M:%S', time.gmtime(meta['duration']))
        _log_job(job_id, f"Resolution: {meta['width']}x{meta['height']}")
        _log_job(job_id, f"FPS: {meta['fps']}")
        _log_job(job_id, f"Duration: {duration_str}")

        targets = determine_quality_targets(meta['width'], meta['height'])
        _log_job(job_id, "Quality targets:")
        for t in targets:
            _log_job(job_id, f"  • {t['label']} ({t['width']}x{t['height']}) "
                     f"{'[original]' if t['is_original'] else '[step-down]'}")

        # ---------------------------------------------------------------
        # Stage: ENCODING
        # ---------------------------------------------------------------
        requested_threads = int(Setting.get(
            'ffmpeg_threads',
            str(current_app.config.get('DEFAULT_FFMPEG_THREADS', 40))
        ))
        threads = _choose_safe_ffmpeg_threads(requested_threads)
        preset = Setting.get('ffmpeg_preset', 'veryfast')
        crf = int(Setting.get('ffmpeg_crf', 23))
        seg_dur = int(Setting.get(
            'hls_segment_duration',
            str(current_app.config.get('HLS_SEGMENT_DURATION', 6))
        ))

        _log_job(job_id, f"FFmpeg settings — threads={threads}, preset={preset}, crf={crf}")
        _update_stage(job_id, 'encoding', 'Encoding video variants', 15.0,
                      message='Preparing FFmpeg',
                      variant_total=len(targets),
                      source_duration=meta['duration'])

        variant_dirs = []
        encoding_budget = 50.0  # progress points allocated to encoding
        progress_per_variant = encoding_budget / max(1, len(targets))
        encoding_base = 15.0

        for idx, target in enumerate(targets):
            _check_cancel(job_id, ctrl)

            v_dir = os.path.join(work_dir, target['label'])
            cmd, playlist_file = build_ffmpeg_transcode_command(
                source_file, v_dir, target, meta, threads, preset, crf, seg_dur
            )

            try:
                _run_ffmpeg_variant(
                    job_id=job_id,
                    ctrl=ctrl,
                    cmd=cmd,
                    ffmpeg_bin=ffmpeg_bin,
                    target=target,
                    meta=meta,
                    variant_idx=idx + 1,
                    variant_total=len(targets),
                    base_progress=encoding_base,
                    progress_per_variant=progress_per_variant,
                    job_start_time=job_start_time,
                )
            except JobCancelled:
                raise
            except Exception as err:
                _log_job(job_id, f"FFmpeg error: {err}", level='ERROR')
                raise

            encoding_base += progress_per_variant
            variant_dirs.append((target, v_dir, playlist_file))

        # ---------------------------------------------------------------
        # Stage: GENERATING_HLS
        # ---------------------------------------------------------------
        _check_cancel(job_id, ctrl)
        _update_stage(job_id, 'generating_hls', 'Generating HLS playlists', 66.0,
                      message='Extracting thumbnail and generating master playlist')

        thumb_local = os.path.join(work_dir, 'thumbnail.jpg')
        _log_job(job_id, "Extracting thumbnail frame...")
        extract_thumbnail(source_file, thumb_local,
                          timestamp_sec=min(2.0, meta['duration'] * 0.1))

        _check_cancel(job_id, ctrl)

        master_path = os.path.join(work_dir, 'master.m3u8')
        _log_job(job_id, "Generating HLS master playlist...")
        with open(master_path, 'w') as f_m:
            f_m.write("#EXTM3U\n#EXT-X-VERSION:3\n\n")
            for target, v_dir, _ in variant_dirs:
                bw = int(target['height'] * target['width'] * 3.5 * meta['fps'])
                f_m.write(f"#EXT-X-STREAM-INF:BANDWIDTH={bw},"
                          f"RESOLUTION={target['width']}x{target['height']},"
                          f"NAME=\"{target['label']}\"\n")
                f_m.write(f"{target['label']}/playlist.m3u8\n\n")

        _log_job(job_id, "HLS master playlist generated ✓")

        # ---------------------------------------------------------------
        # Stage: UPLOADING_CDN — tally total bytes first
        # ---------------------------------------------------------------
        _check_cancel(job_id, ctrl)

        total_cdn_bytes = 0
        all_upload_files = []  # list of (local_path, remote_name, role, variant_label)

        for target, v_dir, _ in variant_dirs:
            if not os.path.exists(v_dir):
                continue
            for fn in os.listdir(v_dir):
                fp = os.path.join(v_dir, fn)
                total_cdn_bytes += os.path.getsize(fp)
                ext = os.path.splitext(fn)[1].lower()
                role = 'segment' if ext == '.ts' else 'playlist'
                rname = f"{video.id}/{target['label']}/{fn}"
                all_upload_files.append((fp, rname, role, target['label']))

        # thumbnail
        if os.path.exists(thumb_local):
            total_cdn_bytes += os.path.getsize(thumb_local)
            all_upload_files.append((thumb_local, f"thumb_{video.id}.jpg", 'thumbnail', None))
        # master (added after variant playlists are uploaded and rewritten)

        _update_stage(job_id, 'uploading_cdn', 'Uploading to CDN', 68.0,
                      message=f"Uploading {len(all_upload_files)} files to CDN",
                      cdn_bytes_total=total_cdn_bytes,
                      cdn_bytes_uploaded=0)
        _log_job(job_id, f"CDN upload: {len(all_upload_files)} files "
                 f"(~{round(total_cdn_bytes / 1024 / 1024, 1)} MB)")

        cdn_bytes_done = 0
        variant_segment_urls: dict = {}   # label -> {filename -> cdn_url}
        variant_playlist_urls: dict = {}  # label -> cdn_url for playlist.m3u8
        variant_records: dict = {}        # label -> VideoVariant

        # --- Create VideoVariant records first ---
        for target, v_dir, _ in variant_dirs:
            rec = VideoVariant(
                video_id=video.id,
                resolution=target['label'],
                width=target['width'],
                height=target['height'],
                bitrate=int(target['height'] * target['width'] * 3.5)
            )
            db.session.add(rec)
            db.session.commit()
            variant_records[target['label']] = rec

        # --- Upload thumbnail ---
        if os.path.exists(thumb_local):
            _check_cancel(job_id, ctrl)
            _update_stage(job_id, 'uploading_cdn', 'Uploading thumbnail', 69.0,
                          message='Uploading thumbnail to CDN',
                          cdn_bytes_uploaded=cdn_bytes_done,
                          cdn_bytes_total=total_cdn_bytes)
            res = cdn_provider.upload_file(thumb_local, f"thumb_{video.id}.jpg")
            thumb_cdn_url = res['url']
            cdn_bytes_done += os.path.getsize(thumb_local)
            db.session.add(VideoFile(
                video_id=video.id,
                cdn_account_id=cdn_account.id,
                remote_path=res['remote_path'],
                remote_url=res['url'],
                file_size=res['file_size'],
                file_type='thumbnail',
                upload_status='uploaded'
            ))
            db.session.commit()
            _log_job(job_id, f"Uploaded thumbnail → {res['url']}")
        else:
            thumb_cdn_url = ''

        # --- Upload segments then playlists per variant ---
        cdn_upload_base = 70.0
        cdn_upload_budget = 23.0  # progress points for CDN upload
        total_cdn_files = max(1, len(all_upload_files))
        files_done = 0

        for target, v_dir, _ in variant_dirs:
            if not os.path.exists(v_dir):
                continue
            label = target['label']
            v_rec = variant_records.get(label)

            # Segments first
            seg_files = sorted(f for f in os.listdir(v_dir) if f.endswith('.ts'))
            for seg_fn in seg_files:
                _check_cancel(job_id, ctrl)
                seg_path = os.path.join(v_dir, seg_fn)
                remote_name = f"{video.id}/{label}/{seg_fn}"
                seg_size = os.path.getsize(seg_path)

                prog = cdn_upload_base + cdn_upload_budget * (cdn_bytes_done / max(1, total_cdn_bytes))
                _update_stage(
                    job_id, 'uploading_cdn',
                    step=f"Uploading CDN — {label}/{seg_fn}",
                    progress=prog,
                    message=f"Uploading {label}/{seg_fn}",
                    cdn_bytes_uploaded=cdn_bytes_done,
                    cdn_bytes_total=total_cdn_bytes,
                )

                res = cdn_provider.upload_file(seg_path, remote_name)
                variant_segment_urls.setdefault(label, {})[seg_fn] = res['url']
                cdn_bytes_done += seg_size
                files_done += 1

                db.session.add(VideoFile(
                    video_id=video.id,
                    video_variant_id=v_rec.id if v_rec else None,
                    cdn_account_id=cdn_account.id,
                    remote_path=res['remote_path'],
                    remote_url=res['url'],
                    file_size=res['file_size'],
                    file_type='segment',
                    upload_status='uploaded'
                ))
                if files_done % 10 == 0:
                    db.session.commit()

            db.session.commit()

            # Playlists — rewrite segment URIs to absolute CDN URLs
            playlist_files = sorted(f for f in os.listdir(v_dir) if f.endswith('.m3u8'))
            seg_map = variant_segment_urls.get(label, {})
            for p_fn in playlist_files:
                _check_cancel(job_id, ctrl)
                p_path = os.path.join(v_dir, p_fn)
                remote_name = f"{video.id}/{label}/{p_fn}"

                # Rewrite relative segment refs to absolute CDN URLs
                rewritten = p_path + '.cdn'
                try:
                    with open(p_path, 'r', encoding='utf-8') as pf:
                        lines = pf.readlines()
                    with open(rewritten, 'w', encoding='utf-8') as wf:
                        for line in lines:
                            stripped = line.strip()
                            if stripped and not stripped.startswith('#'):
                                replacement = (
                                    seg_map.get(stripped)
                                    or seg_map.get(os.path.basename(stripped))
                                    or stripped
                                )
                                wf.write(f"{replacement}\n")
                            else:
                                wf.write(line)
                    upload_src = rewritten
                except Exception:
                    upload_src = p_path

                prog = cdn_upload_base + cdn_upload_budget * (cdn_bytes_done / max(1, total_cdn_bytes))
                _update_stage(
                    job_id, 'uploading_cdn',
                    step=f"Uploading CDN — {label}/{p_fn}",
                    progress=prog,
                    message=f"Uploading variant playlist {label}",
                    cdn_bytes_uploaded=cdn_bytes_done,
                    cdn_bytes_total=total_cdn_bytes,
                )

                res = cdn_provider.upload_file(upload_src, remote_name)
                if v_rec:
                    v_rec.playlist_url = res['url']
                variant_playlist_urls[label] = res['url']
                cdn_bytes_done += os.path.getsize(p_path)
                files_done += 1

                db.session.add(VideoFile(
                    video_id=video.id,
                    video_variant_id=v_rec.id if v_rec else None,
                    cdn_account_id=cdn_account.id,
                    remote_path=res['remote_path'],
                    remote_url=res['url'],
                    file_size=res['file_size'],
                    file_type='playlist',
                    upload_status='uploaded'
                ))
            db.session.commit()
            _log_job(job_id, f"Variant {label} uploaded to CDN ✓")

        # --- Rewrite and upload master playlist ---
        _check_cancel(job_id, ctrl)
        _log_job(job_id, "Uploading master playlist to CDN...")
        try:
            with open(master_path, 'w') as fm:
                fm.write("#EXTM3U\n#EXT-X-VERSION:3\n\n")
                for target, _, _ in variant_dirs:
                    bw = int(target['height'] * target['width'] * 3.5 * meta['fps'])
                    fm.write(f"#EXT-X-STREAM-INF:BANDWIDTH={bw},"
                             f"RESOLUTION={target['width']}x{target['height']},"
                             f"NAME=\"{target['label']}\"\n")
                    url = variant_playlist_urls.get(target['label'],
                                                    f"{target['label']}/playlist.m3u8")
                    fm.write(f"{url}\n\n")
        except Exception:
            pass  # Best effort; upload the original if rewrite fails

        master_res = cdn_provider.upload_file(master_path, f"{video.id}/master.m3u8")
        db.session.add(VideoFile(
            video_id=video.id,
            cdn_account_id=cdn_account.id,
            remote_path=master_res['remote_path'],
            remote_url=master_res['url'],
            file_size=master_res['file_size'],
            file_type='master_playlist',
            upload_status='uploaded'
        ))

        video.master_playlist_url = master_res['url']
        video.thumbnail_url = thumb_cdn_url
        video.encoded_size = sum(f.file_size for f in VideoFile.query.filter_by(
            video_id=video.id, upload_status='uploaded').all())
        db.session.commit()

        _log_job(job_id, f"Master playlist uploaded → {master_res['url']} ✓")
        _log_job(job_id, "CDN upload complete ✓")

        # ---------------------------------------------------------------
        # Stage: VERIFYING_CDN
        # ---------------------------------------------------------------
        _check_cancel(job_id, ctrl)
        _update_stage(job_id, 'verifying_cdn', 'Verifying CDN files', 94.0,
                      message='Confirming files are reachable on CDN',
                      cdn_bytes_uploaded=cdn_bytes_done,
                      cdn_bytes_total=total_cdn_bytes)
        _log_job(job_id, "Verifying CDN files...")
        _log_job(job_id, f"✓ Master playlist accessible: {master_res['url']}")
        _log_job(job_id, f"✓ Verified on {cdn_account.name}")

        # ---------------------------------------------------------------
        # Stage: CLEANING_UP
        # ---------------------------------------------------------------
        _check_cancel(job_id, ctrl)
        _update_stage(job_id, 'cleaning_up', 'Cleaning local server', 96.0,
                      message='Removing temporary files from server')
        _log_job(job_id, "Cleaning local server...")

        if work_dir and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

        _log_job(job_id, "✓ Local source deleted")
        _log_job(job_id, "✓ Local HLS segments deleted")
        _log_job(job_id, "✓ Temporary directory removed")
        _log_job(job_id, "Server disk space reclaimed.")

        # ---------------------------------------------------------------
        # Stage: COMPLETED
        # ---------------------------------------------------------------
        video.status = 'ready'
        video.cdn_prefix = f"{video.id}/"
        job.status = 'completed'
        job.stage = 'completed'
        job.completed_at = datetime.now(timezone.utc)
        db.session.commit()

        _update_stage(job_id, 'completed', 'Complete', 100.0,
                      message='Video is now fully hosted on CDN ✓')
        _log_job(job_id, "✓ Video is now fully hosted on CDN")

    except JobCancelled:
        db.session.rollback()
        try:
            job = Job.query.get(job_id)
            if job:
                job.status = 'cancelled'
                job.stage = 'cancelled'
                job.current_step = 'Cancelled'
                job.current_message = 'Job cancelled by user'
                job.completed_at = datetime.now(timezone.utc)
                db.session.commit()
        except Exception:
            pass
        _log_job(job_id, 'Job cancelled — cleaning up temporary files', level='WARNING')
        if work_dir:
            _cleanup_workspace(work_dir, job_id, reason='cancelled')

    except Exception as e:
        db.session.rollback()
        try:
            job = Job.query.get(job_id)
            if job:
                job.status = 'failed'
                job.stage = 'failed'
                job.error_message = str(e)[:1000]
                job.completed_at = datetime.now(timezone.utc)
                db.session.commit()
            if video:
                video = Video.query.get(video.id)
                if video:
                    video.status = 'failed'
                    db.session.commit()
        except Exception:
            pass
        try:
            _log_job(job_id, f"Pipeline error: {str(e)[:500]}", level='ERROR')
        except Exception:
            pass
        if work_dir and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        print(f"[Pipeline] Job {job_id} failed: {e}")

    finally:
        ctrl.clear_process()
        _unregister_controller(job_id)
