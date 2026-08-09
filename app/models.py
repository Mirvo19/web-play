import uuid
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from app.utils.security import encrypt_api_key, decrypt_api_key, mask_api_key

db = SQLAlchemy()

def generate_uuid():
    return str(uuid.uuid4())

def utc_now():
    return datetime.now(timezone.utc)

class CDNAccount(db.Model):
    __tablename__ = 'cdn_accounts'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    name = db.Column(db.String(100), nullable=False)
    provider = db.Column(db.String(50), nullable=False, default='Hack Club CDN')
    encrypted_credentials = db.Column(db.Text, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    videos = db.relationship('Video', backref='cdn_account', lazy=True)
    files = db.relationship('VideoFile', backref='cdn_account', lazy=True)
    snapshots = db.relationship('StorageSnapshot', backref='cdn_account', lazy=True, cascade='all, delete-orphan')

    def set_api_key(self, api_key: str):
        self.encrypted_credentials = encrypt_api_key(api_key)

    def get_api_key(self) -> str:
        return decrypt_api_key(self.encrypted_credentials)

    @property
    def masked_key(self) -> str:
        raw_key = self.get_api_key()
        return mask_api_key(raw_key)

    def get_latest_storage(self):
        latest = StorageSnapshot.query.filter_by(cdn_account_id=self.id).order_by(StorageSnapshot.checked_at.desc()).first()
        if latest:
            return {
                'used_bytes': latest.used_bytes,
                'available_bytes': latest.available_bytes,
                'total_bytes': latest.total_bytes,
                'checked_at': latest.checked_at.isoformat() if latest.checked_at else None
            }
        # Fallback default (50 GB per CDN account limit)
        cap = 50 * 1024 * 1024 * 1024  # 53,687,091,200 bytes
        used = sum(f.file_size or 0 for f in self.files if f.upload_status == 'uploaded')
        return {
            'used_bytes': used,
            'available_bytes': max(0, cap - used),
            'total_bytes': cap,
            'checked_at': None
        }

    def to_dict(self, include_storage=True):
        data = {
            'id': self.id,
            'name': self.name,
            'provider': self.provider,
            'masked_key': self.masked_key,
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
        if include_storage:
            data['storage'] = self.get_latest_storage()
        return data


class Video(db.Model):
    __tablename__ = 'videos'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    original_filename = db.Column(db.String(255), nullable=True)
    original_size = db.Column(db.BigInteger, default=0)
    encoded_size = db.Column(db.BigInteger, default=0)
    duration = db.Column(db.Float, default=0.0)  # seconds
    source_width = db.Column(db.Integer, default=0)
    source_height = db.Column(db.Integer, default=0)
    source_fps = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(50), default='processing', nullable=False)  # processing, ready, failed, delete_pending, deleted
    cdn_account_id = db.Column(db.String(36), db.ForeignKey('cdn_accounts.id'), nullable=True)
    cdn_prefix = db.Column(db.String(255), nullable=True)
    master_playlist_url = db.Column(db.Text, nullable=True)
    thumbnail_url = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    variants = db.relationship('VideoVariant', backref='video', lazy=True, cascade='all, delete-orphan')
    files = db.relationship('VideoFile', backref='video', lazy=True, cascade='all, delete-orphan')
    jobs = db.relationship('Job', backref='video', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description,
            'original_filename': self.original_filename,
            'original_size': self.original_size,
            'encoded_size': self.encoded_size,
            'duration': self.duration,
            'source_width': self.source_width,
            'source_height': self.source_height,
            'source_fps': self.source_fps,
            'status': self.status,
            'cdn_account_id': self.cdn_account_id,
            'cdn_account_name': self.cdn_account.name if self.cdn_account else 'Unknown',
            'cdn_prefix': self.cdn_prefix,
            'master_playlist_url': self.master_playlist_url,
            'thumbnail_url': self.thumbnail_url,
            'variants': [v.to_dict() for v in self.variants],
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class VideoVariant(db.Model):
    __tablename__ = 'video_variants'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    video_id = db.Column(db.String(36), db.ForeignKey('videos.id'), nullable=False)
    resolution = db.Column(db.String(20), nullable=False)  # e.g. '1440p', '1080p'
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    playlist_url = db.Column(db.Text, nullable=True)
    bitrate = db.Column(db.Integer, default=0)
    file_size = db.Column(db.BigInteger, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now)

    files = db.relationship('VideoFile', backref='variant', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'video_id': self.video_id,
            'resolution': self.resolution,
            'width': self.width,
            'height': self.height,
            'playlist_url': self.playlist_url,
            'bitrate': self.bitrate,
            'file_size': self.file_size,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class VideoFile(db.Model):
    __tablename__ = 'video_files'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    video_id = db.Column(db.String(36), db.ForeignKey('videos.id'), nullable=False)
    video_variant_id = db.Column(db.String(36), db.ForeignKey('video_variants.id'), nullable=True)
    cdn_account_id = db.Column(db.String(36), db.ForeignKey('cdn_accounts.id'), nullable=False)
    remote_path = db.Column(db.Text, nullable=False)  # Remote identifier or path on CDN
    remote_url = db.Column(db.Text, nullable=False)   # Direct HTTP link on CDN
    file_size = db.Column(db.BigInteger, default=0)
    file_type = db.Column(db.String(50), nullable=False)  # segment, playlist, master_playlist, thumbnail
    upload_status = db.Column(db.String(50), default='uploaded', nullable=False)  # uploaded, deleted, failed
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'video_id': self.video_id,
            'video_variant_id': self.video_variant_id,
            'cdn_account_id': self.cdn_account_id,
            'remote_path': self.remote_path,
            'remote_url': self.remote_url,
            'file_size': self.file_size,
            'file_type': self.file_type,
            'upload_status': self.upload_status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'deleted_at': self.deleted_at.isoformat() if self.deleted_at else None
        }


class Job(db.Model):
    __tablename__ = 'jobs'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    video_id = db.Column(db.String(36), db.ForeignKey('videos.id'), nullable=False)
    job_type = db.Column(db.String(50), nullable=False)  # transcode_and_upload, delete_video
    status = db.Column(db.String(50), default='queued', nullable=False)  # queued, processing, completed, failed
    progress = db.Column(db.Float, default=0.0)  # 0.0 to 100.0
    current_step = db.Column(db.String(100), default='Queued')
    current_message = db.Column(db.Text, default='')
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    logs = db.relationship('JobLog', backref='job', lazy=True, cascade='all, delete-orphan', order_by='JobLog.timestamp.asc()')

    def to_dict(self, include_logs=False):
        data = {
            'id': self.id,
            'video_id': self.video_id,
            'job_type': self.job_type,
            'status': self.status,
            'progress': round(self.progress, 1),
            'current_step': self.current_step,
            'current_message': self.current_message,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None
        }
        if include_logs:
            data['logs'] = [log.to_dict() for log in self.logs]
        return data


class JobLog(db.Model):
    __tablename__ = 'job_logs'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    job_id = db.Column(db.String(36), db.ForeignKey('jobs.id'), nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), default=utc_now)
    level = db.Column(db.String(20), default='INFO')
    message = db.Column(db.Text, nullable=False)
    metadata_json = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'job_id': self.job_id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'level': self.level,
            'message': self.message,
            'metadata': self.metadata_json
        }


class Setting(db.Model):
    __tablename__ = 'settings'

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        s = cls.query.get(key)
        return s.value if s else default

    @classmethod
    def set(cls, key: str, value: str):
        s = cls.query.get(key)
        if not s:
            s = cls(key=key, value=str(value))
            db.session.add(s)
        else:
            s.value = str(value)
            s.updated_at = utc_now()
        db.session.commit()


class StorageSnapshot(db.Model):
    __tablename__ = 'storage_snapshots'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    cdn_account_id = db.Column(db.String(36), db.ForeignKey('cdn_accounts.id'), nullable=False)
    used_bytes = db.Column(db.BigInteger, default=0)
    available_bytes = db.Column(db.BigInteger, default=0)
    total_bytes = db.Column(db.BigInteger, default=0)
    checked_at = db.Column(db.DateTime(timezone=True), default=utc_now)
