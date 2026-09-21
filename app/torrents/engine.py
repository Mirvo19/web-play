"""Isolated torrent download engine (aria2c subprocess).

Security model (see feature brief for the full rationale):
  * The ONLY peer-network code runs inside short-lived `aria2c` child
    processes. This module never imports Flask, the DB, or app config, so
    the engine cannot reach secrets even by accident.
  * Children get a scrubbed environment (PATH/HOME/lang only — never
    os.environ), cwd pinned inside the quarantine dir, stdin closed, no
    shell. stdout/stderr are captured to bounded buffers, never logged raw.
  * Each torrent gets its own quarantine dir (0700, jailed). Filenames from
    metadata are used ONLY to locate downloads under that dir (traversal
    fails closed); the handoff path re-sanitizes everything.
  * Resource caps are enforced twice: aria2c flags (peers, bandwidth,
    timeouts) AND a supervisor wall-clock deadline that kills the process
    group. Quarantine is wiped on success, failure, cancel, and timeout.
  * aria2c runs leech-only (--seed-time=0) with LAN discovery off.

Requires the `aria2c` binary (apt: `aria2`); absence disables the feature
via is_available() instead of failing at import.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Optional

from app.torrents.bencode import parse_torrent_info, BencodeError

_log = logging.getLogger(__name__)

TORRENT_FILE_MAX_BYTES = 5 * 1024 * 1024


class TorrentError(RuntimeError):
    pass


class TorrentCancelled(RuntimeError):
    pass


class TorrentTimeout(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Availability + quarantine
# ---------------------------------------------------------------------------

def find_aria2() -> Optional[str]:
    override = os.environ.get("ARIA2_BIN", "").strip()
    if override:
        return override if os.path.exists(override) else None
    return shutil.which("aria2c")


def is_available() -> bool:
    return find_aria2() is not None


def quarantine_for(base_dir: str, token: str) -> str:
    """Create (0700) and return the jailed per-torrent quarantine dir."""
    base = os.path.abspath(base_dir)
    safe_token = "".join(c for c in (token or "") if c.isalnum() or c in ("-", "_"))[:64]
    if not safe_token:
        raise TorrentError("invalid quarantine token")
    path = os.path.abspath(os.path.join(base, "torrent_" + safe_token))
    if path != base and not path.startswith(base + os.sep):
        raise TorrentError("quarantine path escapes base directory")
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def wipe_quarantine(path: str) -> None:
    """Remove a quarantine dir entirely (best effort, never raises)."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        _log.warning("Could not wipe quarantine %s: %s", path, e)


def scrubbed_env(work_dir: str) -> dict:
    """Minimal child environment — no secrets, no app config, ever."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": work_dir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if os.name == "nt":  # dev-box fallback; production is Linux
        for key in ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def _safe_join(base: str, *parts: str) -> str:
    full = os.path.abspath(os.path.join(base, *[p for p in parts if p]))
    if full != base and not full.startswith(base + os.sep):
        raise TorrentError(f"refusing path outside quarantine: {full!r}")
    return full


# ---------------------------------------------------------------------------
# Metadata (no payload downloaded)
# ---------------------------------------------------------------------------

def fetch_metadata(kind: str, source: str, work_dir: str, timeout_sec: int) -> dict:
    """Fetch torrent metadata only. Returns {name, total_size, files}.

    kind 'magnet': source is the magnet URI (downloaded via
      --bt-metadata-only, payload never fetched).
    kind 'file': source is a path to an uploaded .torrent file.
    Raises TorrentError on any problem (bad magnet, timeout, bad file).
    """
    timeout_sec = max(10, int(timeout_sec))
    if kind == "magnet":
        if not source or not source.strip().lower().startswith("magnet:?"):
            raise TorrentError("not a magnet link (must start with 'magnet:?')")
        torrent_path = _fetch_magnet_metadata(source.strip(), work_dir, timeout_sec)
        with open(torrent_path, "rb") as f:
            raw = f.read(TORRENT_FILE_MAX_BYTES + 1)
    elif kind == "file":
        if not source or not os.path.isfile(source):
            raise TorrentError(".torrent file not found")
        if os.path.getsize(source) > TORRENT_FILE_MAX_BYTES:
            raise TorrentError(".torrent file too large")
        with open(source, "rb") as f:
            raw = f.read(TORRENT_FILE_MAX_BYTES + 1)
    else:
        raise TorrentError(f"unknown torrent source kind: {kind!r}")

    if len(raw) > TORRENT_FILE_MAX_BYTES:
        raise TorrentError(".torrent metainfo too large")
    try:
        return parse_torrent_info(raw)
    except BencodeError as e:
        raise TorrentError(f"invalid torrent metadata: {e}")


def _fetch_magnet_metadata(magnet: str, work_dir: str, timeout_sec: int) -> str:
    aria2 = find_aria2()
    if not aria2:
        raise TorrentError("torrent engine unavailable (aria2c not installed)")
    before = set(glob.glob(os.path.join(work_dir, "*.torrent")))
    argv = [
        aria2,
        "--bt-metadata-only=true",
        "--bt-save-metadata=true",
        "--bt-enable-lpd=false",
        "--seed-time=0",
        f"--dir={work_dir}",
        f"--timeout={timeout_sec}",
        "--connect-timeout=30",
        "--summary-interval=0",
        "--show-console-readout=false",
        "--console-log-level=warn",
        "--auto-file-renaming=false",
        magnet,
    ]
    proc = _spawn(argv, work_dir)
    rc = _wait_bounded(proc, timeout_sec + 15, None)
    if rc != 0:
        raise TorrentError("could not fetch torrent metadata (magnet unreachable or timed out)")
    after = [p for p in glob.glob(os.path.join(work_dir, "*.torrent")) if p not in before]
    if not after:
        # Fall back to newest .torrent in case clocks/sets disagree.
        candidates = glob.glob(os.path.join(work_dir, "*.torrent"))
        if not candidates:
            raise TorrentError("magnet resolved but produced no metadata file")
        after = [max(candidates, key=os.path.getmtime)]
    return max(after, key=os.path.getmtime)


# ---------------------------------------------------------------------------
# Selective download
# ---------------------------------------------------------------------------

def expected_locations(meta: dict, indices, work_dir: str) -> dict:
    """Map selected 1-based file indexes to jailed expected download paths."""
    by_index = {f["index"]: f for f in meta.get("files", [])}
    single = len(by_index) == 1
    locations = {}
    for idx in indices:
        entry = by_index.get(idx)
        if entry is None:
            raise TorrentError(f"selected file index {idx} is not in this torrent")
        parts = entry["path"].split("/") if not single else []
        locations[idx] = _safe_join(work_dir, meta["name"], *parts)
    return locations


def run_download(meta: dict, indices, work_dir: str, source: str, limits: dict,
                 progress_cb: Optional[Callable] = None,
                 cancel_event: Optional[threading.Event] = None,
                 timeout_sec: int = 3600) -> list:
    """Download ONLY the selected indexes. Returns [(index, path)].

    limits: {max_peers, bandwidth_kbps (0=uncapped), aria_timeout}.
    progress_cb(per_file_bytes: dict, speed_bps: float) called ~1/s.
    Raises TorrentCancelled / TorrentTimeout / TorrentError.
    `source` is the magnet URI or .torrent path (argv element, never shell).
    """
    aria2 = find_aria2()
    if not aria2:
        raise TorrentError("torrent engine unavailable (aria2c not installed)")
    indices = sorted({int(i) for i in indices})
    if not indices:
        raise TorrentError("no files selected")
    locations = expected_locations(meta, indices, work_dir)

    bw = max(0, int(limits.get("bandwidth_kbps", 0) or 0))
    peers = min(max(1, int(limits.get("max_peers", 50) or 50)), 500)
    aria_timeout = min(max(10, int(limits.get("aria_timeout", 60) or 60)), 600)

    argv = [
        aria2,
        f"--dir={work_dir}",
        "--select-file=" + ",".join(str(i) for i in indices),
        "--seed-time=0",
        "--bt-enable-lpd=false",
        f"--bt-max-peers={peers}",
        "--max-concurrent-downloads=1",
        f"--timeout={aria_timeout}",
        "--connect-timeout=30",
        "--file-allocation=none",  # files grow as pieces land -> honest progress
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--check-integrity=true",
        "--summary-interval=0",
        "--show-console-readout=false",
        "--console-log-level=warn",
        source,
    ]
    if bw > 0:
        argv.insert(-1, f"--max-download-limit={bw}K")

    deadline = time.time() + max(30, int(timeout_sec))
    proc = _spawn(argv, work_dir)
    stderr_tail: deque = deque(maxlen=20)

    def _drain():
        try:
            for line in proc.stderr:
                stderr_tail.append(line.strip())
        except Exception:
            pass

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()

    last_total, last_ts = 0, time.time()
    try:
        while True:
            try:
                rc = proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                rc = None
            if cancel_event is not None and cancel_event.is_set():
                raise TorrentCancelled("torrent download cancelled")
            if time.time() > deadline:
                raise TorrentTimeout(
                    f"torrent exceeded wall-clock limit ({int(timeout_sec)}s) and was stopped")
            per_file = _sample_sizes(locations)
            total = sum(per_file.values())
            now = time.time()
            speed = (total - last_total) / max(now - last_ts, 0.001)
            last_total, last_ts = total, now
            if progress_cb is not None:
                try:
                    progress_cb(per_file, max(0.0, speed))
                except Exception:
                    pass
            if rc is not None:
                break
    except (TorrentCancelled, TorrentTimeout):
        _terminate(proc)
        raise
    finally:
        drain_thread.join(timeout=5)

    if proc.returncode != 0:
        tail = " | ".join(stderr_tail)[:500]
        raise TorrentError(f"torrent download failed (exit={proc.returncode}): {tail}")

    # Confirm every selected file actually landed (size check happens at
    # validation; here we require existence so a wrong-file mistake surfaces).
    done = []
    for idx in indices:
        if not os.path.isfile(locations[idx]):
            raise TorrentError(f"selected file #{idx} missing after download completed")
        done.append((idx, locations[idx]))
    return done


def _sample_sizes(locations: dict) -> dict:
    out = {}
    for idx, path in locations.items():
        try:
            out[idx] = os.path.getsize(path) if os.path.isfile(path) else 0
        except OSError:
            out[idx] = 0
    return out


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------

def _spawn(argv: list, work_dir: str) -> subprocess.Popen:
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "cwd": work_dir,
        "env": scrubbed_env(work_dir),
        "text": True,
        "bufsize": 1,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(argv, **kwargs)
    except FileNotFoundError as e:
        raise TorrentError(f"torrent engine binary missing: {e}")
    except OSError as e:
        raise TorrentError(f"could not start torrent engine: {e}")


def _wait_bounded(proc: subprocess.Popen, timeout_sec: int,
                  cancel_event: Optional[threading.Event]) -> Optional[int]:
    """Wait with 1s ticks so cancel stays responsive. Returns rc or None."""
    deadline = time.time() + max(5, int(timeout_sec))
    while True:
        try:
            return proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        if cancel_event is not None and cancel_event.is_set():
            _terminate(proc)
            raise TorrentCancelled("torrent operation cancelled")
        if time.time() > deadline:
            _terminate(proc)
            raise TorrentTimeout("torrent operation timed out")


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass
