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

@api_bp.route('/videos/upload', methods=['POST'])
@login_required
def upload_video_stream():
    """
    Stream upload endpoint with server disk space monitoring.
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

    # 1. Disk Space Monitoring before accepting file!
    upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')
    os.makedirs(upload_folder, exist_ok=True)
    disk_info = shutil.disk_usage(upload_folder)

    # Require at least 2 GB free disk space before processing
    min_required_disk = 2 * 1024 * 1024 * 1024
    if disk_info.free < min_required_disk:
        return jsonify({
            'error': f'Server disk space critically low ({round(disk_info.free / 1024 / 1024 / 1024, 2)} GB free). Cannot process video upload.'
        }), 507

    # 2. Create Video record
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

    # 3. Stream file directly to temporary disk path
    work_dir = os.path.join(upload_folder, video.id)
    os.makedirs(work_dir, exist_ok=True)
    save_path = os.path.join(work_dir, filename)

    try:
        file.save(save_path)
        video.original_size = os.path.getsize(save_path)
        db.session.commit()
    except Exception as e:
        db.session.delete(video)
        db.session.commit()
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({'error': f'Failed to stream upload file to server disk: {str(e)}'}), 500

    # 4. Queue transcode and upload job
    job = Job(
        video_id=video.id,
        job_type='transcode_and_upload',
        status='queued',
        current_step='Queued',
        current_message='Job queued for background processing'
    )
    db.session.add(job)
    db.session.commit()

    return jsonify({
        'message': 'Upload received and job queued successfully',
        'video_id': video.id,
        'job_id': job.id
    }), 202


@api_bp.route('/videos/upload/init', methods=['POST'])
@login_required
def upload_video_init():
    """
    Initialize a Video + Job and return an upload URL for streamed PUT upload.
    This allows the client to begin uploading and show live server-side progress.
    """
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    cdn_account_id = request.form.get('cdn_account_id', '').strip()
    filename = request.form.get('filename', '').strip()

    if not cdn_account_id:
        return jsonify({'error': 'CDN Account selection is required'}), 400

    cdn_account = CDNAccount.query.get(cdn_account_id)
    if not cdn_account or not cdn_account.enabled:
        return jsonify({'error': 'Selected CDN Account is invalid or disabled'}), 400

    if not filename:
        return jsonify({'error': 'Filename is required'}), 400

    # Create Video record (status processing while upload proceeds)
    video = Video(
        title=title or filename,
        description=description,
        original_filename=filename,
        cdn_account_id=cdn_account.id,
        status='processing'
    )
    db.session.add(video)
    db.session.commit()

    # Create Job immediately so client can poll job state during upload
    job = Job(
        video_id=video.id,
        job_type='transcode_and_upload',
        status='receiving',
        current_step='Receiving upload',
        current_message='Receiving file bytes from client',
        progress=0.0
    )
    db.session.add(job)
    db.session.commit()

    upload_url = f"/api/videos/{video.id}/upload"
    return jsonify({
        'message': 'Upload initialized',
        'video_id': video.id,
        'job_id': job.id,
        'upload_url': upload_url
    }), 201


@api_bp.route('/videos/<video_id>/upload', methods=['PUT', 'POST'])
@login_required
def upload_video_streamed(video_id):
    """
    Receive raw streamed upload for an initialized video. Expects raw body bytes
    (PUT) or multipart/form-data (POST). This handler writes to disk in chunks
    and updates Job.progress based on Content-Length when available.
    """
    video = Video.query.get_or_404(video_id)
    job = Job.query.filter_by(video_id=video.id, status='receiving').order_by(Job.created_at.desc()).first()
    if not job:
        return jsonify({'error': 'No active upload job found for this video'}), 400

    upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')
    work_dir = os.path.join(upload_folder, video.id)
    os.makedirs(work_dir, exist_ok=True)
    filename = video.original_filename or 'source.mp4'
    save_path = os.path.join(work_dir, filename)

    # Try to determine content length
    try:
        total_bytes = int(request.headers.get('Content-Length') or 0)
    except Exception:
        total_bytes = 0

    try:
        # If this is a multipart/form-data POST coming from older clients, fall back
        if 'file' in request.files:
            f = request.files['file']
            f.save(save_path)
            bytes_written = os.path.getsize(save_path)
            video.original_size = bytes_written
            job.status = 'queued'
            job.current_step = 'Queued for processing'
            job.current_message = 'Upload complete, awaiting background processing'
            job.progress = 100.0
            db.session.commit()
            return jsonify({'message': 'Upload saved'}), 201

        # Stream raw body in chunks
        chunk_size = 64 * 1024
        bytes_written = 0
        last_update = 0
        with open(save_path, 'wb') as out_f:
            while True:
                chunk = request.stream.read(chunk_size)
                if not chunk:
                    break
                out_f.write(chunk)
                bytes_written += len(chunk)

                # If the user has requested a cancellation while writing, abort immediately.
                db.session.refresh(job)
                if job.status == 'cancelled':
                    out_f.flush()
                    raise RuntimeError('Upload cancelled by user')

                # Update Job progress periodically (every ~0.5s or when done)
                now_ts = time.time()
                if total_bytes > 0:
                    prog = min(100.0, (bytes_written / float(max(1, total_bytes))) * 100.0 * 0.8)
                else:
                    # Unknown total length — use heuristics: show receiving step at 10%..80%
                    prog = min(80.0, 5.0 + (bytes_written / (1024 * 1024)) * 0.5)

                if now_ts - last_update > 0.5 or bytes_written == total_bytes:
                    job.current_step = 'Receiving upload'
                    job.current_message = f"Receiving {bytes_written} bytes"
                    job.progress = prog
                    db.session.commit()
                    last_update = now_ts

        video.original_size = os.path.getsize(save_path)
        job.status = 'queued'
        job.current_step = 'Queued for processing'
        job.current_message = 'Upload complete, awaiting background processing'
        job.progress = 100.0
        db.session.commit()

        return jsonify({'message': 'Upload saved', 'size': video.original_size}), 201
    except Exception as e:
        return jsonify({'error': f'Failed to save upload: {str(e)}'}), 500


@api_bp.route('/videos/<video_id>', methods=['DELETE'])
@login_required
def delete_video_api(video_id):
    video = Video.query.get_or_404(video_id)

    # Queue background deletion job
    job = Job(
        video_id=video.id,
        job_type='delete_video',
        status='queued',
        current_step='Queued Deletion',
        current_message='Deletion job queued and awaiting worker execution'
    )
    video.status = 'delete_pending'
    db.session.add(job)
    db.session.commit()

    log = JobLog(
        job_id=job.id,
        timestamp=datetime.now(timezone.utc),
        level='INFO',
        message='Deletion job queued and awaiting worker execution'
    )
    db.session.add(log)
    db.session.commit()

    return jsonify({
        'message': 'Video deletion queued successfully',
        'job_id': job.id,
        'job_url': url_for('views.job_detail', job_id=job.id)
    }), 202


@api_bp.route('/jobs/<job_id>', methods=['GET'])
@login_required
def get_job_api(job_id):
    job = Job.query.get_or_404(job_id)
    return jsonify(job.to_dict(include_logs=False)), 200


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


@api_bp.route('/cdn-accounts', methods=['POST'])
@login_required
def create_cdn_account():
    """
    Create a new CDN account with an encrypted API key.
    Expects JSON: { name, provider, api_key }
    """
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


# Module-level caches and state for stats monitoring
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
            with open("/etc/os-release", "r") as f:
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
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if "model name" in line:
                        cpu_model = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
    if cpu_model == "Unknown Processor":
        cpu_model = platform.processor() or "x86_64 Processor"

    physical_cores = psutil.cpu_count(logical=False) or 48
    logical_threads = psutil.cpu_count(logical=True) or 92

    _cached_static_sys_info = {
        'hostname': hostname,
        'os': os_name,
        'kernel': kernel,
        'arch': arch,
        'cpu_model': cpu_model,
        'physical_cores': physical_cores,
        'logical_threads': logical_threads
    }
    return _cached_static_sys_info


def calculate_network_speeds():
    global _last_net_io, _last_net_time
    now = time.time()
    try:
        net_io = psutil.net_io_counters()
    except Exception:
        return {
            'download_speed_bps': 0.0,
            'upload_speed_bps': 0.0,
            'total_received_bytes': 0,
            'total_sent_bytes': 0
        }

    if _last_net_io is None or _last_net_time is None:
        download_speed = 0.0
        upload_speed = 0.0
    else:
        dt = max(now - _last_net_time, 0.001)
        download_diff = max(0, net_io.bytes_recv - _last_net_io.bytes_recv)
        upload_diff = max(0, net_io.bytes_sent - _last_net_io.bytes_sent)
        download_speed = download_diff / dt
        upload_speed = upload_diff / dt

    _last_net_io = net_io
    _last_net_time = now

    return {
        'download_speed_bps': round(download_speed, 2),
        'upload_speed_bps': round(upload_speed, 2),
        'total_received_bytes': net_io.bytes_recv,
        'total_sent_bytes': net_io.bytes_sent
    }


def get_cpu_temperature():
    try:
        if hasattr(psutil, "sensors_temperatures"):
            temps = psutil.sensors_temperatures()
            if temps:
                for key in ('coretemp', 'k10temp', 'cpu_thermal', 'zenpower', 'acpitz'):
                    if key in temps and temps[key]:
                        for sensor in temps[key]:
                            if hasattr(sensor, 'current') and sensor.current is not None and sensor.current > 0:
                                return round(sensor.current, 1)
                for entries in temps.values():
                    for sensor in entries:
                        if hasattr(sensor, 'current') and sensor.current is not None and sensor.current > 0:
                            return round(sensor.current, 1)
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
            loads = os.getloadavg()
            return [round(l, 2) for l in loads]
        elif hasattr(psutil, "getloadavg"):
            loads = psutil.getloadavg()
            return [round(l, 2) for l in loads]
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
        partitions = psutil.disk_partitions(all=False)
        seen = set()
        for p in partitions:
            if p.fstype in ('squashfs', 'iso9660', 'tmpfs', 'devtmpfs') or 'loop' in p.device:
                continue
            if p.mountpoint in seen:
                continue
            try:
                usage = psutil.disk_usage(p.mountpoint)
                seen.add(p.mountpoint)
                mounts.append({
                    'mount': p.mountpoint,
                    'device': p.device,
                    'fstype': p.fstype,
                    'total_bytes': usage.total,
                    'used_bytes': usage.used,
                    'free_bytes': usage.free,
                    'usage_percent': usage.percent
                })
            except Exception:
                pass
    except Exception:
        pass
    return mounts


@api_bp.route('/system/stats', methods=['GET'])
@login_required
def get_system_stats():
    """
    Live server resource and application monitoring endpoint.
    """
    sys_info = get_static_sys_info()
    now_ts = time.time()
    boot_time = psutil.boot_time() if hasattr(psutil, 'boot_time') else now_ts
    uptime_sec = max(0, int(now_ts - boot_time))

    # CPU Stats
    cpu_percent = psutil.cpu_percent(interval=None)
    per_core = psutil.cpu_percent(percpu=True) or []
    cpu_temp = get_cpu_temperature()
    cpu_freq = get_cpu_frequency()
    load_avg = get_load_averages()

    # RAM & Swap Stats
    ram = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Disk Stats
    upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')
    os.makedirs(upload_folder, exist_ok=True)
    main_disk = shutil.disk_usage(upload_folder)
    temp_folder_size = get_cached_temp_folder_size(upload_folder)
    disk_mounts = get_disk_mounts()

    # Network Speeds
    net_stats = calculate_network_speeds()

    # Active Job & Queue Stats
    active_jobs_count = Job.query.filter_by(status='processing').count()
    queued_jobs_count = Job.query.filter_by(status='queued').count()
    completed_jobs_count = Job.query.filter_by(status='completed').count()
    failed_jobs_count = Job.query.filter_by(status='failed').count()
    total_jobs_count = Job.query.count()

    active_jobs = Job.query.filter_by(status='processing').order_by(Job.started_at.desc()).all()
    active_job_data = None
    active_jobs_data = []
    for active_job in active_jobs:
        job_data = active_job.to_dict(include_logs=False)
        if active_job.video:
            job_data['video_title'] = active_job.video.title
        if active_job.started_at:
            started_ts = active_job.started_at.timestamp()
            job_data['elapsed_seconds'] = round(max(0, now_ts - started_ts), 1)
        active_jobs_data.append(job_data)

    if active_jobs_data:
        active_job_data = active_jobs_data[0]

    # Settings
    ffmpeg_threads = Setting.get('ffmpeg_threads', str(current_app.config.get('DEFAULT_FFMPEG_THREADS', 40)))
    max_concurrent = Setting.get('max_concurrent_jobs', str(current_app.config.get('MAX_CONCURRENT_JOBS', 1)))

    # Assets & CDN
    video_count = Video.query.count()
    variant_count = VideoVariant.query.count()
    cdn_file_count = VideoFile.query.count()
    cdn_storage_used = sum(f.file_size or 0 for f in VideoFile.query.filter_by(upload_status='uploaded').all())

    cdn_accounts = CDNAccount.query.filter_by(enabled=True).all()
    cdn_stats = [acc.to_dict(include_storage=True) for acc in cdn_accounts]
    total_avail_cdn_bytes = sum(acc['storage']['available_bytes'] for acc in cdn_stats)

    # Capacity Calculator (based on standard bitrates: 1440p ~8Mbps, 1080p ~4.5Mbps, 720p ~2.5Mbps)
    capacity_calc = {
        'hours_1440p': round(total_avail_cdn_bytes / (8 * 1000 * 1000 / 8 * 3600), 1),
        'hours_1080p': round(total_avail_cdn_bytes / (4.5 * 1000 * 1000 / 8 * 3600), 1),
        'hours_720p': round(total_avail_cdn_bytes / (2.5 * 1000 * 1000 / 8 * 3600), 1),
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
            'usage_percent': round((main_disk.used / main_disk.total) * 100, 1) if main_disk.total else 0.0,
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
