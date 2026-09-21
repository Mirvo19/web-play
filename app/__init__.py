import atexit
import logging
import os
import shutil

from flask import Flask

from app.config import Config
from app.models import Setting, CDNAccount, db
from app.startup import validate_config

log = logging.getLogger(__name__)


def _configure_logging(app: Flask) -> None:
    """One stdlib logging setup for the whole process.

    Format is a stable single line (timestamp, level, logger, message) so
    journald / log aggregators can parse it. Level via LOG_LEVEL env.
    """
    level_name = str(os.environ.get("LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    if app.debug:
        level = logging.DEBUG
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
    ))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Quiet noisy third-party loggers one notch.
    for noisy in ("werkzeug", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def create_app(config_class=Config):
    from app.auth import auth_bp
    from app.routes.api import api_bp
    from app.routes.health import health_bp
    from app.routes.torrents import torrents_bp
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

    # Register Blueprints (health endpoints carry no auth — load balancers)
    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(torrents_bp)
    app.register_blueprint(health_bp)

    # Discover FFmpeg and FFprobe executables BEFORE validation, so the
    # startup check inspects the same paths the pipeline will use.
    ffmpeg_path = os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
    ffprobe_path = os.environ.get('FFPROBE_BIN') or shutil.which('ffprobe')
    app.config['FFMPEG_BINARY'] = ffmpeg_path or 'ffmpeg'
    app.config['FFPROBE_BINARY'] = ffprobe_path or 'ffprobe'

    enable_scheduler = str(
        app.config.get('ENABLE_JOB_SCHEDULER', os.environ.get('ENABLE_JOB_SCHEDULER', 'true'))
    ).lower() in ('1', 'true', 'yes')
    testing = bool(app.config.get('TESTING', False))
    # TESTING never runs ffmpeg: validate/start as scheduler-disabled.
    scheduler_effective = enable_scheduler and not testing

    # Fail loudly on misconfiguration — never degrade silently at request time.
    validate_config(app, scheduler_enabled=scheduler_effective)

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

        # Torrent ingestion defaults (tweakable via Settings UI)
        _torrent_defaults = {
            'torrent_enabled': str(app.config.get('TORRENT_ENABLED', 'true')),
            'torrent_max_concurrent': str(app.config.get('TORRENT_MAX_CONCURRENT', 1)),
            'torrent_max_total_mb': str(app.config.get('TORRENT_MAX_TOTAL_MB', 4096)),
            'torrent_max_peers': str(app.config.get('TORRENT_MAX_PEERS', 50)),
            'torrent_bandwidth_kbps': str(app.config.get('TORRENT_BANDWIDTH_KBPS', 0)),
            'torrent_timeout_sec': str(app.config.get('TORRENT_TIMEOUT_SEC', 7200)),
            'torrent_metadata_timeout_sec': str(app.config.get('TORRENT_METADATA_TIMEOUT_SEC', 120)),
        }
        for key, value in _torrent_defaults.items():
            if not Setting.get(key):
                Setting.set(key, value)

        # Create default CDN account if none exist
        if CDNAccount.query.count() == 0:
            default_cdn = CDNAccount(
                name="Primary Hack Club CDN",
                provider="Hack Club CDN"
            )
            default_cdn.set_api_key("hackclub_default_demo_api_key")
            db.session.add(default_cdn)
            db.session.commit()

        # Persist discovered paths into settings for visibility (non-blocking)
        try:
            Setting.set('ffmpeg_path', ffmpeg_path or '')
            Setting.set('ffprobe_path', ffprobe_path or '')
        except Exception as exc:
            log.warning("Could not persist ffmpeg paths to settings: %s", exc)

    # --- In-process job supervisor (replaces the standalone worker.py) ---
    # The web process owns its background work now: job polling, ffmpeg
    # supervision, CDN upload and cleanup all run on executor threads in
    # this process. Disable explicitly with ENABLE_JOB_SCHEDULER=false
    # (e.g. for a pure test client, or all-but-one process if you scale
    # past one gunicorn worker — the atomic claim keeps that safe anyway).
    testing = bool(app.config.get('TESTING', False))
    supervisor = None
    if scheduler_effective:
        from app.worker.supervisor import JobSupervisor

        poll_interval = float(app.config.get('JOB_POLL_INTERVAL', 2.0))
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

    # --- Torrent coordinator (metadata fetch + selective download) ---
    # Runs beside the job supervisor in the same process; the actual
    # peer-network work happens in scrubbed aria2c children (see
    # app/torrents/engine.py). Idle when torrent_enabled=false.
    # Auxiliary subsystem: if its scratch space is unusable (e.g. a
    # read-only quarantine path under a hardened unit), degrade to
    # torrents-unavailable instead of killing web + supervisor boot.
    torrent_coord = None
    if scheduler_effective:
        from app.torrents.coordinator import TorrentCoordinator

        try:
            torrent_coord = TorrentCoordinator(app)
            torrent_coord.start()
            log.info("Torrent coordinator attached to app.")
        except Exception:
            log.exception(
                "Torrent coordinator failed to start; torrent ingestion "
                "disabled, web + supervisor continuing.")
            torrent_coord = None
    app.extensions['torrent_coordinator'] = torrent_coord

    def _shutdown_supervisor() -> None:
        sup = app.extensions.get('job_supervisor')
        if sup is not None and getattr(sup, 'running', False):
            log.info("atexit: stopping in-process job supervisor...")
            sup.stop()
        coord = app.extensions.get('torrent_coordinator')
        if coord is not None and getattr(coord, 'running', False):
            log.info("atexit: stopping torrent coordinator...")
            coord.stop()

    # Registered once per process; stop() is idempotent.
    atexit.register(_shutdown_supervisor)

    return app
