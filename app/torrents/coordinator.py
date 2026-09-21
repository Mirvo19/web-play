"""In-process torrent coordinator: metadata fetch + selective download.

Owns one poll thread and a small executor (started/stopped from the app
factory next to the job supervisor). Work distribution:
  * fetching_metadata rows  -> fetch worker (no slot limit, executor-bound)
  * downloading rows         -> download worker (gated by torrent_max_concurrent)
  * awaiting_selection rows  -> inert until the user selects files via API
Crash recovery on start: rows left fetching/downloading/validating by an
unclean shutdown are failed and their quarantines wiped.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

_log = logging.getLogger(__name__)


def _app_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def quarantine_base(app) -> str:
    """Quarantine root.

    Default: <app>/torrent-quarantine — inside the already-existing,
    already-writable app dir on purpose. A /tmp sibling was tried first
    and rejected: under ProtectSystem=strict every ReadWritePaths entry
    must pre-exist on the host (systemd builds sandbox mounts BEFORE
    ExecStartPre), so a fresh /tmp path fails boot with 226/NAMESPACE and
    stays fragile across tmpfiles aging. Override with
    TORRENT_QUARANTINE_FOLDER for exotic layouts.
    """
    configured = (app.config.get("TORRENT_QUARANTINE_FOLDER", "") or "").strip()
    base = os.path.abspath(configured) if configured else os.path.join(_app_root(), "torrent-quarantine")
    os.makedirs(base, mode=0o700, exist_ok=True)
    return base


def engine_log_path(app, torrent_id: str) -> str:
    """Per-torrent aria2 log file. Lives NEXT TO quarantines (never inside),
    so wipes, crash recovery, and purges never delete diagnostic history."""
    logs_dir = os.path.join(quarantine_base(app), "engine-logs")
    os.makedirs(logs_dir, mode=0o700, exist_ok=True)
    safe_id = "".join(c for c in (torrent_id or "") if c.isalnum() or c in ("-", "_"))[:64]
    return os.path.join(logs_dir, (safe_id or "unknown") + ".log")


def prune_engine_logs(app, max_age_days: int = 7) -> None:
    """Best-effort removal of engine logs older than max_age_days."""
    try:
        logs_dir = os.path.join(quarantine_base(app), "engine-logs")
        if not os.path.isdir(logs_dir):
            return
        cutoff = time.time() - max(1, max_age_days) * 86400
        for entry in os.listdir(logs_dir):
            if not entry.endswith(".log"):
                continue
            path = os.path.join(logs_dir, entry)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except Exception as e:
        _log.warning("Could not prune engine logs: %s", e)


def _tsetting(app, key: str, default: int, low: int, high: int) -> int:
    from app.models import Setting

    try:
        with app.app_context():
            raw = Setting.get(key, str(default))
        return min(high, max(low, int(raw)))
    except (ValueError, TypeError):
        return min(high, max(low, default))


def torrent_enabled(app) -> bool:
    from app.models import Setting

    try:
        with app.app_context():
            return Setting.get("torrent_enabled", "true").strip().lower() in ("1", "true", "yes")
    except Exception:
        return False


class TorrentCoordinator:
    def __init__(self, app, poll_interval: float = 2.0):
        self._app = app
        self._poll_interval = max(0.5, float(poll_interval))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._executor = None
        self._thread = None
        self._started = False
        self._inflight: dict = {}  # torrent_id -> 'fetch' | 'download'
        self._cancel_events: dict = {}

    # -- lifecycle ------------------------------------------------------
    def start(self) -> "TorrentCoordinator":
        with self._lock:
            if self._started:
                return self
            self._stop.clear()
            self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hc-torrent")
            self._started = True
            self._thread = threading.Thread(target=self._loop, name="hc-torrents", daemon=True)
            self._thread.start()
        _log.info("Recovering interrupted torrent jobs...")
        self._recover()
        prune_engine_logs(self._app)
        _log.info("Torrent coordinator started.")
        return self

    def stop(self, timeout: float = 30.0) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
        _log.info("Torrent coordinator stopping...")
        self._stop.set()
        with self._lock:
            events = list(self._cancel_events.values())
        for ev in events:
            try:
                ev.set()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        _log.info("Torrent coordinator stopped.")

    @property
    def running(self) -> bool:
        with self._lock:
            return self._started

    def cancel(self, torrent_id: str) -> bool:
        """Signal a torrent's worker (if any). Returns True if a worker holds it."""
        with self._lock:
            ev = self._cancel_events.get(torrent_id)
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass
            return True
        return False

    # -- recovery -------------------------------------------------------
    def _recover(self) -> None:
        from app.models import db, TorrentJob
        from app.torrents.engine import wipe_quarantine

        with self._app.app_context():
            stuck = TorrentJob.query.filter(
                TorrentJob.state.in_(["fetching_metadata", "downloading", "validating"])).all()
            base = quarantine_base(self._app)
            for row in stuck:
                row.state = "failed"
                row.error_message = "Interrupted by restart; quarantine wiped"
                row.completed_at = datetime.now(timezone.utc)
                wipe_quarantine(os.path.join(base, "torrent_" + (row.quarantine_token or "")))
            if stuck:
                db.session.commit()
                _log.warning("Failed %d interrupted torrent job(s) after restart", len(stuck))

    # -- poll loop ------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception:
                _log.exception("Error in torrent coordinator cycle")
            self._stop.wait(self._poll_interval)

    def _cycle(self) -> None:
        from app.models import db, TorrentJob

        if not torrent_enabled(self._app):
            return
        max_concurrent = _tsetting(self._app, "torrent_max_concurrent", 1, 1, 4)
        with self._app.app_context():
            fetch_rows = (TorrentJob.query
                           .filter_by(state="fetching_metadata", cancel_requested=False)
                           .order_by(TorrentJob.created_at.asc()).limit(4).all())
            fetch_ids = [r.id for r in fetch_rows]
            active_downloads = (db.session.query(TorrentJob)
                                .filter(TorrentJob.state == "downloading").count())
            slots = max(0, max_concurrent - active_downloads)
            dl_rows = []
            if slots > 0:
                dl_rows = (TorrentJob.query
                           .filter_by(state="downloading", cancel_requested=False)
                           .order_by(TorrentJob.created_at.asc()).limit(slots).all())
            dl_ids = [r.id for r in dl_rows]

        with self._lock:
            fetch_ids = [i for i in fetch_ids if i not in self._inflight]
            dl_ids = [i for i in dl_ids if i not in self._inflight]
            for i in fetch_ids:
                self._inflight[i] = "fetch"
            for i in dl_ids:
                self._inflight[i] = "download"
        for i in fetch_ids:
            self._executor.submit(self._fetch_worker, i)
        for i in dl_ids:
            self._executor.submit(self._download_worker, i)

    def _finish(self, tid: str) -> None:
        with self._lock:
            self._inflight.pop(tid, None)
            self._cancel_events.pop(tid, None)

    def _register_event(self, tid: str) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            self._cancel_events[tid] = ev
        return ev

    # -- workers --------------------------------------------------------
    def _fetch_worker(self, tid: str) -> None:
        from app.models import db, TorrentJob
        from app.torrents import engine as _eng

        ev = self._register_event(tid)
        try:
            with self._app.app_context():
                row = db.session.get(TorrentJob, tid)
                if row is None or row.state != "fetching_metadata":
                    return
                base = quarantine_base(self._app)
                work_dir = _eng.quarantine_for(base, row.quarantine_token)
                timeout = _tsetting(self._app, "torrent_metadata_timeout_sec", 120, 15, 600)
                log_path = engine_log_path(self._app, tid)
                if row.source_kind == "magnet":
                    meta = _eng.fetch_metadata("magnet", row.source_ref, work_dir, timeout, log_path)
                else:
                    meta = _eng.fetch_metadata(
                        "file", os.path.join(work_dir, "source.torrent"), work_dir, timeout)
                if row.cancel_requested:
                    raise _eng.TorrentCancelled("cancelled while fetching metadata")
                row.meta_json = json.dumps(meta)
                row.total_size = meta["total_size"]
                row.display_name = meta["name"][:200]
                row.state = "awaiting_selection"
                db.session.commit()
                _log.info("Torrent %s metadata ready: %d files", tid, len(meta["files"]))
        except _eng.TorrentCancelled:
            self._fail(tid, None, cancelled=True)
        except (_eng.TorrentTimeout, _eng.TorrentError) as e:
            self._fail(tid, str(e)[:500])
        except Exception as e:
            _log.exception("Torrent fetch worker failed for %s", tid)
            self._fail(tid, f"metadata fetch failed: {e}"[:500])
        finally:
            self._finish(tid)

    def _download_worker(self, tid: str) -> None:
        from app.models import db, TorrentJob
        from app.torrents import engine as _eng
        from app.torrents.validate import validate_media_file, ValidationError
        from app.utils.uploads import sanitized_filename

        ev = self._register_event(tid)
        last_write = [0.0]
        try:
            with self._app.app_context():
                row = db.session.get(TorrentJob, tid)
                if row is None or row.state != "downloading":
                    return
                meta = json.loads(row.meta_json or "{}")
                indices = [int(i) for i in json.loads(row.selected_json or "[]")]
                base = quarantine_base(self._app)
                work_dir = _eng.quarantine_for(base, row.quarantine_token)
                source = (row.source_ref if row.source_kind == "magnet"
                          else os.path.join(work_dir, "source.torrent"))
                limits = {
                    "max_peers": _tsetting(self._app, "torrent_max_peers", 50, 5, 500),
                    "bandwidth_kbps": _tsetting(self._app, "torrent_bandwidth_kbps", 0, 0, 100000),
                    "aria_timeout": 60,
                }
                timeout = _tsetting(self._app, "torrent_timeout_sec", 7200, 60, 86400)

                def _progress(per_file: dict, speed: float) -> None:
                    now = time.time()
                    if now - last_write[0] < 2.0:
                        return
                    last_write[0] = now
                    live = db.session.get(TorrentJob, tid)
                    if live is None:
                        return
                    live.progress_json = json.dumps({str(k): int(v) for k, v in per_file.items()})
                    live.speed_bps = max(0.0, float(speed))
                    db.session.commit()

                landed = _eng.run_download(meta, indices, work_dir, source, limits,
                                           progress_cb=_progress,
                                           cancel_event=ev, timeout_sec=timeout,
                                           log_path=engine_log_path(self._app, tid))
                row = db.session.get(TorrentJob, tid)
                row.state = "validating"
                db.session.commit()

                by_index = {f["index"]: f for f in meta.get("files", [])}
                validated = []
                for idx, path in landed:
                    expected = int((by_index.get(idx) or {}).get("size", 0))
                    info = validate_media_file(
                        path, expected,
                        display_name=os.path.basename((by_index.get(idx) or {}).get("path", "")))
                    safe = sanitized_filename(
                        os.path.basename((by_index.get(idx) or {}).get("path", f"file_{idx}")),
                        default=f"file_{idx}{info['extension']}")
                    validated.append((idx, path, info["size"], safe))

                from app.torrents.handoff import hand_off
                handed = hand_off(self._app, row, validated, row.cdn_account_id, work_dir)
                row = db.session.get(TorrentJob, tid)
                row.handed_json = json.dumps(handed)
                row.video_id = handed[0]["video_id"]
                row.pipeline_job_id = handed[0]["job_id"]
                row.state = "handed_off"
                row.progress_json = json.dumps(
                    {str(i): s for i, _, s, _ in validated})
                row.speed_bps = 0.0
                row.completed_at = datetime.now(timezone.utc)
                db.session.commit()
                _log.info("Torrent %s handed off %d file(s)", tid, len(handed))
        except _eng.TorrentCancelled:
            self._fail(tid, None, cancelled=True)
        except (_eng.TorrentTimeout, _eng.TorrentError, ValidationError) as e:
            self._fail(tid, str(e)[:500])
        except Exception as e:
            _log.exception("Torrent download worker failed for %s", tid)
            self._fail(tid, f"download failed: {e}"[:500])
        finally:
            self._finish(tid)

    def _fail(self, tid: str, message: Optional[str], cancelled: bool = False) -> None:
        from app.models import db, TorrentJob
        from app.torrents.engine import wipe_quarantine

        try:
            with self._app.app_context():
                row = db.session.get(TorrentJob, tid)
                if row is None:
                    return
                base = quarantine_base(self._app)
                wipe_quarantine(os.path.join(base, "torrent_" + (row.quarantine_token or "")))
                row.state = "cancelled" if cancelled else "failed"
                row.error_message = None if cancelled else (message or "failed")
                row.speed_bps = 0.0
                row.completed_at = datetime.now(timezone.utc)
                db.session.commit()
        except Exception:
            _log.exception("Could not finalize torrent %s", tid)
