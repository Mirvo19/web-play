"""Unit tests for the Hack Club CDN provider (documented v4 API contract).

HTTP is fully mocked — these pin the provider to openapi.json 4.0.0:
  GET  /api/v4/me            -> key check + real quota
  DELETE /api/v4/upload/{id} -> {"deleted": true} / 404 not-owned / 401 bad key
"""

import os
import unittest
from unittest.mock import patch

import requests as _real_requests

from app.cdn.hackclub import HackClubCDNProvider


class _FakeResponse:
    def __init__(self, status_code, payload=None, raw_invalid_json=False, text=""):
        self.status_code = status_code
        self._payload = payload
        self._raw_invalid = raw_invalid_json
        self.text = text or ""
        self.headers = {"content-type": "application/json"}

    def json(self):
        if self._raw_invalid:
            raise ValueError("No JSON here")
        return self._payload


def _provider():
    return HackClubCDNProvider(api_key="sk_cdn_test")


class ConnectionTests(unittest.TestCase):
    def test_valid_key_reports_owner(self):
        with patch("app.cdn.hackclub.requests.get") as mock_get:
            mock_get.return_value = _FakeResponse(200, {"email": "a@x.com"})
            ok, msg = _provider().test_connection()
        self.assertTrue(ok)
        self.assertIn("a@x.com", msg)
        self.assertIn("/api/v4/me", mock_get.call_args[0][0])

    def test_revoked_key_reports_invalid(self):
        with patch("app.cdn.hackclub.requests.get") as mock_get:
            mock_get.return_value = _FakeResponse(401, {}, text='{"code":"invalid_auth"}')
            ok, msg = _provider().test_connection()
        self.assertFalse(ok)
        self.assertIn("Invalid", msg)

    def test_unreachable_reports_connection_error(self):
        with patch("app.cdn.hackclub.requests.get") as mock_get:
            mock_get.side_effect = _real_requests.ConnectionError("down")
            ok, msg = _provider().test_connection()
        self.assertFalse(ok)
        self.assertIn("Connection error", msg)

    def test_empty_key_refused_without_http(self):
        with patch("app.cdn.hackclub.requests.get") as mock_get:
            ok, msg = HackClubCDNProvider(api_key="").test_connection()
        self.assertFalse(ok)
        mock_get.assert_not_called()


class StorageTests(unittest.TestCase):
    def test_real_quota_values(self):
        with patch("app.cdn.hackclub.requests.get") as mock_get:
            mock_get.return_value = _FakeResponse(
                200, {"storage_used": 1234, "storage_limit": 50_000_000_000})
            info = _provider().get_storage_info()
        self.assertEqual(info["used_bytes"], 1234)
        self.assertEqual(info["total_bytes"], 50_000_000_000)
        self.assertEqual(info["available_bytes"], 50_000_000_000 - 1234)

    def test_http_error_raises(self):
        with patch("app.cdn.hackclub.requests.get") as mock_get:
            mock_get.return_value = _FakeResponse(500, {}, text="boom")
            with self.assertRaises(RuntimeError):
                _provider().get_storage_info()

    def test_invalid_json_raises(self):
        with patch("app.cdn.hackclub.requests.get") as mock_get:
            mock_get.return_value = _FakeResponse(200, raw_invalid_json=True)
            with self.assertRaises(RuntimeError):
                _provider().get_storage_info()


class DeleteTests(unittest.TestCase):
    UID = "019ff18e-7cc7-785d-a2ba-13b0c2a90812"

    def test_deleted_true_is_success(self):
        with patch("app.cdn.hackclub.requests.delete") as mock_delete:
            mock_delete.return_value = _FakeResponse(
                200, {"id": self.UID, "deleted": True})
            ok, tried = _provider().delete_file(self.UID)
        self.assertTrue(ok)
        self.assertIn(f"/api/v4/upload/{self.UID}", tried[0]["endpoint"])
        self.assertEqual(len(tried), 1)  # one documented call, no guessing

    def test_404_is_failure_not_silent_success(self):
        with patch("app.cdn.hackclub.requests.delete") as mock_delete:
            mock_delete.return_value = _FakeResponse(404, {}, text="not owned")
            ok, tried = _provider().delete_file(self.UID)
        self.assertFalse(ok)
        self.assertEqual(tried[0]["status"], 404)

    def test_401_is_failure(self):
        with patch("app.cdn.hackclub.requests.delete") as mock_delete:
            mock_delete.return_value = _FakeResponse(401, {}, text="invalid_auth")
            ok, _ = _provider().delete_file(self.UID)
        self.assertFalse(ok)

    def test_network_error_is_failure(self):
        with patch("app.cdn.hackclub.requests.delete") as mock_delete:
            mock_delete.side_effect = _real_requests.ConnectionError("down")
            ok, tried = _provider().delete_file(self.UID)
        self.assertFalse(ok)
        self.assertIn("error", tried[0])

    def test_public_url_extracts_id(self):
        with patch("app.cdn.hackclub.requests.delete") as mock_delete:
            mock_delete.return_value = _FakeResponse(
                200, {"id": self.UID, "deleted": True})
            ok, tried = _provider().delete_file(
                f"https://cdn.hackclub.com/{self.UID}/segment_0001.ts")
        self.assertTrue(ok)
        self.assertIn(f"/api/v4/upload/{self.UID}", tried[0]["endpoint"])

    def test_empty_identifier_fails_loudly(self):
        ok, tried = _provider().delete_file("")
        self.assertFalse(ok)


class UploadRetryTests(unittest.TestCase):
    def _temp_file(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".ts")
        with os.fdopen(fd, "wb") as f:
            f.write(b"x" * 1024)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_transient_ssl_blip_retries_then_succeeds(self):
        import requests as _req
        path = self._temp_file()
        err = _req.exceptions.SSLError("EOF occurred in violation of protocol")
        good = _FakeResponse(201, {"id": "uid-1", "url": "https://cdn.hackclub.com/uid-1/seg.ts"})
        with patch("app.cdn.hackclub.requests.post") as mock_post, \
             patch("app.cdn.hackclub.time.sleep") as mock_sleep:
            mock_post.side_effect = [err, good]
            out = _provider().upload_file(path, "seg.ts")
        self.assertEqual(out["remote_path"], "uid-1")
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()

    def test_permanent_400_fails_fast_without_retry(self):
        path = self._temp_file()
        with patch("app.cdn.hackclub.requests.post") as mock_post, \
             patch("app.cdn.hackclub.time.sleep") as mock_sleep:
            mock_post.return_value = _FakeResponse(400, {}, text="bad request")
            with self.assertRaises(RuntimeError):
                _provider().upload_file(path, "seg.ts")
        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    def test_exhausted_retries_raise(self):
        import requests as _req
        path = self._temp_file()
        with patch("app.cdn.hackclub.requests.post") as mock_post, \
             patch("app.cdn.hackclub.time.sleep"):
            mock_post.side_effect = _req.ConnectionError("down")
            with self.assertRaises(RuntimeError):
                _provider().upload_file(path, "seg.ts")
        self.assertGreaterEqual(mock_post.call_count, 2)


class ExtractIdTests(unittest.TestCase):
    def test_raw_and_url_forms(self):
        p = _provider()
        self.assertEqual(p.extract_upload_id("abc-123"), "abc-123")
        self.assertEqual(
            p.extract_upload_id("https://cdn.hackclub.com/abc-123/seg.ts"), "abc-123")
        self.assertEqual(p.extract_upload_id(""), "")
        self.assertEqual(p.extract_upload_id(None), "")


if __name__ == "__main__":
    unittest.main()
