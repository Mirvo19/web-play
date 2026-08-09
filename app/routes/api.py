import os
import shutil
import psutil
import time
import platform
import socket
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app, url_for
from werkzeug.utils import secure_filename
from app.auth import login_required
from app.models import db, Video, VideoVariant, VideoFile, Job, CDNAccount, Setting, JobLog, StorageSnapshot
from app.cdn.manager import CDNManager
from app.worker.pipeline import request_job_cancel

api_bp = Blueprint('api', __name__, url_prefix='/api')


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _check_disk_space(upload_folder: str, incoming_bytes: int) -> tuple:
    """
    Return (ok: bool, free_bytes: int, error_msg: str | None).

    Headroom required: incoming file + 3× (estimated HLS output overhead).
    """
    os.makedirs(upload_folder, exist_ok=True)
    disk = shutil.disk_usage(upload_folder)
    # Rough estimate: source + 3 variants × ~1.2× source each
    required = incoming_bytes * 4 + 500 * 1024 * 1024  # extra 500 MB buffer
    if disk.free < required:
        return False, disk.free, (
            f"Insufficient disk space. Need ~{round(required / 1024**3, 2)} GB, "
            f"only {round(disk.free / 1024**3, 2)} GB free."
        )
    return True, disk.free, None


def _max_upload_bytes(app) -> int:
    return int(app.config.get('MAX_UPLOAD_SIZE_GB', 4) * 1024 ** 3)


# ---------------------------------------------------------------------------
# Upload endpoints
# ---------------------------------------------------------------------------

@api_bp.route('/videos/upload', methods=['POST'])
@login_required
def upload_video_stream():
    """
    Simple multipart upload: receive file, queue job, return immediately.
    The file is streamed directly to disk — never loaded into memory.
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No video file provided'}), 400

    file = request.files['file']
    title = request.form.get('title', '').strip() or os.path.splitext(file.filename)[0]
    description = request.form.get('description', '').strip()
    cdn_account_id = request.form.get('cdn_account_id', '').strip()

    if not cdn_account_id:
        return jsonify({'error': 'CDN Account selection is required'}), 400

    cdn_account = CDNAccount.query.get(cdn_account_id)
    if not cdn_account or not cdn_account.enabled:
        return jsonify({'error': 'Selected CDN Account is invalid or disabled'}), 400

    # Check upload size limit from Content-Length header
    max_bytes = _max_upload_bytes(current_app)
    try:
        cl = int(request.headers.get('Content-Length') or 0)
    except Exception:
        cl = 0
    if cl > max_bytes:
        return jsonify({
            'error': f"Upload exceeds maximum allowed size of "
                     f"{current_app.config.get('MAX_UPLOAD_SIZE_GB', 4)} GB."
        }), 413

    upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')
    ok, free, err = _check_disk_space(upload_folder, max(cl, 100 * 1024 * 1024))
    if not ok:
        return jsonify({'error': err}), 507

    # Create Video record
    filename = secure_filename(file.filename)
    video = Video(
        title=title,
        description=description,
        original_filename=filename,
        cdn_account_id=cdn_account.id,
        status='processing'
    )
    db.session.add(video)
    db.session.commit()

    # Create Job first so we have a job_id for the work directory
    job = Job(
        video_id=video.id,
        job_type='transcode_and_upload',
        status='receiving',
        stage='receiving_upload',
        current_step='Receiving upload',
        current_message='Receiving file bytes from client',
        progress=0.0,
        bytes_total=cl if cl > 0 else 0,
    )
    db.session.add(job)
    db.session.commit()

    # Work dir is job-scoped
    work_dir = os.path.join(upload_folder, job.id)
    os.makedirs(work_dir, exist_ok=True)
    save_path = os.path.join(work_dir, filename)

    try:
        # Stream in 4 MB chunks — never call file.read() for the whole body
        chunk_size = 4 * 1024 * 1024
        bytes_written = 0
        start_ts = time.time()
        last_update = 0.0

        with open(save_path, 'wb', buffering=8 * 1024 * 1024) as out_f:
            while True:
                chunk = file.stream.read(chunk_size)
                if not chunk:
                    break
                out_f.write(chunk)
                bytes_written += len(chunk)

                now_ts = time.time()
                if cl > 0 and now_ts - last_update > 2.0:
                    pct = min(100.0, bytes_written / cl * 100.0)
                    db.session.refresh(job)
                    job.bytes_received = bytes_written
                    job.progress = pct
                    db.session.commit()
                    last_update = now_ts

        # Upload complete — immediately transition to queued
        file_size = os.path.getsize(save_path)
        video.original_size = file_size
        job.status = 'queued'
        job.stage = 'queued'
        job.current_step = 'Queued for processing'
        job.current_message = 'Upload complete — awaiting background processing'
        job.progress = 100.0
        job.bytes_received = file_size
        job.bytes_total = file_size
        db.session.commit()

    except Exception as e:
        # Clean up on failure
        try:
            db.session.delete(job)
            db.session.delete(video)
            db.session.commit()
        except Exception:
            pass
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({'error': f'Upload failed: {str(e)}'}), 500

    return jsonify({
        'message': 'Upload received and job queued successfully',
        'video_id': video.id,
        'job_id': job.id
    }), 202


@api_bp.route('/videos/upload/init', methods=['POST'])
@login_required
def upload_video_init():
    """
    Initialize a Video + Job and return an upload URL for a streaming PUT upload.
    Allows the client to show live progress before the body is fully received.
    """
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    cdn_account_id = request.form.get('cdn_account_id', '').strip()
    filename = request.form.get('filename', '').strip()
    try:
        declared_size = int(request.form.get('size', 0))
    except Exception:
        declared_size = 0

    if not cdn_account_id:
        return jsonify({'error': 'CDN Account selection is required'}), 400

    cdn_account = CDNAccount.query.get(cdn_account_id)
    if not cdn_account or not cdn_account.enabled:
        return jsonify({'error': 'Selected CDN Account is invalid or disabled'}), 400

    if not filename:
        return jsonify({'error': 'Filename is required'}), 400

    max_bytes = _max_upload_bytes(current_app)
    if declared_size > max_bytes:
        return jsonify({
            'error': f"File exceeds maximum allowed size of "
                     f"{current_app.config.get('MAX_UPLOAD_SIZE_GB', 4)} GB."
        }), 413

    upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')
    ok, _, err = _check_disk_space(upload_folder, max(declared_size, 100 * 1024 * 1024))
    if not ok:
        return jsonify({'error': err}), 507

    video = Video(
        title=title or filename,
        description=description,
        original_filename=filename,
        cdn_account_id=cdn_account.id,
        status='processing'
    )
    db.session.add(video)
    db.session.commit()

    job = Job(
        video_id=video.id,
        job_type='transcode_and_upload',
        status='receiving',
        stage='receiving_upload',
        current_step='Receiving upload',
        current_message='Receiving file bytes from client',
        progress=0.0,
        bytes_total=declared_size,
    )
    db.session.add(job)
    db.session.commit()

    return jsonify({
        'message': 'Upload initialized',
        'video_id': video.id,
        'job_id': job.id,
        'upload_url': f"/api/videos/{video.id}/upload"
    }), 201


@api_bp.route('/videos/<video_id>/upload', methods=['PUT', 'POST'])
@login_required
def upload_video_streamed(video_id):
    """
    Receive a raw streamed (PUT) or multipart (POST) upload for an
    initialized video.  Writes to disk in chunks, updating job progress.
    Marks the job queued IMMEDIATELY when the last byte lands.
    """
    video = Video.query.get_or_404(video_id)
    job = (
        Job.query
        .filter_by(video_id=video.id, status='receiving')
        .order_by(Job.created_at.desc())
        .first()
    )
    if not job:
        return jsonify({'error': 'No active upload job found for this video'}), 400

    upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')
    # Store upload in the job-scoped directory
    work_dir = os.path.join(upload_folder, job.id)
    os.makedirs(work_dir, exist_ok=True)
    filename = video.original_filename or 'source.mp4'
    save_path = os.path.join(work_dir, filename)

    try:
        content_length = int(request.headers.get('Content-Length') or 0)
    except Exception:
        content_length = 0

    # Enforce size limit
    max_bytes = _max_upload_bytes(current_app)
    if content_length > max_bytes:
        return jsonify({
            'error': f"Upload exceeds maximum allowed size of "
                     f"{current_app.config.get('MAX_UPLOAD_SIZE_GB', 4)} GB."
        }), 413

    # Update bytes_total from actual header if not set on init
    if content_length > 0 and (job.bytes_total or 0) == 0:
        job.bytes_total = content_length
        db.session.commit()

    try:
        # Multipart fallback (older clients)
        if 'file' in request.files:
            f = request.files['file']
            f.save(save_path)
            file_size = os.path.getsize(save_path)
            video.original_size = file_size
            job.status = 'queued'
            job.stage = 'queued'
            job.current_step = 'Queued for processing'
            job.current_message = 'Upload complete — awaiting background processing'
            job.progress = 100.0
            job.bytes_received = file_size
            job.bytes_total = file_size
            db.session.commit()
            return jsonify({'message': 'Upload saved', 'size': file_size}), 201

        # Streaming raw body — 4 MB chunks
        chunk_size = 4 * 1024 * 1024
        bytes_written = 0
        last_update = 0.0
        last_cancel_check = 0.0
        start_ts = time.time()

        with open(save_path, 'wb', buffering=8 * 1024 * 1024) as out_f:
            while True:
                chunk = request.stream.read(chunk_size)
                if not chunk:
                    break
                out_f.write(chunk)
                bytes_written += len(chunk)

                now_ts = time.time()

                # Cancellation check every 5 s (avoids DB hammering)
                if now_ts - last_cancel_check > 5.0:
                    db.session.refresh(job)
                    last_cancel_check = now_ts
                    if job.status == 'cancelled':
                        raise RuntimeError('Upload cancelled by user')

                # Progress update every 2 s
                if now_ts - last_update > 2.0:
                    elapsed = now_ts - start_ts
                    speed_bps = bytes_written / max(elapsed, 0.001)

                    if content_length > 0:
                        pct = min(99.0, bytes_written / content_length * 100.0)
                        eta = int(max(0, (content_length - bytes_written) / max(speed_bps, 1)))
                    else:
                        pct = min(80.0, 5.0 + bytes_written / (1024 * 1024) * 0.5)
                        eta = None

                    job.stage = 'receiving_upload'
                    job.current_step = 'Receiving upload'
                    job.current_message = (
                        f"Receiving {round(bytes_written / 1024**2, 1)} MB"
                        + (f" / {round(content_length / 1024**2, 1)} MB" if content_length else "")
                    )
                    job.progress = pct
                    job.bytes_received = bytes_written
                    job.bytes_total = content_length
                    if eta is not None:
                        job.eta_seconds = eta
                    db.session.commit()
                    last_update = now_ts

        # --- Upload body fully received ---
        file_size = os.path.getsize(save_path)
        video.original_size = file_size

        # Transition to queued IMMEDIATELY — do not leave it "receiving"
        job.status = 'queued'
        job.stage = 'queued'
        job.current_step = 'Queued for processing'
        job.current_message = 'Upload complete — awaiting background processing'
        job.progress = 100.0
        job.bytes_received = file_size
        job.bytes_total = file_size
        job.eta_seconds = None
        db.session.commit()

        return jsonify({'message': 'Upload saved', 'size': file_size}), 201

    except Exception as e:
        return jsonify({'error': f'Upload failed: {str(e)}'}), 500


# ---------------------------------------------------------------------------
# Video management
# ---------------------------------------------------------------------------

@api_bp.route('/videos/<video_id>', methods=['DELETE'])
@login_required
def delete_video_api(video_id):
    video = Video.query.get_or_404(video_id)

    job = Job(
        video_id=video.id,
        job_type='delete_video',
        status='queued',
        stage='queued',
        current_step='Queued Deletion',
        current_message='Deletion job queued and awaiting worker execution'
    )
    video.status = 'delete_pending'
    db.session.add(job)
    db.session.commit()

    db.session.add(JobLog(
        job_id=job.id,
        timestamp=datetime.now(timezone.utc),
        level='INFO',
        message='Deletion job queued and awaiting worker execution'
    ))
    db.session.commit()

    return jsonify({
        'message': 'Video deletion queued successfully',
        'job_id': job.id,
        'job_url': url_for('views.job_detail', job_id=job.id)
    }), 202


# ---------------------------------------------------------------------------
# Job API
# ---------------------------------------------------------------------------

@api_bp.route('/jobs/<job_id>', methods=['GET'])
@login_required
def get_job_api(job_id):
    """
    Lightweight job status endpoint — returns all progress fields.
    This is called every ~1-2 s by the UI; keep it fast.
    """
    job = Job.query.get_or_404(job_id)
    data = job.to_dict(include_logs=False)

    # Compute elapsed_seconds live if the job is actively running
    if job.started_at and job.status == 'processing':
        now_ts = time.time()
        started_ts = job.started_at.timestamp()
        data['elapsed_seconds'] = max(0, int(now_ts - started_ts))

    return jsonify(data), 200


@api_bp.route('/jobs/<job_id>/cancel', methods=['POST'])
@login_required
def cancel_job_api(job_id):
    job = request_job_cancel(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'message': 'Cancellation requested',
        'job_id': job.id,
        'status': job.status
    }), 200


@api_bp.route('/jobs/<job_id>/logs', methods=['GET'])
@login_required
def get_job_logs(job_id):
    job = Job.query.get_or_404(job_id)
    since = request.args.get('since')

    query = JobLog.query.filter_by(job_id=job.id)
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            query = query.filter(JobLog.timestamp > since_dt)
        except Exception:
            pass

    logs = query.order_by(JobLog.timestamp.asc()).all()
    return jsonify({
        'job': job.to_dict(),
        'logs': [l.to_dict() for l in logs]
    })


# ---------------------------------------------------------------------------
# CDN Accounts
# ---------------------------------------------------------------------------

@api_bp.route('/cdn-accounts', methods=['POST'])
@login_required
def create_cdn_account():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    provider = (data.get('provider') or 'Hack Club CDN').strip()
    api_key = (data.get('api_key') or '').strip()

    if not name:
        return jsonify({'error': 'Account name is required'}), 400
    if not api_key:
        return jsonify({'error': 'API key is required'}), 400

    account = CDNAccount(name=name, provider=provider)
    account.set_api_key(api_key)
    db.session.add(account)
    db.session.commit()

    return jsonify(account.to_dict(include_storage=False)), 201


@api_bp.route('/cdn-accounts/<account_id>/test', methods=['POST'])
@login_required
def test_cdn_account(account_id):
    account = CDNAccount.query.get_or_404(account_id)
    success, msg = CDNManager.test_account(account)
    return jsonify({'success': success, 'message': msg})


@api_bp.route('/cdn-accounts/<account_id>', methods=['DELETE'])
@login_required
def delete_cdn_account(account_id):
    account = CDNAccount.query.get_or_404(account_id)
    db.session.delete(account)
    db.session.commit()
    return jsonify({'message': 'CDN Account deleted successfully'})


# ---------------------------------------------------------------------------
# System stats
# ---------------------------------------------------------------------------

_cached_static_sys_info = None
_last_net_io = None
_last_net_time = None
_last_temp_size = 0
_last_temp_time = 0


def get_static_sys_info():
    global _cached_static_sys_info
    if _cached_static_sys_info is not None:
        return _cached_static_sys_info

    hostname = socket.gethostname() or platform.node() or "unknown"
    kernel = platform.release() or "Linux"
    arch = platform.machine() or "x86_64"
    os_name = f"{platform.system()} {platform.release()}"
    if os.path.exists("/etc/os-release"):
        try:
            with open("/etc/os-release") as f:
                info = {}
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        info[k] = v.strip('"')
                if "PRETTY_NAME" in info:
                    os_name = info["PRETTY_NAME"]
        except Exception:
            pass

    cpu_model = "Unknown Processor"
    if platform.system() == "Linux" and os.path.exists("/proc/cpuinfo"):
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        cpu_model = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
    if cpu_model == "Unknown Processor":
        cpu_model = platform.processor() or "x86_64 Processor"

    _cached_static_sys_info = {
        'hostname': hostname,
        'os': os_name,
        'kernel': kernel,
        'arch': arch,
        'cpu_model': cpu_model,
        'physical_cores': psutil.cpu_count(logical=False) or 48,
        'logical_threads': psutil.cpu_count(logical=True) or 92,
    }
    return _cached_static_sys_info


def calculate_network_speeds():
    global _last_net_io, _last_net_time
    now = time.time()
    try:
        net_io = psutil.net_io_counters()
    except Exception:
        return {'download_speed_bps': 0.0, 'upload_speed_bps': 0.0,
                'total_received_bytes': 0, 'total_sent_bytes': 0}

    if _last_net_io is None:
        dl, ul = 0.0, 0.0
    else:
        dt = max(now - _last_net_time, 0.001)
        dl = max(0, net_io.bytes_recv - _last_net_io.bytes_recv) / dt
        ul = max(0, net_io.bytes_sent - _last_net_io.bytes_sent) / dt

    _last_net_io = net_io
    _last_net_time = now
    return {'download_speed_bps': round(dl, 2), 'upload_speed_bps': round(ul, 2),
            'total_received_bytes': net_io.bytes_recv, 'total_sent_bytes': net_io.bytes_sent}


def get_cpu_temperature():
    try:
        if hasattr(psutil, "sensors_temperatures"):
            temps = psutil.sensors_temperatures()
            if temps:
                for key in ('coretemp', 'k10temp', 'cpu_thermal', 'zenpower', 'acpitz'):
                    if key in temps and temps[key]:
                        for s in temps[key]:
                            if getattr(s, 'current', None) and s.current > 0:
                                return round(s.current, 1)
                for entries in temps.values():
                    for s in entries:
                        if getattr(s, 'current', None) and s.current > 0:
                            return round(s.current, 1)
    except Exception:
        pass
    return None


def get_cpu_frequency():
    try:
        freq = psutil.cpu_freq()
        if freq and getattr(freq, 'current', None):
            return round(freq.current, 1)
    except Exception:
        pass
    return None


def get_load_averages():
    try:
        if hasattr(os, "getloadavg"):
            return [round(l, 2) for l in os.getloadavg()]
        elif hasattr(psutil, "getloadavg"):
            return [round(l, 2) for l in psutil.getloadavg()]
    except Exception:
        pass
    return [0.0, 0.0, 0.0]


def get_cached_temp_folder_size(upload_folder):
    global _last_temp_size, _last_temp_time
    now = time.time()
    if now - _last_temp_time < 10:
        return _last_temp_size
    total = 0
    if os.path.exists(upload_folder):
        try:
            for root, _, files in os.walk(upload_folder):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
        except Exception:
            pass
    _last_temp_size = total
    _last_temp_time = now
    return total


def get_disk_mounts():
    mounts = []
    try:
        seen = set()
        for p in psutil.disk_partitions(all=False):
            if p.fstype in ('squashfs', 'iso9660', 'tmpfs', 'devtmpfs') or 'loop' in p.device:
                continue
            if p.mountpoint in seen:
                continue
            try:
                usage = psutil.disk_usage(p.mountpoint)
                seen.add(p.mountpoint)
                mounts.append({
                    'mount': p.mountpoint, 'device': p.device, 'fstype': p.fstype,
                    'total_bytes': usage.total, 'used_bytes': usage.used,
                    'free_bytes': usage.free, 'usage_percent': usage.percent
                })
            except Exception:
                pass
    except Exception:
        pass
    return mounts


@api_bp.route('/system/stats', methods=['GET'])
@login_required
def get_system_stats():
    sys_info = get_static_sys_info()
    now_ts = time.time()
    boot_time = getattr(psutil, 'boot_time', lambda: now_ts)()
    uptime_sec = max(0, int(now_ts - boot_time))

    cpu_percent = psutil.cpu_percent(interval=None)
    per_core = psutil.cpu_percent(percpu=True) or []
    cpu_temp = get_cpu_temperature()
    cpu_freq = get_cpu_frequency()
    load_avg = get_load_averages()

    ram = psutil.virtual_memory()
    swap = psutil.swap_memory()

    upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')
    os.makedirs(upload_folder, exist_ok=True)
    main_disk = shutil.disk_usage(upload_folder)
    temp_folder_size = get_cached_temp_folder_size(upload_folder)
    disk_mounts = get_disk_mounts()
    net_stats = calculate_network_speeds()

    active_jobs_count = Job.query.filter_by(status='processing').count()
    queued_jobs_count = Job.query.filter_by(status='queued').count()
    completed_jobs_count = Job.query.filter_by(status='completed').count()
    failed_jobs_count = Job.query.filter_by(status='failed').count()
    total_jobs_count = Job.query.count()

    active_jobs = Job.query.filter_by(status='processing').order_by(Job.started_at.desc()).all()
    active_jobs_data = []
    for aj in active_jobs:
        jd = aj.to_dict(include_logs=False)
        if aj.video:
            jd['video_title'] = aj.video.title
        if aj.started_at:
            jd['elapsed_seconds'] = max(0, int(now_ts - aj.started_at.timestamp()))
        active_jobs_data.append(jd)

    active_job_data = active_jobs_data[0] if active_jobs_data else None

    ffmpeg_threads = Setting.get('ffmpeg_threads', str(current_app.config.get('DEFAULT_FFMPEG_THREADS', 40)))
    max_concurrent = Setting.get('max_concurrent_jobs', str(current_app.config.get('MAX_CONCURRENT_JOBS', 1)))

    video_count = Video.query.count()
    variant_count = VideoVariant.query.count()
    cdn_file_count = VideoFile.query.count()
    cdn_storage_used = sum(
        f.file_size or 0
        for f in VideoFile.query.filter_by(upload_status='uploaded').all()
    )

    cdn_accounts = CDNAccount.query.filter_by(enabled=True).all()
    cdn_stats = [acc.to_dict(include_storage=True) for acc in cdn_accounts]
    total_avail_cdn_bytes = sum(acc['storage']['available_bytes'] for acc in cdn_stats)

    capacity_calc = {
        'hours_1440p': round(total_avail_cdn_bytes / (8_000_000 / 8 * 3600), 1),
        'hours_1080p': round(total_avail_cdn_bytes / (4_500_000 / 8 * 3600), 1),
        'hours_720p': round(total_avail_cdn_bytes / (2_500_000 / 8 * 3600), 1),
    }

    return jsonify({
        'hostname': sys_info['hostname'],
        'os': sys_info['os'],
        'kernel': sys_info['kernel'],
        'arch': sys_info['arch'],
        'cpu_model': sys_info['cpu_model'],
        'uptime': uptime_sec,
        'load': load_avg,
        'timestamp': int(now_ts),

        'cpu': {
            'usage_percent': cpu_percent,
            'cores': sys_info['physical_cores'],
            'threads': sys_info['logical_threads'],
            'physical_cores': sys_info['physical_cores'],
            'logical_threads': sys_info['logical_threads'],
            'temperature': cpu_temp,
            'frequency_mhz': cpu_freq,
            'per_core': per_core,
            'load_avg': load_avg
        },

        'ram': {
            'total_bytes': ram.total,
            'used_bytes': ram.used,
            'available_bytes': ram.available,
            'percent': ram.percent,
            'usage_percent': ram.percent,
            'swap_used': swap.used,
            'swap_total': swap.total,
            'swap_percent': swap.percent
        },

        'disk': {
            'total_bytes': main_disk.total,
            'used_bytes': main_disk.used,
            'free_bytes': main_disk.free,
            'usage_percent': round(main_disk.used / main_disk.total * 100, 1) if main_disk.total else 0.0,
            'temp_folder_size': temp_folder_size,
            'temp_storage_bytes': temp_folder_size,
            'mounts': disk_mounts
        },

        'network': {
            'download_speed_bps': net_stats['download_speed_bps'],
            'upload_speed_bps': net_stats['upload_speed_bps'],
            'download': round(net_stats['download_speed_bps'] / (1024 * 1024), 2),
            'upload': round(net_stats['upload_speed_bps'] / (1024 * 1024), 2),
            'total_received': net_stats['total_received_bytes'],
            'total_sent': net_stats['total_sent_bytes'],
            'total_received_bytes': net_stats['total_received_bytes'],
            'total_sent_bytes': net_stats['total_sent_bytes']
        },

        'player': {
            'jobs': {
                'active': active_jobs_count,
                'queued': queued_jobs_count,
                'completed': completed_jobs_count,
                'failed': failed_jobs_count,
                'total': total_jobs_count
            },
            'active_job': active_job_data,
            'active_jobs': active_jobs_data,
            'ffmpeg_config': {
                'ffmpeg_threads': ffmpeg_threads,
                'max_concurrent_jobs': max_concurrent,
                'active_ffmpeg_jobs': active_jobs_count
            },
            'stats': {
                'video_count': video_count,
                'variant_count': variant_count,
                'cdn_file_count': cdn_file_count,
                'cdn_storage_used_bytes': cdn_storage_used
            },
            'cdn_accounts': cdn_stats,
            'capacity_calculator': capacity_calc
        },

        'active_job': active_job_data,
        'cdn_accounts': cdn_stats,
        'capacity_calculator': capacity_calc
    })


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@api_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def handle_settings():
    if request.method == 'GET':
        return jsonify({
            'ffmpeg_threads': Setting.get('ffmpeg_threads', '40'),
            'max_concurrent_jobs': Setting.get('max_concurrent_jobs', '1'),
            'ffmpeg_preset': Setting.get('ffmpeg_preset', 'veryfast'),
            'ffmpeg_crf': Setting.get('ffmpeg_crf', '23'),
            'hls_segment_duration': Setting.get('hls_segment_duration', '6')
        })

    data = request.get_json() or {}
    if 'ffmpeg_threads' in data:
        val = int(data['ffmpeg_threads'])
        if 1 <= val <= 92:
            Setting.set('ffmpeg_threads', str(val))
    if 'max_concurrent_jobs' in data:
        val = int(data['max_concurrent_jobs'])
        if 1 <= val <= 5:
            Setting.set('max_concurrent_jobs', str(val))
    if 'ffmpeg_preset' in data:
        Setting.set('ffmpeg_preset', str(data['ffmpeg_preset']))
    if 'ffmpeg_crf' in data:
        Setting.set('ffmpeg_crf', str(data['ffmpeg_crf']))
    if 'hls_segment_duration' in data:
        Setting.set('hls_segment_duration', str(data['hls_segment_duration']))

    return jsonify({'message': 'Processing settings updated successfully'})
