"""Regression tests for the unified single-process rebuild.

Covers: fail-loud startup, fail-closed auth, validated settings,
jailed/size-limited uploads, atomic-claim supervisor lifecycle,
and health endpoint status codes.
"""

import io
import os
import tempfile
import unittest

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_ANON_KEY", "")

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.startup import StartupError  # noqa: E402


def _test_config(**overrides):
    attrs = dict(
        TESTING=True,
        SECRET_KEY="s" * 40,
        JWT_SECRET_KEY="j" * 40,
        CDN_ENCRYPTION_KEY="c" * 40,
        SUPABASE_URL="",
        SUPABASE_ANON_KEY="",
        ALLOW_INSECURE_DEV_AUTH=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        UPLOAD_FOLDER=tempfile.mkdtemp(),
        ENABLE_JOB_SCHEDULER="false",
    )
    attrs.update(overrides)
    return type("TestConfig", (Config,), attrs)


def _authed_client(app):
    from app.auth import generate_session_jwt

    with app.app_context():
        token, _ = generate_session_jwt("u1", "a@x.com")
    client = app.test_client()
    client.set_cookie("access_token", token)
    return client


class StartupTests(unittest.TestCase):
    def test_production_refuses_placeholder_secrets(self):
        cfg = _test_config(TESTING=False, FLASK_ENV="production",
                           SECRET_KEY="change-this-to-a-secure-random-secret-key")
        with self.assertRaises(StartupError):
            create_app(cfg)

    def test_production_refuses_dev_auth_flag(self):
        cfg = _test_config(TESTING=False, FLASK_ENV="production",
                           ALLOW_INSECURE_DEV_AUTH=True)
        with self.assertRaises(StartupError):
            create_app(cfg)

    def test_production_refuses_missing_ffmpeg_with_scheduler(self):
        cfg = _test_config(TESTING=False, FLASK_ENV="production",
                           ENABLE_JOB_SCHEDULER="true",
                           FFMPEG_BINARY="/nonexistent/ffmpeg")
        with self.assertRaises(StartupError):
            create_app(cfg)


class AuthFailClosedTests(unittest.TestCase):
    def test_login_refused_without_auth_config(self):
        app = create_app(_test_config(ALLOW_INSECURE_DEV_AUTH=False))
        resp = app.test_client().post(
            "/login", json={"email": "anyone@x.com", "password": "123456"})
        self.assertEqual(resp.status_code, 503)

    def test_dev_fallback_loopback_only(self):
        app = create_app(_test_config(ALLOW_INSECURE_DEV_AUTH=True))
        client = app.test_client()
        ok = client.post("/login", json={"email": "d@x.com", "password": "123456"})
        self.assertEqual(ok.status_code, 200)
        remote = client.post("/login", json={"email": "d@x.com", "password": "123456"},
                             environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
        self.assertEqual(remote.status_code, 403)


class SettingsValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_test_config())
        self.client = _authed_client(self.app)

    def test_rejects_bad_values_with_400(self):
        for payload in ({"ffmpeg_threads": "abc"},
                        {"ffmpeg_preset": "; rm -rf /"},
                        {"ffmpeg_crf": 999},
                        {"hls_segment_duration": 99},
                        {"max_concurrent_jobs": 99}):
            resp = self.client.post("/api/settings", json=payload)
            self.assertEqual(resp.status_code, 400, payload)

    def test_accepts_good_values(self):
        resp = self.client.post("/api/settings", json={
            "ffmpeg_threads": 16, "ffmpeg_preset": "fast",
            "ffmpeg_crf": 24, "hls_segment_duration": 4,
            "max_concurrent_jobs": 2})
        self.assertEqual(resp.status_code, 200)

    def test_builder_rejects_raw_preset(self):
        from app.worker.ffmpeg_processor import build_ffmpeg_transcode_command
        with self.assertRaises(ValueError):
            build_ffmpeg_transcode_command(
                "in.mp4", tempfile.mkdtemp(),
                {"width": 128, "height": 72, "label": "t"},
                {"fps": 30}, preset="evil;touch /tmp/pwned")


class UploadHardeningTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_test_config(MAX_UPLOAD_SIZE_GB=0.00001))
        self.client = _authed_client(self.app)
        with self.app.app_context():
            from app.models import CDNAccount
            self.account_id = CDNAccount.query.first().id

    def test_traversal_filename_is_jailed(self):
        resp = self.client.post(
            "/api/videos/upload",
            data={"file": (io.BytesIO(b"x" * 100), "../../evil.mp4"),
                  "title": "t", "cdn_account_id": self.account_id},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 202)
        with self.app.app_context():
            from app.models import Video
            video = Video.query.first()
            self.assertEqual(video.original_filename, "evil.mp4")
            self.assertNotIn("..", video.original_filename)
            self.assertNotIn("/", video.original_filename)

    def test_oversize_rejected_on_actual_bytes(self):
        resp = self.client.post(
            "/api/videos/upload",
            data={"file": (io.BytesIO(b"y" * 100000), "big.mp4"),
                  "title": "t", "cdn_account_id": self.account_id},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 413)


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_test_config())
        self.client = _authed_client(self.app)
        with self.app.app_context():
            from app.models import Video, Job, db
            self.stuck = Video(title="stuck", status="processing")
            db.session.add(self.stuck)
            db.session.commit()
            job = Job(video_id=self.stuck.id, job_type="transcode_and_upload",
                      status="queued")
            db.session.add(job)
            db.session.commit()
            self.ready = Video(title="live", status="ready")
            db.session.add(self.ready)
            db.session.commit()
            self.stuck_id, self.ready_id = self.stuck.id, self.ready.id

    def test_purge_stuck_video_cascades(self):
        resp = self.client.delete(f"/api/videos/{self.stuck_id}/metadata")
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            from app.models import Video, Job
            self.assertIsNone(db_session_get(Video, self.stuck_id))
            self.assertEqual(Job.query.filter_by(video_id=self.stuck_id).count(), 0)

    def test_purge_ready_video_refused(self):
        resp = self.client.delete(f"/api/videos/{self.ready_id}/metadata")
        self.assertEqual(resp.status_code, 409)

    def test_purge_missing_video_404(self):
        resp = self.client.delete("/api/videos/does-not-exist/metadata")
        self.assertEqual(resp.status_code, 404)

    def test_dashboard_shows_purge_and_theme_toggle(self):
        html = self.client.get("/dashboard").data.decode()
        self.assertIn("data-purge-video", html)
        self.assertIn('id="themeToggle"', html)
        self.assertIn('data-theme="dark"', html)

    def test_watch_shows_downloads(self):
        html = self.client.get(f"/watch/{self.ready_id}").data.decode()
        self.assertIn("Download to this computer", html)
        self.assertIn("data-copy-url", html)


def db_session_get(model, key):
    from app.models import db
    return db.session.get(model, key)


class SupervisorTests(unittest.TestCase):
    def test_claim_empty_queue_and_clean_stop(self):
        from app.worker.supervisor import JobSupervisor, claim_next_job

        # File-backed SQLite: all supervisor threads share one database
        # (a :memory: DB gives every thread its own empty database).
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        app = create_app(_test_config(
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{path}"))

        def _cleanup():
            with app.app_context():
                from app.models import db
                db.session.remove()
                db.engine.dispose()
            try:
                os.remove(path)
            except OSError:
                pass
        self.addCleanup(_cleanup)
        supervisor = JobSupervisor(app, poll_interval=5.0, max_workers=1)
        supervisor.start()
        try:
            self.assertTrue(supervisor.running)
            self.assertIsNone(claim_next_job(app))
        finally:
            supervisor.stop(timeout=10)
        self.assertFalse(supervisor.running)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_test_config())

    def test_up_is_fast_200(self):
        resp = self.app.test_client().get("/up")
        self.assertEqual(resp.status_code, 200)

    def test_ready_reports_database(self):
        resp = self.app.test_client().get("/ready")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["database"]["ok"])

    def test_deep_health_returns_json_checks(self):
        resp = self.app.test_client().get("/health")
        body = resp.get_json()
        self.assertIn("checks", body)
        self.assertIn(resp.status_code, (200, 503))


if __name__ == "__main__":
    unittest.main()
