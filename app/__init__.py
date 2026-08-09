import os
from flask import Flask
from app.config import Config
from app.models import db, Setting, CDNAccount
from app.auth import auth_bp
from app.routes.views import views_bp
from app.routes.api import api_bp
import shutil

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

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

        # NOTE: The background job worker is NOT started here.
        # It runs as a completely separate process via worker.py, managed by
        # hc-cdn-worker.service. This ensures that Gunicorn web workers never
        # accidentally spawn their own processing loops.

        # Discover FFmpeg and FFprobe executables (use absolute paths when available)
        ffmpeg_path = os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg')
        ffprobe_path = os.environ.get('FFPROBE_BIN') or shutil.which('ffprobe')
        app.config['FFMPEG_BINARY'] = ffmpeg_path or 'ffmpeg'
        app.config['FFPROBE_BINARY'] = ffprobe_path or 'ffprobe'

        # Persist discovered paths into settings for visibility (non-blocking)
        try:
            if ffmpeg_path:
                Setting.set('ffmpeg_path', ffmpeg_path)
            else:
                Setting.set('ffmpeg_path', '')
            if ffprobe_path:
                Setting.set('ffprobe_path', ffprobe_path)
            else:
                Setting.set('ffprobe_path', '')
        except Exception:
            # Do not fail startup for inability to persist settings
            pass

    return app
