import os
import shutil
import psutil
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from werkzeug.utils import secure_filename
from app.auth import login_required
from app.models import db, Video, Job, CDNAccount, Setting, JobLog, StorageSnapshot
from app.cdn.manager import CDNManager

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
        current_message='Deletion job queued'
    )
    video.status = 'delete_pending'
    db.session.add(job)
    db.session.commit()

    return jsonify({
        'message': 'Video deletion queued successfully',
        'job_id': job.id
    }), 202


@api_bp.route('/cdn-accounts', methods=['GET', 'POST'])
@login_required
def handle_cdn_accounts():
    if request.method == 'GET':
        accounts = CDNAccount.query.order_by(CDNAccount.created_at.desc()).all()
        return jsonify([acc.to_dict(include_storage=True) for acc in accounts])

    # POST - Add new CDN account
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    provider = data.get('provider', 'Hack Club CDN').strip()
    api_key = data.get('api_key', '').strip()

    if not name or not api_key:
        return jsonify({'error': 'Name and API Key are required'}), 400

    account = CDNAccount(name=name, provider=provider)
    account.set_api_key(api_key)
    db.session.add(account)
    db.session.commit()

    # Test connection
    success, msg = CDNManager.test_account(account)
    return jsonify({
        'message': 'CDN Account saved successfully',
        'account': account.to_dict(include_storage=True),
        'connection_test': {'success': success, 'message': msg}
    }), 201


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


@api_bp.route('/system/stats', methods=['GET'])
@login_required
def get_system_stats():
    """
    Live server resource monitoring for 48 cores / 92 threads, 2 GB RAM, 16 GB Disk.
    """
    # CPU Stats
    cpu_percent = psutil.cpu_percent(interval=None)
    physical_cores = psutil.cpu_count(logical=False) or 48
    logical_threads = psutil.cpu_count(logical=True) or 92
    per_cpu = psutil.cpu_percent(percpu=True) or []

    # RAM Stats
    ram = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Disk Stats
    upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp/video-processing')
    os.makedirs(upload_folder, exist_ok=True)
    disk = shutil.disk_usage(upload_folder)

    temp_folder_size = 0
    if os.path.exists(upload_folder):
        for root, dirs, files in os.walk(upload_folder):
            temp_folder_size += sum(os.path.getsize(os.path.join(root, name)) for name in files)

    # Active FFmpeg Job Stats
    active_job = Job.query.filter_by(status='processing').first()
    active_job_data = active_job.to_dict(include_logs=True) if active_job else None

    # CDN Accounts Breakdown
    cdn_accounts = CDNAccount.query.filter_by(enabled=True).all()
    cdn_stats = [acc.to_dict(include_storage=True) for acc in cdn_accounts]

    total_avail_cdn_bytes = sum(acc['storage']['available_bytes'] for acc in cdn_stats)

    # Capacity Calculator Estimates (based on standard bitrates: 1440p ~8Mbps, 1080p ~4.5Mbps, 720p ~2.5Mbps)
    capacity_calc = {
        'hours_1440p': round(total_avail_cdn_bytes / (8 * 1000 * 1000 / 8 * 3600), 1),
        'hours_1080p': round(total_avail_cdn_bytes / (4.5 * 1000 * 1000 / 8 * 3600), 1),
        'hours_720p': round(total_avail_cdn_bytes / (2.5 * 1000 * 1000 / 8 * 3600), 1),
    }

    return jsonify({
        'cpu': {
            'usage_percent': cpu_percent,
            'physical_cores': physical_cores,
            'logical_threads': logical_threads,
            'per_core': per_cpu[:12]  # return summary of core load
        },
        'ram': {
            'total_bytes': ram.total,
            'used_bytes': ram.used,
            'available_bytes': ram.available,
            'percent': ram.percent,
            'swap_used': swap.used,
            'swap_total': swap.total
        },
        'disk': {
            'total_bytes': disk.total,
            'used_bytes': disk.used,
            'free_bytes': disk.free,
            'temp_folder_size': temp_folder_size
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
