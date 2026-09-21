# Migration note — unified single-process build

## What changed structurally

- **The separate worker process is gone.** `worker.py` (the `hc-cdn-worker.service`
  entrypoint) no longer runs a job loop; it is a deprecated shim that prints a
  warning. `app/worker/background.py`'s standalone `run_worker_forever` loop is
  kept only as a compatibility wrapper.
- **One process does everything.** `create_app()` (`app/__init__.py`) now starts
  an in-process `JobSupervisor` (`app/worker/supervisor.py`): a poll thread plus
  a bounded `ThreadPoolExecutor` that runs the same transcode/upload/delete
  pipeline code as before. `run.py` is the single entrypoint for web + jobs.
- **Job claiming is atomic.** Queued jobs are claimed with `SELECT … FOR UPDATE
  SKIP LOCKED` (PostgreSQL) + a compare-and-swap `UPDATE … WHERE status='queued'`,
  so even two pollers can never double-execute a job.
- **Shutdown is real.** SIGTERM/SIGINT (and interpreter exit via `atexit`) stop
  polling, terminate in-flight ffmpeg processes through their job controllers,
  await executor threads, then exit. The old "shutdown flag set but never read"
  bug is fixed via a process-wide shutdown event polled at every stage boundary
  and every second inside ffmpeg supervision.
- **Crash recovery kept.** On every boot, jobs left `processing` are re-queued
  and jobs left `receiving` (interrupted uploads) are marked failed — same as
  the old worker, now running inside the app process.
- **Health probes:** `GET /up` (liveness), `GET /ready` (DB + scheduler),
  `GET /health` (deep: DB, ffmpeg/ffprobe, disk, per-CDN reachability; 200/503).
- **Logging** is stdlib `logging` to stderr (journald-ready); scattered `print()`
  paths were removed.
- **Security fixes bundled in:** fail-closed auth (no more any-email bypass;
  loopback-only dev mode only with `ALLOW_INSECURE_DEV_AUTH=true`), jailed
  uploads with real byte-count limits, no default secrets (production boot
  refuses placeholders), honest CDN test/storage results, ffmpeg timeouts +
  bounded buffers + stdout-independent cancel, validated settings, `|tojson` +
  event-bound frontend with no `onclick`/`innerHTML` sinks.

## Operator setup changes

1. **Remove the worker unit** (do this on deploy):
   `sudo systemctl stop hc-cdn-worker.service; sudo systemctl disable hc-cdn-worker.service; sudo rm /etc/systemd/system/hc-cdn-worker.service`
2. **Install the updated unit:** copy the new `hc-cdn-player.service` (single
   unit, includes `/ready` `ExecStartPost` gate and a 90 s stop timeout for
   in-flight encodes), then `daemon-reload`, `enable`, `restart hc-cdn-player`.
3. **New environment variables** (see `.env.example`): `ENABLE_JOB_SCHEDULER`
   (default `true`), `JOB_POLL_INTERVAL` (default `2.0`),
   `FFMPEG_VARIANT_TIMEOUT_SEC` (default `7200`), `ALLOW_INSECURE_DEV_AUTH`
   (default `false` — dev only), `LOG_LEVEL` (default `INFO`), `WEB_CONCURRENCY`
   (default `1`).
4. **Secrets are enforced:** production boot now requires real `SECRET_KEY`,
   `JWT_SECRET_KEY` (≥32 chars, non-placeholder) and `CDN_ENCRYPTION_KEY`, plus
   real `SUPABASE_URL`/`SUPABASE_ANON_KEY` (or dev mode, loopback only).
5. **ffmpeg/ffprobe must be installed** whenever the scheduler is enabled
   (otherwise the app exits at startup with a clear error instead of failing
   jobs later). `Ffmpeg` paths can be overridden with `FFMPEG_BIN`/`FFPROBE_BIN`.
6. **Gunicorn:** `workers` now defaults to 1 (`WEB_CONCURRENCY`) and
   `preload_app` is `false` (required — the app owns threads post-fork).
