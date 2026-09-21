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
    active_job_query = Job.query.filter(Job.status.in_(['queued', 'processing']))
    active_job_count = active_job_query.count()
    active_job_list = active_job_query.order_by(Job.created_at.desc()).limit(5).all()
    cdn_accounts = CDNAccount.query.filter_by(enabled=True).all()
    
    total_cdn_bytes = sum(acc.get_latest_storage()['total_bytes'] for acc in cdn_accounts) or (50 * 1024 * 1024 * 1024)
    used_cdn_bytes = sum(acc.get_latest_storage()['used_bytes'] for acc in cdn_accounts)
    available_cdn_bytes = max(0, total_cdn_bytes - used_cdn_bytes)

    return render_template(
        'dashboard.html',
        videos=videos,
        active_jobs=active_job_count,
        active_job_count=active_job_count,
        active_job_list=active_job_list,
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

@views_bp.route('/jobs/<job_id>', strict_slashes=False)
@login_required
def job_detail(job_id):
    job = Job.query.get_or_404(job_id)
    return render_template('job_detail.html', job=job)

@views_bp.route('/cdn-accounts')
@login_required
def cdn_accounts():
    from flask import current_app
    from app.cdn import supabase_store

    accounts = CDNAccount.query.order_by(CDNAccount.created_at.desc()).all()
    accounts_info = [acc.to_dict(include_storage=True) for acc in accounts]

    # Supabase is the primary mirror; local DB is the fallback. Surface
    # which source served this page so staleness is never silent. While the
    # mirror is reachable, lazily push any local accounts missing remotely
    # (self-repair after an outage).
    if not supabase_store.configured():
        source, detail = 'local-only', 'Supabase mirror not configured (need SUPABASE_SERVICE_ROLE_KEY + cdn_accounts table)'
    else:
        ok, payload = supabase_store.fetch_remote()
        if ok:
            missing = [a for a in accounts if a.id not in supabase_store.remote_ids(payload)]
            for acc in missing:
                supabase_store.push_account(acc)
            source = 'supabase'
            detail = f"mirrored ({len(payload)} remote accounts" + (f", {len(missing)} re-pushed" if missing else "") + ")"
        else:
            source = 'local-fallback'
            detail = f"Supabase unreachable — serving local copy ({payload})"
            current_app.logger.warning("CDN page served from local fallback: %s", payload)

    return render_template('cdn_accounts.html', cdn_accounts=accounts_info,
                           cdn_source=source, cdn_source_detail=detail)

@views_bp.route('/torrents')
@login_required
def torrents():
    cdn_accounts = CDNAccount.query.filter_by(enabled=True).all()
    accounts_info = [acc.to_dict(include_storage=False) for acc in cdn_accounts]
    return render_template('torrents.html', cdn_accounts=accounts_info)

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
        'hls_segment_duration': Setting.get('hls_segment_duration', '6'),
        'torrent_enabled': Setting.get('torrent_enabled', 'true'),
        'torrent_max_concurrent': Setting.get('torrent_max_concurrent', '1'),
        'torrent_max_total_mb': Setting.get('torrent_max_total_mb', '4096'),
        'torrent_max_peers': Setting.get('torrent_max_peers', '50'),
        'torrent_bandwidth_kbps': Setting.get('torrent_bandwidth_kbps', '0'),
        'torrent_timeout_sec': Setting.get('torrent_timeout_sec', '7200'),
        'torrent_metadata_timeout_sec': Setting.get('torrent_metadata_timeout_sec', '120'),
    }
    return render_template('settings.html', settings=current_settings)
