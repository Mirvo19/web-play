import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'default-dev-secret-key-change-in-prod')
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'default-jwt-secret-key-change-in-prod')
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=24)
    JWT_TOKEN_LOCATION = ['headers', 'cookies']
    JWT_HEADER_NAME = 'Authorization'
    JWT_HEADER_TYPE = 'Bearer'
    JWT_COOKIE_SECURE = False  # Set True in HTTPS production
    JWT_COOKIE_CSRF_PROTECT = False

    # Database
    db_url = os.environ.get('DATABASE_URL', 'sqlite:///app.db')
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # CDN Encryption
    CDN_ENCRYPTION_KEY = os.environ.get('CDN_ENCRYPTION_KEY', '')

    # Supabase Auth
    SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
    SUPABASE_ANON_KEY = os.environ.get('SUPABASE_ANON_KEY', '')
    SUPABASE_SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')

    # Video Processing
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', '/tmp/video-processing')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024 * 1024  # 16 GB max streaming payload
    HLS_SEGMENT_DURATION = int(os.environ.get('HLS_SEGMENT_DURATION', 6))
    DEFAULT_FFMPEG_THREADS = int(os.environ.get('DEFAULT_FFMPEG_THREADS', 40))
    MAX_CONCURRENT_JOBS = int(os.environ.get('MAX_CONCURRENT_JOBS', 2))
    # Whether to also echo job logs and upload events to stdout (for local debugging)
    LOG_TO_STDOUT = os.environ.get('LOG_TO_STDOUT', 'false').lower() in ('1', 'true', 'yes')

    # App version metadata used in UI footers and tooltips
    APP_VERSION = os.environ.get('APP_VERSION', 'v1.0.0-patch-1')
