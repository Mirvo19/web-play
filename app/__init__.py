import atexit
import logging
import os
import shutil

from flask import Flask

from app.config import Config
from app.models import Setting, CDNAccount, db

log = logging.getLogger(__name__)


def _configure_logging(app: Flask) -> None:
    """Single structured logging setup (stdlib only). Full JSON options land in Stage 3."""
    level = logging.DEBUG if app.debug else logging.INFO
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    else:
        logging.getLogger().setLevel(level)
    # Quiet noisy third-party loggers one notch.
    for noisy in ("werkzeug", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def create_app(config_class=Config):
    from app.auth import auth_bp
    from app.routes.api import api_bp
    from app.routes.views import views_bp

    app = Flask(__name__)
    app.config.from_object(config_class)

    _configure_logging(app)

    # Initialize extensions
    db.init_app(app)

    # Inject built-in helpers into Jinja templates
    app.jinja_env.globals.update(
        round=round,
        min=min,
        max=max,
        int=int,
        app_version=app.config.get('APP_VERSION', 'v1.0.0-patch-1')
    )

    # Register Blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp)

    # Initialize DB & Default Settings inside App Context
    with app.app_context():
        db.create_all()

        # Set default processing settings if missing
        if not Setting.get('ffmpeg_threads'):
            Setting.set('ffmpeg_threads', str(app.config.get('DEFAULT_FFMPEG_THREADS', 40)))
        if not Setting.get('max_concurrent_jobs'):
            Setting.set('max_concurrent_jobs', str(app.config.get('MAX_CONCURRENT_JOBS', 1)))
        if not Setting.get('ffmpeg_preset'):
            Setting.set('ffmpeg_preset', 'veryfast')
        if not Setting.get('ffmpeg_crf'):
            Setting.set('ffmpeg_crf', '23')
        if not Setting.get('hls_segment_duration'):
            Setting.set('hls_segment_duration', str(app.config.get('HLS_SEGMENT_DURATION', 6)))

        # Create default CDN account if none exist
        if CDNAccount.query.count() == 0:
            default_cdn = CDNAccount(
                name="Primary Hack Club CDN",
                provider="Hack Club CDN"
            )
            default_cdn.set_api_key("hackclub_default_demo_api_key")
            db.session.add(default_cdn)
            db.session.commit()

        # Discover FFmpeg and FFprobe executables (use absolute paths when available)
        ffmpeg_path = os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
        ffprobe_path = os.environ.get('FFPROBE_BIN') or shutil.which('ffprobe')
        app.config['FFMPEG_BINARY'] = ffmpeg_path or 'ffmpeg'
        app.config['FFPROBE_BINARY'] = ffprobe_path or 'ffprobe'

        # Persist discovered paths into settings for visibility (non-blocking)
        try:
            Setting.set('ffmpeg_path', ffmpeg_path or '')
            Setting.set('ffprobe_path', ffprobe_path or '')
        except Exception as exc:
            # Do not fail startup for inability to persist settings
            log.warning("Could not persist ffmpeg paths to settings: %s", exc)

    # --- In-process job supervisor (replaces the standalone worker.py) ---
    # The web process owns its background work now: job polling, ffmpeg
    # supervision, CDN upload and cleanup all run on executor threads in
    # this process. Disable explicitly with ENABLE_JOB_SCHEDULER=false
    # (e.g. for a pure test client, or all-but-one gunicorn worker if you
    # scale past one process — the atomic claim keeps that safe anyway).
    enable_scheduler = str(
        app.config.get('ENABLE_JOB_SCHEDULER', os.environ.get('ENABLE_JOB_SCHEDULER', 'true'))
    ).lower() in ('1', 'true', 'yes')
    testing = bool(app.config.get('TESTING', False))
    supervisor = None
    if enable_scheduler and not testing:
        from app.worker.supervisor import JobSupervisor

        poll_interval = float(os.environ.get('JOB_POLL_INTERVAL', '2.0'))
        max_workers = int(app.config.get('MAX_CONCURRENT_JOBS', 1))
        supervisor = JobSupervisor(app, poll_interval=poll_interval, max_workers=max_workers)
        supervisor.start()
        log.info("In-process job supervisor attached to app (workers=%d).", max_workers)
    else:
        log.info(
            "Job supervisor NOT started (ENABLE_JOB_SCHEDULER=%s, TESTING=%s).",
            enable_scheduler,
            testing,
        )
    app.extensions['job_supervisor'] = supervisor

    def _shutdown_supervisor() -> None:
        sup = app.extensions.get('job_supervisor')
        if sup is not None and getattr(sup, 'running', False):
            log.info("atexit: stopping in-process job supervisor...")
            sup.stop()

    # Registered once per process; stop() is idempotent.
    atexit.register(_shutdown_supervisor)

    return app
