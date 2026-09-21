"""Stage 1 tests: torrent engine isolation + parsing + validation.

aria2c itself is NOT required here — the download loop is exercised with a
stubbed child process. Pure parts (bencode, magic bytes, quarantine, env
scrubbing) run for real.
"""

import os
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.torrents import bencode, validate
from app.torrents import engine as eng


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


def sample_multi_torrent() -> bytes:
    return benc({
        b"announce": b"http://tracker.example/announce",
        b"info": {
            b"name": b"Show.S01",
            b"piece length": 262144,
            b"pieces": b"0" * 20,
            b"files": [
                {b"length": 1500, b"path": [b"ep01.mkv"]},
                {b"length": 2500, b"path": [b"subs", b"ep01.srt"]},
            ],
        },
    })


def sample_single_torrent() -> bytes:
    return benc({
        b"announce": b"http://tracker.example/announce",
        b"info": {
            b"name": b"movie.mp4",
            b"length": 3000,
            b"piece length": 262144,
            b"pieces": b"0" * 20,
        },
    })


class BencodeTests(unittest.TestCase):
    def test_multi_file_parse(self):
        meta = bencode.parse_torrent_info(sample_multi_torrent())
        self.assertEqual(meta["name"], "Show.S01")
        self.assertEqual(meta["total_size"], 4000)
        self.assertEqual(
            [(f["index"], f["path"], f["size"]) for f in meta["files"]],
            [(1, "ep01.mkv", 1500), (2, "subs/ep01.srt", 2500)],
        )

    def test_single_file_parse(self):
        meta = bencode.parse_torrent_info(sample_single_torrent())
        self.assertEqual(meta["files"], [{"index": 1, "path": "movie.mp4", "size": 3000}])

    def test_rejects_garbage_and_bombs(self):
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"not bencoded")
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"d3:foo3:barextra")
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"i12x")
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"l" * 30 + b"e" * 30)  # nesting bomb
        with self.assertRaises(bencode.BencodeError):
            bencode.parse_torrent_info(benc({b"nope": 1}))

    def test_rejects_empty_path_segment(self):
        bad = benc({b"info": {b"name": b"x", b"piece length": 1, b"pieces": b"",
                              b"files": [{b"length": 5, b"path": [b""]}]}})
        with self.assertRaises(bencode.BencodeError):
            bencode.parse_torrent_info(bad)


class ValidateTests(unittest.TestCase):
    def _file(self, content: bytes) -> str:
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_mp4_and_mkv_pass(self):
        mp4 = self._file(b"\x00\x00\x00\x20ftypisom" + b"p" * 2000)
        out = validate.validate_media_file(mp4, os.path.getsize(mp4), "movie.mp4")
        self.assertEqual(out["extension"], ".mp4")
        mkv = self._file(b"\x1a\x45\xdf\xa3" + b"m" * 2000)
        self.assertEqual(
            validate.validate_media_file(mkv, os.path.getsize(mkv), "ep.mkv")["extension"], ".mkv")

    def test_masquerade_rejected(self):
        fake = self._file(b"#!/bin/sh\nevil" + b"e" * 2000)
        with self.assertRaises(validate.ValidationError):
            validate.validate_media_file(fake, os.path.getsize(fake), "movie.mp4")

    def test_bad_extension_and_size_mismatch_rejected(self):
        exe = self._file(b"MZ" + b"e" * 2000)
        with self.assertRaises(validate.ValidationError):
            validate.validate_media_file(exe, os.path.getsize(exe), "run.exe")
        mp4 = self._file(b"\x00\x00\x00\x20ftypisom" + b"p" * 2000)
        with self.assertRaises(validate.ValidationError):
            validate.validate_media_file(mp4, 999999, "movie.mp4")
        tiny = self._file(b"\x00\x00\x00\x20ftyp")
        with self.assertRaises(validate.ValidationError):
            validate.validate_media_file(tiny, os.path.getsize(tiny), "movie.mp4")


class IsolationTests(unittest.TestCase):
    def test_quarantine_token_cannot_escape(self):
        base = tempfile.mkdtemp()
        q = eng.quarantine_for(base, "../../etc-passwd!!")
        self.assertTrue(os.path.abspath(q).startswith(os.path.abspath(base) + os.sep))
        self.assertIn("etc-passwd", os.path.basename(q))
        with self.assertRaises(eng.TorrentError):
            eng.quarantine_for(base, "!!!")
        eng.wipe_quarantine(q)
        self.assertFalse(os.path.exists(q))

    def test_scrubbed_env_leaks_nothing(self):
        os.environ["TORRENT_ISOLATION_PROBE"] = "super-secret-value"
        os.environ["CDN_ENCRYPTION_KEY"] = "super-secret-value"
        try:
            env = eng.scrubbed_env(tempfile.mkdtemp())
        finally:
            del os.environ["TORRENT_ISOLATION_PROBE"]
            del os.environ["CDN_ENCRYPTION_KEY"]
        blob = "\n".join(f"{k}={v}" for k, v in env.items())
        self.assertNotIn("super-secret-value", blob)
        self.assertNotIn("DATABASE_URL", env)

    def test_traversal_paths_fail_closed(self):
        meta = {"name": "T", "total_size": 20,
                "files": [{"index": 1, "path": "ok.mkv", "size": 10},
                          {"index": 2, "path": "../../evil.mkv", "size": 10}]}
        with self.assertRaises(eng.TorrentError):
            eng.expected_locations(meta, [2], tempfile.mkdtemp())

    def test_magnet_shape_rejected_without_network(self):
        with self.assertRaises(eng.TorrentError):
            eng.fetch_metadata("magnet", "https://example.com/not-a-magnet",
                               tempfile.mkdtemp(), 30)


class FakePopen:
    """Stub aria2c: fail two polls, then exit 0 (file pre-created by test)."""

    def __init__(self, *a, **k):
        self.stderr = []
        self.pid = 999999
        self.returncode = 0
        self._polls = 0

    def wait(self, timeout=None):
        self._polls += 1
        if self._polls < 3:
            raise subprocess.TimeoutExpired("aria2c", timeout)
        return 0


class DownloadLoopTests(unittest.TestCase):
    def test_success_path_reports_progress_and_returns_files(self):
        base = tempfile.mkdtemp()
        q = eng.quarantine_for(base, "loop1")
        target = os.path.join(q, "Show.S01", "ep01.mkv")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(b"m" * 1500)
        meta = bencode.parse_torrent_info(sample_multi_torrent())

        seen = []

        def cb(per_file, speed):
            seen.append((dict(per_file), speed))

        with patch.object(eng, "find_aria2", return_value="aria2c"), \
             patch.object(eng, "_spawn", return_value=FakePopen()):
            done = eng.run_download(meta, [1], q, "magnet:?xt=urn:btih:abc",
                                    {"max_peers": 5, "bandwidth_kbps": 0, "aria_timeout": 30},
                                    progress_cb=cb,
                                    cancel_event=threading.Event(),
                                    timeout_sec=60)
        self.assertEqual(done, [(1, target)])
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], {1: 1500})
        eng.wipe_quarantine(q)

    def test_cancel_stops_and_wipes(self):
        base = tempfile.mkdtemp()
        q = eng.quarantine_for(base, "loop2")
        meta = bencode.parse_torrent_info(sample_multi_torrent())
        cancel = threading.Event()
        cancel.set()
        with patch.object(eng, "find_aria2", return_value="aria2c"), \
             patch.object(eng, "_spawn", return_value=FakePopen()):
            with patch.object(eng, "_terminate") as term:
                with self.assertRaises(eng.TorrentCancelled):
                    eng.run_download(meta, [1], q, "magnet:?xt=urn:btih:abc",
                                     {"max_peers": 5}, cancel_event=cancel, timeout_sec=60)
                term.assert_called_once()
        eng.wipe_quarantine(q)


if __name__ == "__main__":
    unittest.main()
