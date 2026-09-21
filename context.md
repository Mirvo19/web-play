# HC CDN Player – Context Overview

## Project Purpose
HC CDN Player is a Flask‑based web application that provides a video upload interface, transcodes uploaded videos into HLS (HTTP Live Streaming) variants using **FFmpeg**, and publishes the resulting segments and playlists to a Content Delivery Network (CDN). It tracks metadata, job state, and CDN usage via a SQLAlchemy‑backed SQLite/PostgreSQL database.

## Primary Features
- **Video Upload UI** – drag‑and‑drop or file‑select upload page.
- **Background Processing** – asynchronous pipeline for transcoding, thumbnail extraction, master playlist generation, and CDN upload.
- **CDN Integration** – pluggable provider interface (`app/cdn/`) with a built‑in Hack Club CDN implementation.
- **Job Management** – queue, monitor, and delete jobs via REST API and web UI.
- **Analytics & Stats** – server‑side CPU/RAM/disk monitoring and CDN storage statistics.
- **Authentication** – JWT‑based session cookies with optional Supabase Auth fallback.

## Technology Stack
- **Backend**: Python 3.11, Flask, Gunicorn, Flask‑SQLAlchemy, Flask‑JWT‑Extended
- **Database**: SQLite (development) / PostgreSQL (production) via SQLAlchemy
- **Background Worker**: Thread‑based worker (`app/worker/`) that polls the `jobs` table.
- **Video Processing**: FFmpeg & FFprobe executed via subprocess.
- **CDN**: Abstract `CDNProvider` interface; default implementation for Hack Club CDN.
- **Security**: Fernet symmetric encryption for storing CDN API keys (`app/utils/security.py`).
- **System Monitoring**: `psutil` for live CPU/RAM/disk stats.
- **Deployment**: Systemd service files (`hc-cdn-player.service`, `hc-cdn-worker.service`) running Gunicorn and the worker under a dedicated `hc-cdn` user.
- **Static Assets**: Jinja2 templates, CSS (including glass‑morphism styles).

## Repository Layout
```
.
├─ app/                     # Flask package
│  ├─ __init__.py          # create_app() – config, DB, blueprints
│  ├─ auth.py              # JWT auth helpers
│  ├─ config.py            # Env‑based configuration
│  ├─ models.py            # SQLAlchemy ORM definitions
│  ├─ routes/
│  │   ├─ api.py           # `/api/*` endpoints
│  │   └─ views.py         # UI routes (`/dashboard`, `/upload`, …)
│  ├─ cdn/                 # CDN abstraction layer
│  │   ├─ base.py
│  │   ├─ hackclub.py
│  │   └─ manager.py
│  ├─ utils/
│  │   └─ security.py      # encryption utilities
│  └─ worker/              # Background processing
│       ├─ background.py     # worker thread entrypoint
│       ├─ pipeline.py      # transcode & upload logic
│       ├─ ffmpeg_processor.py
│       └─ deleter.py       # remote file deletion pipeline
├─ static/                  # CSS, images, JS
├─ templates/              # Jinja2 HTML templates
├─ run.py                   # CLI entry – runs Flask app
├─ gunicorn.conf.py         # Gunicorn configuration
├─ hc-cdn-player.service   # Systemd unit for web service
├─ hc-cdn-worker.service   # Systemd unit for background worker
├─ requirements.txt         # Python dependencies
├─ schema.sql               # Initial DB schema (SQLite/Postgres)
└─ README.md                # High‑level project description
```

## Hosting & Deployment
1. **System Requirements**
   - Linux server (Ubuntu/Debian recommended) with `ffmpeg` and `ffprobe` available in `$PATH`.
   - Python 3.11+, virtual environment (`python -m venv venv`).
   - Systemd (default on most modern distros).
   - Disk space ≥ 2 GB free for temporary uploads.
2. **Installation Steps**
   ```bash
   # Clone repository
   git clone https://github.com/your-org/hc-cdn-player.git /opt/hc-cdn-player
   cd /opt/hc-cdn-player

   # Create dedicated user (optional but recommended)
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin hc-cdn

   # Set up virtualenv and install deps
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt

   # Create .env (see Environment Variables section)
   cp .env.example .env
   # edit .env with your values

   # Initialize DB
   flask db upgrade   # or run the app once to auto‑create tables
   ```
3. **Systemd Services**
   - Copy `hc-cdn-player.service` and `hc-cdn-worker.service` to `/etc/systemd/system/`.
   - Reload systemd and enable services:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable hc-cdn-player.service
   sudo systemctl enable hc-cdn-worker.service
   sudo systemctl start hc-cdn-player.service
   sudo systemctl start hc-cdn-worker.service
   ```
   - Logs can be inspected via `journalctl -u hc-cdn-player` and `journalctl -u hc-cdn-worker`.
4. **Port & Access**
   - The Gunicorn service binds to `0.0.0.0:${PORT}` (default `5000`). Adjust firewall/NAT as needed.
   - HTTPS termination is typically handled by a reverse proxy (NGINX, Caddy) that forwards to the Gunicorn socket.

## Environment Variables
| Variable | Description | Example |
|----------|-------------|---------|
| `PORT` | Port for Gunicorn to listen on | `5000` |
| `UPLOAD_FOLDER` | Temporary directory for incoming video files | `/var/www/uploads` |
| `CDN_ENCRYPTION_KEY` | Fernet key for encrypting CDN API secrets (32‑byte base64) | `b'...'` |
| `HACKCLUB_CDN_API_KEY` | API key for Hack Club CDN provider | `sk_live_…` |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | Optional Supabase auth configuration | `https://xyz.supabase.co` |
| `SQLALCHEMY_DATABASE_URI` | DB connection string | `sqlite:///app.db` or `postgresql://user:pass@host/db` |
| `FFMPEG_THREADS` | Number of threads FFmpeg may use | `40` |
| `FFMPEG_PRESET` | Encoding speed/quality trade‑off | `veryfast` |
| `FFMPEG_CRF` | Constant Rate Factor (quality) | `23` |
| `HLS_SEGMENT_DURATION` | Segment length in seconds | `6` |

## API Endpoints (selected)
- `POST /api/videos/upload` – upload a video file, creates a `Video` record and queues a processing job.
- `DELETE /api/videos/<video_id>` – queue a deletion job for the video and its CDN assets.
- `GET /api/cdn-accounts` – list configured CDN accounts.
- `POST /api/cdn-accounts` – add a new CDN account (encrypted key stored).
- `POST /api/cdn-accounts/<account_id>/test` – test connectivity to the CDN.
- `GET /api/jobs/<job_id>/logs` – retrieve background job log output.
- `GET /api/system/stats` – server resource usage and CDN storage stats.
- `GET/POST /api/settings` – read or update FFmpeg and processing settings.

## Background Worker Workflow
1. Worker thread polls `jobs` table for `status='queued'`.
2. For each job, it:
   - Retrieves video metadata via `ffprobe`.
   - Determines target resolutions (original + one step down).
   - Generates FFmpeg HLS command strings.
   - Executes FFmpeg, producing `.m3u8` playlist and `.ts` segments.
   - Extracts a thumbnail frame.
   - Uploads all assets to the selected CDN via `CDNProvider.upload_file`.
   - Persists `VideoVariant` and `VideoFile` records.
   - Updates job status to `completed` and logs the process.
3. Deletion jobs call `CDNProvider.delete_file` for each associated remote file, then remove DB rows.

## Security Considerations
- CDN API keys are encrypted at rest with Fernet; the secret key must be kept out of source control.
- JWTs are signed with `JWT_SECRET_KEY` (defaults to a random secret if not provided).
- Systemd services run under the non‑root `hc-cdn` user with limited filesystem permissions.
- The application checks free disk space before accepting uploads (fails if < 2 GB).

## Scaling & Extensibility
- **Horizontal scaling** can be achieved by running multiple Gunicorn workers behind a load balancer; the background worker can be run as a separate service on each node sharing the same DB.
- **Additional CDN providers** can be added by implementing `CDNProvider` in `app/cdn/` and registering it via `CDNManager`.
- **Job queue** currently uses a simple DB‑backed polling mechanism; for higher throughput, replace with Redis/RabbitMQ and a task queue library (Celery, RQ).

## Development Workflow
1. Run the app locally with hot‑reload:
   ```bash
   export FLASK_APP=run.py
   export FLASK_ENV=development
   flask run
   ```
2. Run the worker in a separate terminal:
   ```bash
   python -m app.worker.background
   ```
3. Run tests (if provided):
   ```bash
   pytest
   ```

---
*This document is intended for large language models (LLMs) to quickly gain a complete understanding of the HC CDN Player application, its deployment model, usage patterns, and internal architecture.*
