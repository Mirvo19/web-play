from flask import Blueprint, render_template, redirect, url_for, request
from app.auth import login_required
from app.models import Video, Job, CDNAccount, Setting

views_bp = Blueprint('views', __name__)

@views_bp.route('/')
@login_required
def index():
    return redirect(url_for('views.dashboard'))

@views_bp.route('/dashboard')
@login_required
def dashboard():
    videos = Video.query.order_by(Video.created_at.desc()).all()
    active_jobs = Job.query.filter_by(status='processing').count()
    cdn_accounts = CDNAccount.query.filter_by(enabled=True).all()
    
    total_cdn_bytes = sum(acc.get_latest_storage()['total_bytes'] for acc in cdn_accounts) or (50 * 1024 * 1024 * 1024)
    used_cdn_bytes = sum(acc.get_latest_storage()['used_bytes'] for acc in cdn_accounts)
    available_cdn_bytes = max(0, total_cdn_bytes - used_cdn_bytes)

    return render_template(
        'dashboard.html',
        videos=videos,
        active_jobs=active_jobs,
        cdn_accounts=cdn_accounts,
        total_cdn_bytes=total_cdn_bytes,
        used_cdn_bytes=used_cdn_bytes,
        available_cdn_bytes=available_cdn_bytes
    )

@views_bp.route('/upload')
@login_required
def upload():
    cdn_accounts = CDNAccount.query.filter_by(enabled=True).all()
    accounts_info = [acc.to_dict(include_storage=True) for acc in cdn_accounts]
    return render_template('upload.html', cdn_accounts=accounts_info)

@views_bp.route('/watch/<video_id>')
@login_required
def watch(video_id):
    video = Video.query.get_or_404(video_id)
    return render_template('watch.html', video=video)

@views_bp.route('/jobs')
@login_required
def jobs_list():
    jobs = Job.query.order_by(Job.created_at.desc()).all()
    return render_template('jobs.html', jobs=jobs)

@views_bp.route('/jobs/<job_id>')
@login_required
def job_detail(job_id):
    job = Job.query.get_or_404(job_id)
    return render_template('job_detail.html', job=job)

@views_bp.route('/cdn-accounts')
@login_required
def cdn_accounts():
    accounts = CDNAccount.query.order_by(CDNAccount.created_at.desc()).all()
    accounts_info = [acc.to_dict(include_storage=True) for acc in accounts]
    return render_template('cdn_accounts.html', cdn_accounts=accounts_info)

@views_bp.route('/stats')
@login_required
def stats():
    return render_template('stats.html')

@views_bp.route('/settings')
@login_required
def settings():
    current_settings = {
        'ffmpeg_threads': Setting.get('ffmpeg_threads', '40'),
        'max_concurrent_jobs': Setting.get('max_concurrent_jobs', '1'),
        'ffmpeg_preset': Setting.get('ffmpeg_preset', 'veryfast'),
        'ffmpeg_crf': Setting.get('ffmpeg_crf', '23'),
        'hls_segment_duration': Setting.get('hls_segment_duration', '6')
    }
    return render_template('settings.html', settings=current_settings)
