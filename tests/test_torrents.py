"""Stage 2+3 tests: torrent model, API validation, handoff, coordinator.

aria2c is NOT required: availability-gated paths are stubbed, the engine
loop was covered in test_torrents_engine.py, and handoff is exercised with
real validated media files on disk.
"""

import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_ANON_KEY", "")

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402


def _test_config(**overrides):
    qdir = tempfile.mkdtemp(prefix="tq-")
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
        TORRENT_QUARANTINE_FOLDER=qdir,
        ENABLE_JOB_SCHEDULER="false",
    )
    attrs.update(overrides)
    return type("TorrentTestConfig", (Config,), attrs)


def _authed_client(app):
    from app.auth import generate_session_jwt

    with app.app_context():
        token, _ = generate_session_jwt("u1", "a@x.com")
    client = app.test_client()
    client.set_cookie("access_token", token)
    return client


def benc(value) -> bytes:
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(benc(v) for v in value) + b"e"
    if isinstance(value, dict):
        out = b"d"
        for k in sorted(value):
            out += benc(k) + benc(value[k])
        return out + b"e"
    raise TypeError(value)


def sample_torrent_bytes() -> bytes:
    return benc({
        b"announce": b"http://tracker.example/announce",
        b"info": {
            b"name": b"Clip",
            b"length": 2048,
            b"piece length": 262144,
            b"pieces": b"0" * 20,
        },
    })


class ModelTests(unittest.TestCase):
    def test_to_dict_progress_math(self):
        from app.models import TorrentJob

        row = TorrentJob(
            source_kind="magnet", source_ref="magnet:?x", quarantine_token="t",
            state="downloading",
            meta_json=json.dumps({"name": "T", "total_size": 3000, "files": [
                {"index": 1, "path": "a.mp4", "size": 1000},
                {"index": 2, "path": "b.mp4", "size": 2000}]}),
            selected_json=json.dumps([1, 2]),
            progress_json=json.dumps({"1": 1000, "2": 500}),
            speed_bps=500.0,
        )
        d = row.to_dict()
        self.assertEqual(d["selected_total"], 3000)
        self.assertEqual(d["progress"]["bytes_done"], 1500)
        self.assertEqual(d["progress"]["pct"], 50.0)
        self.assertEqual(d["eta_seconds"], 3)


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_test_config())
        self.client = _authed_client(self.app)

    def test_rejects_non_magnet(self):
        resp = self.client.post("/api/torrents/submit", json={"magnet": "https://x/y"})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_non_torrent_file(self):
        resp = self.client.post(
            "/api/torrents/submit",
            data={"file": (io.BytesIO(b"nope"), "evil.exe")},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)

    def test_engine_missing_fails_closed(self):
        # No aria2c on this box -> honest 503, nothing stored.
        resp = self.client.post("/api/torrents/submit", json={"magnet": "magnet:?xt=urn:btih:abc"})
        self.assertEqual(resp.status_code, 503)
        with self.app.app_context():
            from app.models import TorrentJob
            self.assertEqual(TorrentJob.query.count(), 0)

    def test_coordinator_missing_fails_closed(self):
        from app.torrents import engine as _eng

        self.app.extensions["torrent_coordinator"] = None
        with patch.object(_eng, "is_available", return_value=True):
            resp = self.client.post("/api/torrents/submit",
                                    json={"magnet": "magnet:?xt=urn:btih:abc"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("unavailable", resp.get_json()["error"])

    def test_quarantine_base_unwritable_raises_loudly(self):
        from app.torrents.coordinator import quarantine_base

        fd, blocker = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(blocker) and os.remove(blocker))
        self.app.config["TORRENT_QUARANTINE_FOLDER"] = os.path.join(blocker, "sub")
        with self.assertRaises(OSError):
            quarantine_base(self.app)

    def test_default_quarantine_lives_in_app_tree(self):
        from app.torrents.coordinator import quarantine_base

        self.app.config["TORRENT_QUARANTINE_FOLDER"] = ""
        base = quarantine_base(self.app)
        self.assertTrue(base.endswith("torrent-quarantine"))
        self.assertTrue(os.path.isdir(base))
        upload = os.path.abspath(self.app.config["UPLOAD_FOLDER"])
        self.assertFalse(os.path.abspath(base).startswith(upload + os.sep))
        import shutil
        shutil.rmtree(base, ignore_errors=True)

    def test_magnet_submit_ok_when_available(self):
        from app.torrents import engine as _eng
        from app.models import TorrentJob

        self.app.extensions["torrent_coordinator"] = object()
        with patch.object(_eng, "is_available", return_value=True):
            resp = self.client.post("/api/torrents/submit",
                                    json={"magnet": "magnet:?xt=urn:btih:abc"})
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()["torrent"]
        self.assertEqual(body["state"], "fetching_metadata")
        with self.app.app_context():
            row = TorrentJob.query.first()
            self.assertEqual(row.source_kind, "magnet")

    def test_torrent_file_submit_stores_source(self):
        from app.torrents import engine as _eng
        from app.models import TorrentJob

        self.app.extensions["torrent_coordinator"] = object()
        with patch.object(_eng, "is_available", return_value=True):
            resp = self.client.post(
                "/api/torrents/submit",
                data={"file": (io.BytesIO(sample_torrent_bytes()), "clip.torrent")},
                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 202)
        with self.app.app_context():
            row = TorrentJob.query.first()
            self.assertEqual(row.source_kind, "file")
            from app.torrents.coordinator import quarantine_base
            src = os.path.join(quarantine_base(self.app),
                               "torrent_" + row.quarantine_token, "source.torrent")
            self.assertTrue(os.path.isfile(src))


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_test_config())
        self.client = _authed_client(self.app)
        with self.app.app_context():
            from app.models import TorrentJob, CDNAccount, db
            self.account_id = CDNAccount.query.first().id
            row = TorrentJob(
                source_kind="magnet", source_ref="magnet:?x", quarantine_token="sel1",
                state="awaiting_selection", display_name="Big",
                total_size=300 * 1024 * 1024,
                meta_json=json.dumps({"name": "Big", "total_size": 300 * 1024 * 1024,
                                      "files": [{"index": 1, "path": "a.mp4",
                                                 "size": 300 * 1024 * 1024}]}),
            )
            db.session.add(row)
            db.session.commit()
            self.tid = row.id

    def _select(self, payload):
        return self.client.post(f"/api/torrents/{self.tid}/select", json=payload)

    def test_unknown_index_rejected(self):
        resp = self._select({"indexes": [99], "cdn_account_id": self.account_id})
        self.assertEqual(resp.status_code, 400)

    def test_oversize_selection_rejected(self):
        # 300 MB claimed vs the 100 MB floor cap -> 413, nothing stored.
        with self.app.app_context():
            from app.models import Setting
            Setting.set("torrent_max_total_mb", "100")
        resp = self._select({"indexes": [1], "cdn_account_id": self.account_id})
        self.assertEqual(resp.status_code, 413)

    def test_bad_account_rejected(self):
        resp = self._select({"indexes": [1], "cdn_account_id": "nope"})
        self.assertEqual(resp.status_code, 400)

    def test_select_ok_transitions_to_downloading(self):
        resp = self._select({"indexes": [1], "cdn_account_id": self.account_id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["torrent"]["state"], "downloading")
        # Second select on a non-awaiting row is a conflict.
        resp2 = self._select({"indexes": [1], "cdn_account_id": self.account_id})
        self.assertEqual(resp2.status_code, 409)


class CancelTests(unittest.TestCase):
    def test_cancel_awaiting_wipes_and_finalizes(self):
        from app.models import TorrentJob, db

        app = create_app(_test_config())
        client = _authed_client(app)
        with app.app_context():
            row = TorrentJob(source_kind="magnet", source_ref="magnet:?x",
                             quarantine_token="cancel1", state="awaiting_selection")
            db.session.add(row)
            db.session.commit()
            tid = row.id
            from app.torrents.coordinator import quarantine_base
            from app.torrents import engine as _eng
            q = _eng.quarantine_for(quarantine_base(app), "cancel1")
            marker = os.path.join(q, "junk")
            with open(marker, "w") as f:
                f.write("x")
        resp = client.post(f"/api/torrents/{tid}/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["torrent"]["state"], "cancelled")
        self.assertFalse(os.path.exists(q))
        with app.app_context():
            self.assertEqual(db.session.get(TorrentJob, tid).state, "cancelled")


class HandoffTests(unittest.TestCase):
    def _media(self, qdir, name, size=2048):
        path = os.path.join(qdir, name)
        with open(path, "wb") as f:
            f.write(b"\x00\x00\x00\x20ftypisom" + b"p" * (size - 12))
        return path

    def test_handoff_creates_queued_pipeline_rows(self):
        from app.models import Video, Job, db
        from app.torrents.handoff import hand_off

        app = create_app(_test_config())
        with app.app_context():
            from app.models import CDNAccount
            acc_id = CDNAccount.query.first().id
            qdir = tempfile.mkdtemp()
            p1 = self._media(qdir, "ep01.mkv", 2048)

            class T:
                display_name = "Show"
            # mkv magic for an .mkv name:
            os.remove(p1)
            p1 = os.path.join(qdir, "ep01.mkv")
            with open(p1, "wb") as f:
                f.write(b"\x1a\x45\xdf\xa3" + b"m" * 2044)

            class TRow:
                display_name = "Show"
                quarantine_token = "hand1"

            handed = hand_off(app, TRow(),
                              [(1, p1, 2048, "ep01.mkv")], acc_id, qdir)
            self.assertEqual(len(handed), 1)
            video = db.session.get(Video, handed[0]["video_id"])
            job = db.session.get(Job, handed[0]["job_id"])
            self.assertEqual(video.original_filename, "ep01.mkv")
            self.assertEqual(video.title, "ep01")
            self.assertEqual(job.job_type, "transcode_and_upload")
            self.assertEqual(job.status, "queued")
            self.assertFalse(os.path.exists(qdir))  # quarantine wiped

    def test_handoff_sanitizes_traversal_name(self):
        from app.torrents.handoff import hand_off

        app = create_app(_test_config())
        with app.app_context():
            from app.models import CDNAccount
            acc_id = CDNAccount.query.first().id
            qdir = tempfile.mkdtemp()
            p1 = self._media(qdir, "x.mp4", 2048)
            handed = hand_off(app, type("T", (), {"display_name": "T",
                                                  "quarantine_token": "hand2"})(),
                              [(1, p1, 2048, "../../evil.mp4")], acc_id, qdir)
            self.assertEqual(handed[0]["filename"], "evil.mp4")

    def test_handoff_nothing_valid_raises(self):
        from app.torrents.handoff import hand_off

        app = create_app(_test_config())
        with app.app_context():
            from app.models import CDNAccount
            acc_id = CDNAccount.query.first().id
            qdir = tempfile.mkdtemp()
            with self.assertRaises(RuntimeError):
                hand_off(app, type("T", (), {"display_name": "T",
                                             "quarantine_token": "hand3"})(),
                         [], acc_id, qdir)


class CoordinatorRecoveryTests(unittest.TestCase):
    def test_interrupted_rows_failed_on_start(self):
        from app.models import TorrentJob, db
        from app.torrents.coordinator import TorrentCoordinator, quarantine_base
        from app.torrents import engine as _eng

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: (db_cleanup(), os.path.exists(path) and os.remove(path)))

        app = create_app(_test_config(SQLALCHEMY_DATABASE_URI=f"sqlite:///{path}"))

        def db_cleanup():
            with app.app_context():
                from app.models import db as _db
                _db.session.remove()
                _db.engine.dispose()

        with app.app_context():
            row = TorrentJob(source_kind="magnet", source_ref="magnet:?x",
                             quarantine_token="rec1", state="downloading")
            db.session.add(row)
            db.session.commit()
            tid = row.id
            q = _eng.quarantine_for(quarantine_base(app), "rec1")

        coord = TorrentCoordinator(app)
        coord.start()
        try:
            with app.app_context():
                self.assertEqual(db.session.get(TorrentJob, tid).state, "failed")
            self.assertFalse(os.path.exists(q))
        finally:
            coord.stop(timeout=10)


class TorrentsPageTests(unittest.TestCase):
    def test_tab_renders_with_contracts(self):
        app = create_app(_test_config())
        client = _authed_client(app)
        html = client.get("/torrents").data.decode()
        for needle in ["torrent-bootstrap", "magnetForm", "magnetInput",
                       "torrentDrop", "torrentFile", "torrentList",
                       "/api/torrents"]:
            self.assertIn(needle, html)
        dash = client.get("/dashboard").data.decode()
        self.assertIn('href="/torrents"', dash)
        settings = client.get("/settings").data.decode()
        self.assertIn("torrentEnabled", settings)
        self.assertIn("torrentMaxTotalMb", settings)


class EngineLogEndpointTests(unittest.TestCase):
    def test_tail_and_missing_cases(self):
        from app.models import TorrentJob, db
        from app.torrents.coordinator import engine_log_path

        app = create_app(_test_config())
        client = _authed_client(app)
        with app.app_context():
            row = TorrentJob(source_kind="magnet", source_ref="magnet:?x",
                             quarantine_token="log1", state="downloading")
            db.session.add(row)
            db.session.commit()
            tid = row.id
            path = engine_log_path(app, tid)
            with open(path, "w") as f:
                f.write("\n".join(f"line{i}" for i in range(20)) + "\n")

        resp = client.get(f"/api/torrents/{tid}/engine-log?lines=3")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        # lines=3 is below the 10-line floor -> last 10 of 20
        self.assertEqual(body["lines"], [f"line{i}" for i in range(10, 20)])
        self.assertTrue(body["truncated"])
        self.assertEqual(body["total_lines"], 20)

        resp = client.get("/api/torrents/nope/engine-log")
        self.assertEqual(resp.status_code, 404)

        with app.app_context():
            os.remove(path)
        resp = client.get(f"/api/torrents/{tid}/engine-log")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
