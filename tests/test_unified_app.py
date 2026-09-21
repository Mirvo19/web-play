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


class ChunkedUploadTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_test_config(MAX_UPLOAD_SIZE_GB=1))
        self.client = _authed_client(self.app)
        with self.app.app_context():
            from app.models import CDNAccount
            self.account_id = CDNAccount.query.first().id

    def _init(self, size=300):
        resp = self.client.post("/api/videos/upload/init", data={
            "title": "chunked", "description": "", "cdn_account_id": self.account_id,
            "filename": "big.bin", "size": str(size)})
        self.assertEqual(resp.status_code, 201)
        return resp.get_json()["video_id"]

    def _put(self, vid, offset, body):
        return self.client.put(
            f"/api/videos/{vid}/upload?offset={offset}", data=body,
            content_type="application/octet-stream")

    def test_chunks_append_and_complete_queues(self):
        from app.models import Job, db

        vid = self._init()
        off = 0
        for _ in range(3):
            resp = self._put(vid, off, b"y" * 100)
            self.assertEqual(resp.status_code, 200)
            off = resp.get_json()["received"]
        self.assertEqual(off, 300)
        resp = self.client.post(f"/api/videos/{vid}/complete")
        self.assertEqual(resp.status_code, 201)
        with self.app.app_context():
            job = Job.query.filter_by(video_id=vid).first()
            self.assertEqual(job.status, "queued")
            self.assertEqual(job.bytes_received, 300)

    def test_offset_mismatch_returns_authoritative_offset(self):
        vid = self._init()
        resp = self._put(vid, 50, b"x" * 10)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["received"], 0)
        # Resume from the returned offset works.
        resp = self._put(vid, 0, b"x" * 10)
        self.assertEqual(resp.status_code, 200)

    def test_short_complete_rejected(self):
        vid = self._init(size=300)
        self._put(vid, 0, b"z" * 100)
        resp = self.client.post(f"/api/videos/{vid}/complete")
        self.assertEqual(resp.status_code, 400)

    def test_empty_complete_rejected(self):
        vid = self._init(size=100)
        resp = self.client.post(f"/api/videos/{vid}/complete")
        self.assertEqual(resp.status_code, 400)

    def test_chunk_over_cap_rejected(self):
        from app.models import Job, db

        # ~1 MB cap via config; a 2 MB-declared upload's second chunk overflows.
        app = create_app(_test_config(MAX_UPLOAD_SIZE_GB=0.001))
        client = _authed_client(app)
        with app.app_context():
            from app.models import CDNAccount
            acc = CDNAccount.query.first().id
        init = client.post("/api/videos/upload/init", data={
            "title": "t", "description": "", "cdn_account_id": acc,
            "filename": "b.bin", "size": "900000"})
        vid = init.get_json()["video_id"]
        resp = client.put(f"/api/videos/{vid}/upload?offset=0", data=b"q" * 1000,
                          content_type="application/octet-stream")
        self.assertEqual(resp.status_code, 200)
        # declared 2MB but cap ~1MB: next chunk exceeds running cap.
        resp = client.put(f"/api/videos/{vid}/upload?offset=1000", data=b"q" * 2000000,
                          content_type="application/octet-stream")
        self.assertEqual(resp.status_code, 413)
        with app.app_context():
            job = Job.query.filter_by(video_id=vid).first()
            # Failed chunk left no trailing bytes behind the recorded offset.
            self.assertEqual(job.bytes_received, 1000)


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

    def test_purge_deletes_orphaned_cdn_files(self):
        from app.cdn.manager import CDNManager
        from app.models import Video, VideoFile, CDNAccount, db

        deleted_remote = []

        class SweepProvider:
            def delete_file(self, ident):
                deleted_remote.append(ident)
                return True, [{"endpoint": "x", "status": 200, "body": '{"deleted": true}'}]

        with self.app.app_context():
            acc = CDNAccount.query.first()
            video = Video(title="half", status="failed", cdn_account_id=acc.id)
            db.session.add(video)
            db.session.commit()
            for i in range(2):
                db.session.add(VideoFile(
                    video_id=video.id, cdn_account_id=acc.id,
                    remote_path=f"orphan-{i}", remote_url=f"http://x/{i}",
                    file_size=10, file_type="segment", upload_status="uploaded"))
            db.session.commit()
            vid = video.id

        orig = CDNManager.get_provider_instance
        CDNManager.get_provider_instance = classmethod(lambda cls, acc: SweepProvider())
        try:
            resp = self.client.delete(f"/api/videos/{vid}/metadata")
        finally:
            CDNManager.get_provider_instance = orig
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["cdn"]["deleted"], 2)
        self.assertEqual(body["cdn"]["failed"], 0)
        self.assertEqual(sorted(deleted_remote), ["orphan-0", "orphan-1"])
        with self.app.app_context():
            from app.models import Video
            self.assertIsNone(db_session_get(Video, vid))

    def test_purge_reports_unowned_leftovers_but_still_purges(self):
        from app.cdn.manager import CDNManager
        from app.models import Video, VideoFile, CDNAccount, db

        class ForeignProvider:
            def delete_file(self, ident):
                return False, [{"endpoint": "https://cdn.hackclub.com/api/v4/upload/x",
                                "status": 404, "body": "no matching resource"}]

        with self.app.app_context():
            acc = CDNAccount.query.first()
            video = Video(title="half2", status="failed", cdn_account_id=acc.id)
            db.session.add(video)
            db.session.commit()
            db.session.add(VideoFile(
                video_id=video.id, cdn_account_id=acc.id,
                remote_path="orphan-x", remote_url="http://x/x",
                file_size=10, file_type="segment", upload_status="uploaded"))
            db.session.commit()
            vid = video.id

        orig = CDNManager.get_provider_instance
        CDNManager.get_provider_instance = classmethod(lambda cls, acc: ForeignProvider())
        try:
            resp = self.client.delete(f"/api/videos/{vid}/metadata")
        finally:
            CDNManager.get_provider_instance = orig
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["cdn"]["deleted"], 0)
        self.assertEqual(body["cdn"]["failed"], 1)
        self.assertEqual(body["cdn"]["not_owned"], ["orphan-x"])
        with self.app.app_context():
            from app.models import Video
            self.assertIsNone(db_session_get(Video, vid))

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


class DeleterHonestyTests(unittest.TestCase):
    def _setup_delete_job(self, n_files=2):
        from app.models import Video, VideoFile, Job, CDNAccount, db
        with self.app.app_context():
            acc = CDNAccount.query.first()
            video = Video(title="doomed", status="delete_pending",
                          cdn_account_id=acc.id)
            db.session.add(video)
            db.session.commit()
            for i in range(n_files):
                db.session.add(VideoFile(
                    video_id=video.id, cdn_account_id=acc.id,
                    remote_path=f"file-{i}", remote_url=f"http://x/{i}",
                    file_size=10, file_type="segment", upload_status="uploaded"))
            job = Job(video_id=video.id, job_type="delete_video", status="queued")
            db.session.add(job)
            db.session.commit()
            return video.id, job.id

    def setUp(self):
        self.app = create_app(_test_config())
        self.client = _authed_client(self.app)

    def test_successful_delete_completes(self):
        from app.worker import deleter
        from app.cdn.manager import CDNManager
        from app.models import Video, Job, db

        vid, jid = self._setup_delete_job()

        class GoodProvider:
            def delete_file(self, ident):
                return True, [{"endpoint": "x", "status": 204, "body": ""}]

        orig = CDNManager.get_provider_instance
        CDNManager.get_provider_instance = classmethod(lambda cls, acc: GoodProvider())
        try:
            with self.app.app_context():
                deleter.execute_video_deletion(jid)
        finally:
            CDNManager.get_provider_instance = orig
        with self.app.app_context():
            self.assertEqual(db.session.get(Job, jid).status, "completed")
            self.assertEqual(db.session.get(Video, vid).status, "deleted")

    def test_auth_rejection_fails_job_and_keeps_video(self):
        from app.worker import deleter
        from app.cdn.manager import CDNManager
        from app.models import Video, Job, db

        vid, jid = self._setup_delete_job()

        class BadKeyProvider:
            def delete_file(self, ident):
                return False, [{"endpoint": "x", "status": 401,
                                "body": '{"error":"invalid_auth"}'}]

        orig = CDNManager.get_provider_instance
        CDNManager.get_provider_instance = classmethod(lambda cls, acc: BadKeyProvider())
        try:
            with self.app.app_context():
                deleter.execute_video_deletion(jid)
        finally:
            CDNManager.get_provider_instance = orig
        with self.app.app_context():
            job = db.session.get(Job, jid)
            self.assertEqual(job.status, "failed")
            self.assertIn("key", job.error_message)
            # Video stays retryable, NOT marked deleted:
            self.assertEqual(db.session.get(Video, vid).status, "delete_pending")

    def test_not_owned_404_fails_with_ownership_hint(self):
        from app.worker import deleter
        from app.cdn.manager import CDNManager
        from app.models import Video, Job, db

        vid, jid = self._setup_delete_job()

        class ForeignKeyProvider:
            def delete_file(self, ident):
                return False, [{"endpoint": "https://cdn.hackclub.com/api/v4/upload/x",
                                "status": 404, "body": "no matching resource"}]

        orig = CDNManager.get_provider_instance
        CDNManager.get_provider_instance = classmethod(lambda cls, acc: ForeignKeyProvider())
        try:
            with self.app.app_context():
                deleter.execute_video_deletion(jid)
        finally:
            CDNManager.get_provider_instance = orig
        with self.app.app_context():
            job = db.session.get(Job, jid)
            self.assertEqual(job.status, "failed")
            self.assertIn("different", job.error_message)
            self.assertEqual(db.session.get(Video, vid).status, "delete_pending")

    def test_rekey_account_repairs_decryption(self):
        with self.app.app_context():
            from app.models import CDNAccount, db
            acc_id = CDNAccount.query.first().id
        resp = self.client.put(f"/api/cdn-accounts/{acc_id}",
                               json={"api_key": "sk_cdn_testkey123"})
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            from app.models import CDNAccount, db
            acc = db.session.get(CDNAccount, acc_id)
            self.assertEqual(acc.get_api_key(), "sk_cdn_testkey123")

    def test_rekey_rejects_empty_key(self):
        with self.app.app_context():
            from app.models import CDNAccount
            acc_id = CDNAccount.query.first().id
        resp = self.client.put(f"/api/cdn-accounts/{acc_id}", json={"api_key": "  "})
        self.assertEqual(resp.status_code, 400)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_test_config())
        self.client = _authed_client(self.app)

    def test_unconfigured_mirror_falls_back(self):
        from app.cdn import supabase_store
        with self.app.app_context():
            self.assertFalse(supabase_store.configured())
            ok, _ = supabase_store.fetch_remote()
            self.assertFalse(ok)
        self.assertEqual(self.client.post("/api/cdn-accounts/sync").status_code, 503)
        html = self.client.get("/cdn-accounts").data.decode()
        self.assertIn("Local only", html)

    def test_create_pushes_mirror(self):
        from unittest.mock import patch
        from app.cdn import supabase_store

        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            return True, None

        with self.app.app_context():
            self.app.config["SUPABASE_URL"] = "https://xyz.supabase.co"
            self.app.config["SUPABASE_SERVICE_ROLE_KEY"] = "service-key"
        with patch.object(supabase_store, "_request", side_effect=fake_request):
            resp = self.client.post("/api/cdn-accounts", json={
                "name": "mir", "provider": "Hack Club CDN", "api_key": "sk_test"})
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertTrue(body["mirror"]["ok"])
        self.assertEqual(calls[0][0], "POST")
        sent = calls[0][2]
        self.assertNotIn("sk_test", str(sent))
        self.assertTrue(sent["encrypted_credentials"])

    def test_diagnostics_never_wear_gateway_status(self):
        """Failed CDN diagnostics must return 200 + success:false, never 502
        (browsers mislabel 502 as 'Bad Gateway', hiding the real message)."""
        with self.app.app_context():
            from app.models import CDNAccount
            acc_id = CDNAccount.query.first().id
        resp = self.client.post(f"/api/cdn-accounts/{acc_id}/test")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("success", resp.get_json())
        resp = self.client.post(f"/api/cdn-accounts/{acc_id}/refresh-storage")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("success", resp.get_json())

    def test_reveal_returns_plaintext_once(self):
        with self.app.app_context():
            from app.models import CDNAccount
            acc_id = CDNAccount.query.first().id
        resp = self.client.post(f"/api/cdn-accounts/{acc_id}/reveal")
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            from app.models import CDNAccount, db
            acc = db.session.get(CDNAccount, acc_id)
            self.assertEqual(resp.get_json()["api_key"], acc.get_api_key())
        self.assertEqual(
            self.client.post("/api/cdn-accounts/nope/reveal").status_code, 404)

    def test_export_has_encrypted_blob_not_plaintext(self):
        import json as _json
        with self.app.app_context():
            from app.models import CDNAccount, db
            acc = CDNAccount.query.first()
            acc.set_api_key("sk_super_secret_plaintext")
            db.session.commit()
        resp = self.client.get("/api/cdn-accounts/export")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
        payload = _json.loads(resp.data.decode())
        blob = payload["accounts"][0]["encrypted_credentials"]
        self.assertTrue(blob)
        self.assertNotIn("sk_super_secret_plaintext", resp.data.decode())


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
