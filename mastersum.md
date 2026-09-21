# Master Summary

## 1. Project Overview
HC CDN Player is a Flask-based video hosting pipeline: browser upload UI → Flask API (`run.py` + `app/__init__.py:create_app()`) → SQLAlchemy job queue (SQLite dev / PostgreSQL prod) → standalone worker process (`worker.py` → `app/worker/background.py:run_worker_forever`) → FFmpeg/ffprobe HLS transcode (`app/worker/ffmpeg_processor.py`, `app/worker/pipeline.py`) → Hack Club CDN upload (`app/cdn/hackclub.py`) → HLS watch page. Auth is custom PyJWT session cookies with optional Supabase password-grant verification (`app/auth.py`), plus a permissive fallback when Supabase is unconfigured. System stats (psutil) and CDN storage accounting are exposed via `/api/system/stats`.
Tech stack: Python 3.11, Flask 3.1.3, Flask-SQLAlchemy 3.1.1, Gunicorn (sync + threads, unix socket), psutil, requests, cryptography (Fernet), PyJWT (Flask-JWT-Extended installed but unused), Jinja2 templates, FFmpeg/ffprobe subprocesses, systemd units for web + worker.
Entry points: `run.py` (web), `worker.py` (worker loop), `migrate_jobs.py` (DDL migration), `gunicorn.conf.py`, `hc-cdn-player.service` / `hc-cdn-worker.service`, `schema.sql`.

## 2. Architecture
- `run.py` — creates Flask app via `create_app()`, serves on `0.0.0.0:$PORT` (dev/direct mode).
  → calls `app/__init__.py:create_app()`.
- `app/__init__.py` — config load, `db.init_app`, Jinja globals, blueprint registration (`auth_bp`, `views_bp`, `api_bp`), `db.create_all()`, default settings + default CDN account seed, FFmpeg/ffprobe discovery. Explicitly does NOT start worker threads.
- `app/config.py` — env-based config (secrets, DB URI, Supabase, upload limits, FFmpeg defaults). All web/worker processes share it.
- `app/auth.py` — `generate_session_jwt` / `verify_session_jwt` / `get_current_user` / `login_required`; routes `/login`, `/logout`, `/api/auth/session`. Supabase Auth via raw `requests`, else fallback auth.
  → used by `app/routes/api.py` + `app/routes/views.py` (`@login_required`).
- `app/models.py` — SQLAlchemy ORM: `CDNAccount`, `Video`, `VideoVariant`, `VideoFile`, `Job`, `JobLog`, `Setting`, `StorageSnapshot`. `Setting.get/set` helpers; `CDNAccount.set_api_key/get_api_key/masked_key/get_latest_storage/to_dict`.
- `app/routes/views.py` — server-rendered UI (`/`, `/dashboard`, `/upload`, `/watch/<id>`, `/jobs`, `/jobs/<id>`, `/cdn-accounts`, `/stats`, `/settings`). Reads DB, renders `app/templates/*.html`.
- `app/routes/api.py` — JSON API: uploads (`/videos/upload`, `/videos/upload/init`, `/videos/<id>/upload`), deletion (`DELETE /videos/<id>`), jobs (`GET /jobs/<id>`, `/cancel`, `/logs`), CDN accounts CRUD/test, `/system/stats`, `/settings`. Writes `Video`/`Job` rows, streams bytes to `UPLOAD_FOLDER/<job_id>/`.
- `app/cdn/base.py` — `CDNProvider` ABC (`test_connection`, `upload_file`, `delete_file`, `get_storage_info`).
- `app/cdn/hackclub.py` — `HackClubCDNProvider` (v4 API, 100 MB/file, 50 GB/account). Called by pipeline/deleter via manager.
- `app/cdn/manager.py` — provider registry/factory (`get_provider_instance`, `test_account`, `get_available_accounts`).
- `app/utils/security.py` — Fernet encrypt/decrypt/mask for CDN API keys. Used by `CDNAccount`.
- `app/worker/background.py` — worker loop: `_recover_interrupted_jobs` → `_cleanup_orphan_workspaces` → poll `_pick_and_start_job` every 2 s → daemon thread `_run_job` → `execute_video_pipeline` / `execute_video_deletion`.
- `app/worker/pipeline.py` — transcode+upload stages: validating → inspecting_media → encoding (`_run_ffmpeg_variant`) → generating_hls → uploading_cdn → verifying_cdn → cleaning_up → completed; `JobController` cancel signalling; `request_job_cancel` (called cross-process from API).
- `app/worker/ffmpeg_processor.py` — `inspect_video` (ffprobe JSON), `determine_quality_targets` (original + one step-down), `build_ffmpeg_transcode_command` (libx264/AAC HLS), `extract_thumbnail`.
- `app/worker/deleter.py` — `execute_video_deletion`: iterate `VideoFile(uploaded)`, `delete_file` each, mark `deleted`, mark `Video.status='deleted'`.
- `worker.py` — standalone entry: `create_app()` + `run_worker_forever(app)`; installs SIGTERM/SIGINT handler (broken, see Bugs).
- `migrate_jobs.py` — adds 13 `jobs` columns (SQLite PRAGMA check / PG `ADD COLUMN IF NOT EXISTS`), then `db.create_all()`.
- `schema.sql` — full DDL + seed settings + Supabase RLS policies (all tables `FOR ALL TO authenticated`).
- `app/templates/*.html` + `app/static/css/claymorphism.css` — Jinja UI (dashboard, upload with streaming PUT, watch with hls.js, jobs/job_detail polling, stats dashboard, settings, login, cdn_accounts).
- `tests/test_background_worker.py` — stale unit test referencing non-existent worker APIs (always fails).
- `gunicorn.conf.py`, `hc-cdn-*.service`, `.env.example`, `requirements.txt` — prod runtime config.

Data/control flow:
```
browser → views (Jinja) / api (JSON, @login_required)
  → Video+Job rows + file bytes in /tmp/video-processing/<job_id>/
  → worker poll (2 s) claims oldest queued → thread
  → ffprobe → ffmpeg variants → thumbnail → master.m3u8
  → CDN upload per file → VideoVariant/VideoFile rows
  → verify (log-only) → rmtree work_dir → job completed / video ready
cancel: browser → POST /api/jobs/<id>/cancel → cancel_requested=True → worker _check_cancel raises JobCancelled
delete: browser → DELETE /api/videos/<id> → delete_video job → deleter → CDN delete_file per VideoFile
```

## 3. File Inventory
| file path | one-line purpose | notable dependencies |
|---|---|---|
| `run.py` | Flask dev/direct entry (`create_app()`, `app.run`) | `app`, `os` |
| `worker.py` | Standalone worker entry + signal handlers | `app.create_app`, `app.worker.background` |
| `app/__init__.py` | App factory, DB init, blueprint registration, seeding, ffmpeg discovery | `flask`, `app.config`, `app.models`, `app.auth`, `app.routes.*`, `shutil` |
| `app/config.py` | Env-driven configuration with insecure defaults | `os`, `dotenv`, `datetime` |
| `app/models.py` | SQLAlchemy ORM + `Setting.get/set`, storage fallback | `flask_sqlalchemy`, `app.utils.security` |
| `app/auth.py` | JWT session issue/verify, Supabase login + fallback, `login_required` | `PyJWT`, `requests`, `flask` |
| `app/routes/views.py` | Server-rendered UI routes | `flask`, `app.auth`, `app.models` |
| `app/routes/api.py` | Upload/job/CDN/settings/system-stats JSON API | `flask`, `werkzeug.secure_filename`, `psutil`, `shutil`, `app.worker.pipeline` |
| `app/cdn/base.py` | `CDNProvider` abstract interface | `abc` |
| `app/cdn/hackclub.py` | Hack Club CDN v4 upload/delete/stats/test | `requests`, `urllib.parse` |
| `app/cdn/manager.py` | Provider registry + factory | `app.cdn.*`, `app.models` |
| `app/utils/security.py` | Fernet encrypt/decrypt/mask for API keys | `cryptography.fernet`, `hashlib`, `flask` |
| `app/worker/background.py` | Queue poll loop, crash recovery, orphan cleanup, thread dispatch | `flask`, `app.models`, `threading`, `shutil` |
| `app/worker/pipeline.py` | Full transcode→HLS→CDN pipeline with progress + cancel | `subprocess`, `psutil`, `requests`, `app.cdn.manager`, `ffmpeg_processor` |
| `app/worker/ffmpeg_processor.py` | ffprobe inspect, ladder, ffmpeg command builder, thumbnail | `subprocess`, `json`, `shutil` |
| `app/worker/deleter.py` | CDN file deletion job executor | `app.cdn.manager`, `app.models`, `flask` |
| `app/cdn/__init__.py` | Package marker | — |
| `app/worker/__init__.py` | Package marker | — |
| `app/utils/__init__.py` | Package marker | — |
| `migrate_jobs.py` | Adds 13 `jobs` columns safely (sqlite + pg) | `dotenv`, `sqlalchemy.text`, `app` |
| `schema.sql` | Canonical DDL + settings seed + RLS policies | Postgres/SQLite |
| `gunicorn.conf.py` | Prod gunicorn tuning (2 workers, 4 threads, socket, 300 s timeout) | `multiprocessing` (unused) |
| `hc-cdn-player.service` | systemd unit for web (gunicorn socket) | — |
| `hc-cdn-worker.service` | systemd unit for worker (MemoryMax 1600M) | — |
| `requirements.txt` | Pinned Python deps | — |
| `tests/test_background_worker.py` | Stale/broken worker unit test | `unittest`, `unittest.mock` |
| `.env.example` | Documented env template (placeholders only) | — |
| `app/templates/base.html` | Layout, nav, session-warning + GitHub SHA widget JS | Jinja, vanilla JS |
| `app/templates/dashboard.html` | Video table + delete-to-job JS | Jinja, fetch |
| `app/templates/upload.html` | Init→streaming-PUT upload UI with progress/cancel | Jinja, XHR |
| `app/templates/watch.html` | hls.js player (manual quality lock) + metadata | Jinja, hls.js |
| `app/templates/jobs.html` | Job list + cancel buttons | Jinja |
| `app/templates/job_detail.html` | Per-job polling + cancel + log tail | Jinja |
| `app/templates/cdn_accounts.html` | Add/test/delete CDN accounts | Jinja, fetch |
| `app/templates/stats.html` | Live CPU/RAM/disk/net/CDN dashboard (~40 KB JS) | Jinja, fetch, psutil-backed API |
| `app/templates/settings.html` | FFmpeg settings form | Jinja |
| `app/templates/login.html` | Email/password login form | Jinja |
| `app/static/css/claymorphism.css` | Global styles | — |
| `master.md` | Pre-existing human overview (stale: claims worker starts in `create_app`) | — |
| `context.md` | Pre-existing LLM context doc | — |

Review coverage note: all Python source, SQL, services, config, and `.env.example` fully read. Templates scanned for `|safe`/innerHTML/XSS sinks and auth-gating (all routes carry `@login_required`); full JS logic in `stats.html`/`upload.html` only skimmed. `claymorphism.css` not audited. `.env` exists locally but was NOT read (untracked, gitignored — verified via `git ls-files` that only `.env.example` is tracked).

## 4. Bugs Found
1. `app/auth.py:111-116` — fallback accepts ANY email with password ≥ 6 chars when Supabase unconfigured (`user_id = f"user_{hash(email)}"`, `authenticated=True`). Any anonymous user gets a valid 24 h JWT. Why: auth bypass, full access to uploads/CDN keys/jobs. Severity: critical. Confidence: high.
2. `app/routes/api.py:183` + `app/routes/api.py:264-265` — path traversal / absolute-path overwrite. `upload_video_init` stores `filename` raw (no `secure_filename`); `upload_video_streamed` does `os.path.join(work_dir, video.original_filename)` — a name like `../../x` stays inside-ish but `/etc/cron.d/p` (absolute) makes `join` return the absolute path, so the subsequent `open(save_path,'wb')` overwrites arbitrary files as `hc-cdn` user. Contrast `upload_video_stream` which does sanitize (`api.py:88`). Why: arbitrary file write. Severity: critical. Confidence: high.
3. `app/routes/api.py:70-80,268-278` + `app/routes/api.py:309-315` — `Content-Length`-only size enforcement is bypassable. If header is missing/spoofed (`cl=0`), the 413 check is skipped and the chunk loop (`while True: ...read(4MB)`) has no running `bytes_written > max_bytes` abort, so arbitrarily large bodies are written until disk fills. Multipart fallback `f.save(save_path)` (`api.py:289`) has no size check at all. Why: disk-fill DoS + limit bypass. Severity: high. Confidence: high.
4. `worker.py:28-36` + `app/worker/background.py:183-189` — graceful-shutdown flag never read. `_handle_signal` sets `_shutdown_requested=True` but `run_worker_forever` loops `while True: ...; time.sleep(2)` unconditionally; daemon job threads + ffmpeg children are SIGTERM-killed mid-encode. Why: corrupt partial encodes, jobs stuck `processing` until next `_recover_interrupted_jobs`. Severity: high. Confidence: high.
5. `tests/test_background_worker.py:7,27-28,34-38` — test imports `background.start_job_thread` and `background.has_active_job_for_video`, neither of which exists in `app/worker/background.py` (actual API is `_pick_and_start_job`/`_run_job`/`run_worker_forever`). Suite errors with `AttributeError` unconditionally. Why: no working regression coverage; refactors ship untested. Severity: high. Confidence: high.
6. `app/cdn/hackclub.py:39-45` — `test_connection` returns `(True, "API Key validated"/"API Key format valid")` whenever `len(key)>=8`, even after non-401 HTTP errors or network exceptions. Why: invalid/offline keys report "successful", users discover failure hours later during upload. Severity: high. Confidence: high.
7. `app/worker/pipeline.py:300-371` — ffmpeg supervision gaps: (a) cancel is only polled per stdout line (`_check_cancel` at `pipeline.py:365`), so a stalled ffmpeg emitting nothing is un-cancellable; (b) `proc.wait()` (`pipeline.py:370`) has no timeout, hangs forever on hung ffmpeg; (c) `stderr_lines` grows unbounded for long encodes. Why: stuck `processing` jobs, worker slot exhaustion (`max_concurrent_jobs=1` default), memory growth. Severity: high. Confidence: medium-high.
8. `app/worker/background.py:89-114` — job claim is not atomic. `count(processing)` → `SELECT oldest queued` → `UPDATE+commit` are separate statements with no `FOR UPDATE SKIP LOCKED`/unique guard; two worker processes (explicitly suggested as scaling path in `context.md`) can claim the same job → double encode + double CDN upload + duplicate `VideoVariant`/`VideoFile` rows. Why: race under documented scaling topology. Severity: high (medium as-deployed single worker). Confidence: medium.
9. `app/routes/api.py:838-851` — settings POST has no validation/error handling for most fields. `int(data['ffmpeg_threads'])` / `int(data['max_concurrent_jobs'])` raise 500 on non-numeric input; `ffmpeg_preset` stored verbatim and interpolated into ffmpeg argv (`ffmpeg_processor.py:194`), `ffmpeg_crf`/`hls_segment_duration` unchecked (CRF valid 0-51, seg duration must be >0 — 0 breaks `keyint = seg*fps` → `max(1,...)` hides it but produces 1-frame GOPs). Why: 500s + self-inflicted pipeline breakage. Severity: medium. Confidence: high.
10. `app/worker/ffmpeg_processor.py:72-73,81-86` — unguarded numeric parses of ffprobe output: `float(format_data.get('duration',0.0))`, `int(bit_rate)`, `float(num)/float(den)` for `r_frame_rate`. Crafted/corrupt inputs (`duration:"N/A"`, `r_frame_rate:"0/0"` handled but `"ntsc"`/`""` not) raise `ValueError`/`ZeroDivisionError` outside the `try` that only wraps `subprocess.run`. Why: one weird file fails job with confusing traceback instead of clean "unreadable source" error. Severity: medium. Confidence: high.
11. `app/worker/ffmpeg_processor.py:189-190` — `-c:v copy` fast-path for h264 originals keeps `-g/-keyint_min/-hls_time` flags that `copy` cannot honor (no re-encode → no forced keyframes). Why: HLS segments not starting on keyframes → stutter/unseekable variant on some players. Severity: medium. Confidence: medium.
12. `app/auth.py:105` — `resp.json()` called up to 3× on the Supabase error path with no `try` for `ValueError`; non-JSON error body raises, caught by outer `except` → 500 `Supabase Auth service error` instead of 401, confusing login failures. Why: wrong status + error text. Severity: medium. Confidence: high.
13. `app/worker/pipeline.py:657` vs `pipeline.py:605,803` — inconsistent bitrate math: variant row uses `w*h*3.5` (drops `fps`) while master playlist uses `w*h*3.5*fps`. Why: stored `bitrate` disagrees with `BANDWIDTH` by ~30×, misleading stats/ABR. Severity: medium. Confidence: high.
14. `app/worker/deleter.py:115-118` — unconditional success logs (`✓ CDN files deleted`, `✓ Playlists deleted`, `✓ Thumbnail deleted`) even when `failed_count>0`; progress (`deleter.py:111`) uses `deleted_count` only so partial failure still marches to 95%→100% `completed`. Why: operators believe CDN purge succeeded when files remain (cost + stale content). Severity: medium. Confidence: high.
15. `app/worker/pipeline.py:214-223` — `_choose_safe_ffmpeg_threads` silently caps user-configured threads (24/32/40 by RAM) with only a log line; on the documented 2 GB server `threads=40` always becomes ≤24. Why: UI setting lies; perf tuning confusion. Severity: low-medium. Confidence: high.
16. `app/routes/api.py:136,321,345-349` + `app/worker/pipeline.py:345-362` — hot DB writes with no locking/backoff: upload progress `db.session.refresh(job)+commit` every 2 s per upload, ffmpeg progress commit every 0.5 s, cancel poll `Job.query.get` per stdout line. Under default SQLite + `threads=4` gunicorn this yields `database is locked` 500s and lost progress. Why: perf + reliability on the default deployment target. Severity: medium. Confidence: medium.
17. `app/routes/api.py:721-724` + `app/models.py:69` — full-table Python sums (`for f in VideoFile.query...all()`, `sum(f.file_size...)`) on every `/system/stats` and dashboard render. Why: O(n) memory/time, OOM/slowdown at scale; should be `func.sum`. Severity: medium. Confidence: high.
18. `app/cdn/hackclub.py:228-249` — `get_storage_info` swallows all exceptions and returns `used=0 / available=50GB`, which the dashboard renders as fact. Why: outage/quota-exhaustion invisible; capacity calculator (`api.py:730-734`) overstates headroom. Severity: medium. Confidence: high.
19. `app/auth.py:113` — `user_{hash(email)}`: CPython `hash()` is salted per process (`PYTHONHASHSEED`), so the same email maps to different `sub` after every restart (and can be negative). Why: unstable identity, breaks per-user audit/ownership later. Severity: low-medium. Confidence: high.
20. `app/routes/api.py:59` — `os.path.splitext(file.filename)[0]` assumes non-None filename; empty/None filename raises `TypeError` → 500 instead of 400. `secure_filename('')` (`api.py:88`) can also yield `''` → `save_path` is the work dir → `open(dir,'wb')` fails into the generic cleanup path. Why: unhandled 500 on trivial bad input. Severity: low-medium. Confidence: medium.
21. `app/routes/api.py:369-370` — streamed-upload failure path returns 500 but (unlike `upload_video_stream:154-164`) leaves the partial file + `Video` + `receiving` `Job` behind; only a worker restart flips them to `failed`. Why: orphan rows/files accumulate. Severity: medium. Confidence: high.
22. `app/routes/views.py:21-23` + `app/routes/api.py:715-716` — `Setting.get` values used as numbers without validation (`int()` in pipeline will throw on corrupt strings, `round(total/…)` in dashboard ok). Corrupt single settings row crashes pipeline (`pipeline.py:532-542`). Why: single bad row DoS's all encodes. Severity: low-medium. Confidence: medium.
23. `app/models.py:247` — `round(self.progress, 1)` in `Job.to_dict` throws `TypeError` if `progress` is `None` (possible on legacy rows pre-migration). Why: `GET /api/jobs/<id>` 500 for old jobs. Severity: low-medium. Confidence: medium.
24. `app/routes/api.py:136` — `db.session.refresh(job)` inside the upload loop raises if the row was concurrently cancelled/deleted; uncaught → 500 mid-upload. Why: cancel-during-upload races to 500 instead of clean abort. Severity: low-medium. Confidence: medium.
25. `app/routes/api.py:379-406` — `DELETE /videos/<id>` queues a deletion job unconditionally: no check for existing `processing`/`delete_pending` jobs, so double-DELETE (or delete-during-transcode) creates competing transcode+delete pipelines on the same `video_id`. Why: duplicate CDN deletes/uploads, confusing terminal state. Severity: medium. Confidence: medium.
26. `app/worker/pipeline.py:780` — playlist-upload byte accounting uses `os.path.getsize(p_path)` (original) while the bytes actually sent are the rewritten `upload_src` (`.cdn`), which differ. `total_cdn_files`/`files_done` counted but never used for progress (bytes used instead). Why: progress/ETA drift + dead vars. Severity: low. Confidence: high.
27. `app/worker/pipeline.py:595-596` — `extract_thumbnail` return ignored; failure only surfaces as empty `thumbnail_url` with no warning log. Why: silent missing thumbnails. Severity: low. Confidence: high.
28. `app/worker/pipeline.py:810` — bare `except: pass` around master-playlist rewrite uploads the stale file silently. Why: hidden failure mode. Severity: low. Confidence: high.
29. `app/routes/api.py:560-561` — `physical_cores or 48`, `logical_threads or 92` hard-coded fallbacks presented as real hardware data. Why: misleading inventory on non-Linux/odd hosts. Severity: low. Confidence: high.
30. `app/routes/api.py:691` — `GET /system/stats` calls `os.makedirs(upload_folder)` (write side-effect in a read endpoint) and `get_cached_temp_folder_size` walks the whole upload tree inline (up to seconds of blocking per poll). Why: slow stats + surprising mutation. Severity: low-medium. Confidence: medium.
31. `app/cdn/manager.py:20` — unknown `provider` strings silently fall back to `HackClubCDNProvider`. Why: misconfiguration hidden. Severity: low. Confidence: high.
32. `app/cdn/hackclub.py:86-226` vs `app/cdn/base.py:28` — `delete_file` returns `(bool, attempts)` tuple while the ABC declares `-> bool`; only `deleter.py:71-74` handles both shapes. Why: contract violation traps future callers. Severity: low-medium. Confidence: high.
33. `app/worker/pipeline.py:110-147` + comment `pipeline.py:140-142` — `request_job_cancel`'s in-process `ctrl.request_cancel()` is dead in production (web and worker are separate processes; acknowledged in the comment). Why: dead path, false impression of instant kill. Severity: low. Confidence: high.
34. `app/worker/background.py:135-154` — `_run_job` opens a second `app.app_context()` inside the caller's context; nested push is redundant. `background.py:143,155` + ~15 sites in `pipeline.py`/`deleter.py`/`api.py` use legacy `Query.get()` (SQLAlchemy 2.x `LegacyAPIWarning`, removal-track). Why: tech debt + log noise. Severity: low. Confidence: high.
35. `requirements.txt:3,5,6,7` — `Flask-JWT-Extended==4.7.1` and `supabase==2.31.0` are installed but never imported (auth uses raw `PyJWT`+`requests`); `cryptography==50.0.0` / `psutil==7.0.0` exceed the highest versions published at review time (44.x / 6.x-7.x boundary) — installs may fail on a fresh venv. Why: bloat + broken fresh-install risk. Severity: low-medium. Confidence: medium (verify with `pip index` before changing).
36. `app/models.py:40-41` — `created_at`/`updated_at` use `default=utc_now` (called without parens → correct) but `updated_at` also needs `onupdate=utc_now` (present) — however SQLite `DateTime(timezone=True)` drops tzinfo, so `datetime.fromisoformat(since)` (naive, `api.py:454`) compared against aware DB timestamps raises `TypeError: can't compare offset-naive and offset-aware` on some backends. Why: `/logs?since=` 500s depending on backend. Severity: low-medium. Confidence: medium.

## 5. Security Concerns
S1. `app/auth.py:111-116` — authentication bypass via fallback (any email + 6-char password mints a valid JWT when Supabase unset). Same as Bugs §4.1. Severity: critical. Confidence: high.
S2. `app/config.py:8-9` — hard-coded default `SECRET_KEY`/`JWT_SECRET_KEY` (`default-dev-secret-key-change-in-prod`). Any deployment missing env vars shares keys → JWT forgery + session signing bypass. Severity: critical. Confidence: high.
S3. `app/routes/api.py:183,264-265` — upload filename path traversal / absolute-path file overwrite (no `secure_filename` on the init→streamed path). Same as Bugs §4.2. Severity: critical. Confidence: high.
S4. `app/utils/security.py:11-15` — hard-coded fallback Fernet material (`"default-fallback-secret-key-32bytes-long!!"` when `CDN_ENCRYPTION_KEY` empty, incl. `RuntimeError` path). All such installs encrypt CDN keys under the same SHA-256-derived key → any holder of source can decrypt any leaked DB. Severity: high. Confidence: high.
S5. `app/__init__.py:48-55` — seeds a `Primary Hack Club CDN` account with literal `hackclub_default_demo_api_key` when table empty. If an operator misses setup, uploads attempt with a known-bad key (info leak in logs) or, worse, the row is later edited in place leaving confusion about which key is live. Severity: medium. Confidence: high.
S6. `app/auth.py:97,129-135` + `app/config.py:14-15` — session cookie without `Secure`, JWT CSRF protection off (`JWT_COOKIE_CSRF_PROTECT=False`), 24 h lifetime, and the raw `access_token` also returned in JSON body (encourages `localStorage`, XSS-stealable) while the cookie is `HttpOnly` — mixed guidance. No `__Host-` prefix / no rotating refresh tokens. Severity: medium. Confidence: high.
S7. `app/auth.py:66-136` — `/login` has no rate limiting, lockout, or CAPTCHA; Supabase errors are proxied with attacker-influenced text (`S7b: auth.py:105` double-`resp.json()` can leak upstream internals via 500). Severity: medium. Confidence: high.
S8. `app/routes/api.py:55-170,242-370` — missing upload content validation: no extension/MIME allowlist, no magic-byte sniffing, no per-chunk size cap (see §4.3). Any authenticated user (trivially obtainable per S1) can store arbitrary bytes that ffmpeg later parses (ffprobe CVEs surface) and that fill disk. Severity: high (medium if S1 fixed). Confidence: high.
S9. `app/routes/api.py:483-488,491-496` — CDN account create/test endpoints accept arbitrary `provider` strings and echo `test_connection` messages containing upstream `response.text` (`hackclub.py:84`); stored keys are re-encrypted server-side but creation has no strength check and `DELETE /cdn-accounts/<id>` (`api.py:499-505`) has no guard against deleting the account backing in-flight jobs/videos. Severity: low-medium. Confidence: medium.
S10. `app/templates/dashboard.html:175`, `app/templates/cdn_accounts.html:97` — account/video names interpolated into inline `onclick="...(..., '{{ acc.name }}')"` with only single-quote escaping (`|replace("'", "\\'")`); names containing `</script>`, backticks, or `\` break out → stored XSS executing in an authenticated session (can call DELETE/upload APIs with cookies). `watch.html:155` injects `master_playlist_url` into a JS string literal unescaped (`const masterUrl = "{{ ... }}"`) — a malicious URL with `";...` breaks out. Jinja autoescape covers HTML-body sinks (`watch.html:129,138` ok), but these JS-string sinks are not `|tojson`-encoded. Severity: medium. Confidence: medium (flagged from static read; confirm with payload test).
S11. `app/templates/stats.html:502-852`, `base.html:306` — `innerHTML` sinks fed by `/api/system/stats` (mount paths, CPU model, job messages) and the GitHub commits widget (author names, messages) without sanitization. A poisoned mount label/job message or malicious commit payload becomes script execution. Severity: low-medium. Confidence: medium.
S12. `app/worker/ffmpeg_processor.py:17-43,222-238` — `input_path`/`output_path` reach `subprocess` argv (list form, no `shell=True` — good) but originate from upload filenames/DB without canonicalization; combined with S3 this is the overwrite primitive. Separately, attacker-controlled media is parsed by ffmpeg/ffprobe (large attack surface, no sandbox/seccomp/nice/ulimit). Severity: medium. Confidence: medium.
S13. `app/utils/security.py:38-40` — `decrypt_api_key` swallows all exceptions and returns `""`; callers (`models.py:55`, `manager.py:21`) proceed with an empty key, producing confusing 401s and masking key-rotation breakage (fail-closed would be safer). Severity: low-medium (availability/integrity). Confidence: high.
S14. `migrate_jobs.py:69` — prints `db_uri[:80]` to stdout/journald; Postgres URIs embed `user:password@`, so DB credentials land in logs. Severity: low. Confidence: high.
S15. `schema.sql:150-189` — blanket RLS `FOR ALL TO authenticated USING (true) WITH CHECK (true)` gives every Supabase-authenticated user full read/write on CDN keys, videos, jobs, settings; no ownership scoping, and it is irrelevant anyway because the Flask app connects via SQLAlchemy (bypasses PostgREST/RLS) — defense-in-depth theater. Severity: low (document, tighten if Supabase REST is ever used directly). Confidence: medium.
S16. `.env` hygiene: `.env` exists in the working tree but is correctly gitignored/untracked (verified `git ls-files` shows only `.env.example`); `.env.example:9` ships a realistic-looking Fernet sample — ensure nobody copies it verbatim to prod (overlaps S4). Severity: low. Confidence: high.

## 6. Code Smells / Cleanup Candidates
- Dead code / unreachable:
  - `pipeline.py:431-448` legacy `video.id`-dir move: current upload endpoints only write job-scoped dirs, so the branch never fires.
  - `pipeline.py:689-691` `total_cdn_files`/`files_done` counted, never used for progress.
  - `pipeline.py:140-145` cross-process controller poke (self-documented dead).
  - `gunicorn.conf.py:22` `import multiprocessing` unused.
  - Unused deps: `Flask-JWT-Extended`, `supabase` (see §4.35).
- Duplication:
  - Upload-size + disk checks repeated 3× (`upload_video_stream`, `upload_video_init`, `upload_video_streamed`) — extract one `validate_upload_request()` helper.
  - Master-playlist writer duplicated (`pipeline.py:601-609` vs `800-809`); bandwidth formula `w*h*3.5*fps` in 3 places.
  - `VideoFile` insert blocks repeated ~5× in pipeline — helper `record_cdn_file()`.
  - `_log_job` (`pipeline.py:164`) vs `log_delete_job` (`deleter.py:7`) — same shape, different modules.
  - Temp-size walk (`api.py:628-646`) vs disk-mount walk vs stats assembly — move to a `system_stats` util with caching+lock.
- Inconsistent patterns:
  - `secure_filename` on one upload path, raw `request.form.get('filename')` on the other (§4.2).
  - Cleanup on failure in `upload_video_stream` but not `upload_video_streamed` (§4.21).
  - `delete_file` tuple vs ABC `bool` (§4.32); `to_dict(include_storage)` called with both `True/False` inconsistently.
  - Deprecated `Model.query.get()` in ~15 sites (all listed in §4.34) — migrate to `db.session.get(Model, id)`.
  - Bare `except Exception: pass` swallows (e.g. `__init__.py:78-80`, `api.py:454-457`, `pipeline.py:810`).
  - Logging: `LOG_TO_STDOUT` print-guards scattered in `pipeline.py:182-186`, `deleter.py:21-25`, `hackclub.py:150-158` — use stdlib `logging`.
- TODO/FIXME/HACK: none found via repo-wide grep (only hit is the word "HACKCLUB_CDN_API_KEY" in `context.md:106`). No `TODO` comments in Python/HTML.
- Missing validation (recap): settings ranges (§4.9), upload MIME/size (§4.3/S8), ffprobe numeric guards (§4.10), `?since=` datetime shape (§4.36).
- Stale docs: `master.md:10` claims `create_app()` "starts the background worker" (false — worker is separate since the split); `context.md:97` documents TCP `0.0.0.0:$PORT` while `gunicorn.conf.py:37` binds a unix socket; `deleter.py:28` docstring implies DB rows removed while code keeps them as `deleted`.
- Frontend hygiene: inline `onclick` + `innerHTML` templating (S10/S11); no CSP header set anywhere; `|tojson` not used for JS-string interpolation.

## 7. Suggested Fixes (Optional Quick Wins)
1. S1 / §4.1 (fallback auth bypass): gate the fallback behind an explicit env flag (e.g. `ALLOW_DEV_FALLBACK=true`) that defaults to off and refuses to start in production without Supabase; fail closed with 503 "auth not configured". One `if` + startup assert in `create_app()`.
2. S3 / §4.2 (upload path traversal): `filename = secure_filename(request.form.get('filename',''))` in `upload_video_init`, reject empty results with 400; in `upload_video_streamed` resolve `save_path` then assert `os.path.realpath(save_path).startswith(os.path.realpath(work_dir)+os.sep)`.
3. S2/S4 (§4.2-config/§4.4-encryption defaults): on startup, `raise RuntimeError` if `SECRET_KEY`/`JWT_SECRET_KEY` still equal the `default-*` sentinels or `CDN_ENCRYPTION_KEY` is empty (or generate+persist once with a loud warning in dev only). Never ship fallback constants.
4. §4.3 (streaming size bypass): track `bytes_written` in both chunk loops and abort with 413 + cleanup once `> max_bytes`; apply the same cap to the `f.save()` branch (check `Content-Length` first, then post-save size with delete-on-exceed).
5. §4.4 (worker shutdown): replace the ignored flag with a `threading.Event`; `run_worker_forever` waits on it (`event.wait(2)`), and on set stops claiming jobs, terminates active `JobController` procs, and joins threads before exit; set `TimeoutStopSec` to cover worst-case SIGTERM→SIGKILL.
6. §4.5 (broken tests): rewrite `tests/test_background_worker.py` against the real API (`_pick_and_start_job` with an in-memory SQLite app + `_recover_interrupted_jobs`), or delete it so CI is not red-by-default; add a smoke test for `request_job_cancel` queued→cancelled.
7. §4.6/§4.18 (CDN test + storage lies): `test_connection` must return `(False, ...)` on non-2xx/exception (drop the `len>=8` fallback); `get_storage_info` should raise or return `{"unknown": True}` so the UI can render "unreachable" instead of fake 50 GB free.
8. §4.7 (ffmpeg hangs): add `proc.wait(timeout=…)` + kill fallback, bound `stderr_lines` (e.g. `collections.deque(maxlen=200)`), and poll `cancel_requested` on a timer (every 1-2 s) in addition to per-stdout-line.

*Audit method: full sequential read of every tracked source file listed in §3 (plus template XSS-sink scan and `git ls-files`/`git status` verification). No source files were modified; only this `mastersum.md` is created.*
