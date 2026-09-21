# Control Panel — HC CDN Player server setup (fresh container)

Runbook for a clean Ubuntu/Debian container/VPS. You are **root**, so no
`sudo` anywhere — commands run as root directly, app commands as the
`hc-cdn` service user via `runuser`.

> Paths match this repo: app at `/opt/hc-cdn-player`,
> socket `/run/hc-cdn-player/gunicorn.sock`, logs `/var/log/hc-cdn-player`,
> uploads `/tmp/video-processing`.

---

## 0. Variables (paste once at the top of your session)

```bash
APP_DIR=/opt/hc-cdn-player
APP_USER=hc-cdn
REPO_URL=https://github.com/Mirvo19/web-play.git
BRANCH=main
```

- `APP_DIR` — where the app lives (matches `WorkingDirectory=` in
  `hc-cdn-player.service` and `WEB_CONCURRENCY`/gunicorn config).
- `APP_USER` — unprivileged system user the service runs as (no login, no
  home). Never run the app as root.
- `REPO_URL` — this project's real repo (remote `origin` is
  `Mirvo19/web-play`). HTTPS form so the server needs no SSH deploy key.
- `BRANCH` — `main` (current branch of this repo).

---

## 1. COMPLETE RESET — wipe everything and start over

Destroys all app state on this machine. **Back up first if you need anything:**

```bash
# Optional backup (DB + secrets) before wiping:
cp /opt/hc-cdn-player/instance/app.db ~/app.db.$(date +%F).bak 2>/dev/null || \
cp /opt/hc-cdn-player/app.db ~/app.db.$(date +%F).bak 2>/dev/null || echo "no db file found"
cp /opt/hc-cdn-player/.env ~/env.$(date +%F).bak 2>/dev/null && chmod 0600 ~/env.*.bak || echo "no .env found"
```

Full reset:

```bash
# 1. Stop + disable BOTH units (old worker unit may still exist)
systemctl stop hc-cdn-worker.service hc-cdn-player.service 2>/dev/null
systemctl disable hc-cdn-worker.service hc-cdn-player.service 2>/dev/null

# 2. Delete unit files (the worker unit is obsolete — single-process build)
rm -f /etc/systemd/system/hc-cdn-worker.service /etc/systemd/system/hc-cdn-player.service
systemctl daemon-reload
systemctl reset-failed

# 3. Kill anything left behind, wipe all app data
pkill -f gunicorn; pkill -f "run:app"; sleep 2
rm -rf /opt/hc-cdn-player /var/log/hc-cdn-player /tmp/video-processing
rm -rf /run/hc-cdn-player

# 4. Remove the service user (recreated fresh in §3)
userdel $APP_USER 2>/dev/null; groupdel $APP_USER 2>/dev/null; echo "user removed"

# 5. Optional: remove the nginx site for a from-scratch web setup
rm -f /etc/nginx/sites-enabled/hc-cdn-player /etc/nginx/sites-available/hc-cdn-player
systemctl reload nginx 2>/dev/null

# 6. Verify the slate is clean (all should print nothing / fail gracefully)
systemctl status hc-cdn-player hc-cdn-worker --no-pager 2>&1 | head -5
ls /opt/hc-cdn-player 2>&1; id hc-cdn 2>&1
```

---

## 2. OS preparation

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip ffmpeg curl git nginx ufw

# Sanity checks
python3 --version        # need 3.11+
ffmpeg -version | head -1
ffprobe -version | head -1
```

---

## 3. Service user + directories

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin $APP_USER
mkdir -p $APP_DIR /var/log/hc-cdn-player /tmp/video-processing
chown -R $APP_USER:www-data $APP_DIR /var/log/hc-cdn-player
chmod 0750 $APP_DIR
```

---

## 4. Deploy the code

```bash
runuser -u $APP_USER -- git clone --branch $BRANCH $REPO_URL $APP_DIR
cd $APP_DIR && runuser -u $APP_USER -- git log --oneline -1   # confirm correct code
```

---

## 5. Virtualenv + dependencies

```bash
runuser -u $APP_USER -- python3 -m venv $APP_DIR/venv
runuser -u $APP_USER -- $APP_DIR/venv/bin/pip install --upgrade pip
runuser -u $APP_USER -- $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt
```

---

## 6. Environment file (`$APP_DIR/.env`)

```bash
runuser -u $APP_USER -- cp $APP_DIR/.env.example $APP_DIR/.env
nano $APP_DIR/.env
```

Fill in — **production boot refuses placeholders**, so generate real values:

```bash
# Generate secrets (paste into .env):
python3 -c "import secrets; print(secrets.token_hex(32))"   # SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"   # JWT_SECRET_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # CDN_ENCRYPTION_KEY
```

Required in `.env`:

| Variable | Value |
|---|---|
| `SECRET_KEY` / `JWT_SECRET_KEY` | the two generated hex strings (≥32 chars) |
| `CDN_ENCRYPTION_KEY` | the generated Fernet key |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | your Supabase project (auth will not work without these) |
| `DATABASE_URL` | `sqlite:///app.db` (default) or `postgresql://user:pass@localhost/db` |
| `UPLOAD_FOLDER` | `/tmp/video-processing` |
| `ENABLE_JOB_SCHEDULER` | `true` (single-process job runner) |
| `ALLOW_INSECURE_DEV_AUTH` | `false` — **never `true` on a server** |
| `WEB_CONCURRENCY` | `1` (one scheduler/ffmpeg pool) |

```bash
chmod 0600 $APP_DIR/.env
```

---

## 7. Validate config + initialize database (before starting systemd)

```bash
# Loud config check: exits non-zero with a clear error if anything is wrong
# (bad secrets, missing ffmpeg, unreachable DB, no auth configured)
runuser -u $APP_USER -- $APP_DIR/venv/bin/python -c \
  "from app import create_app; create_app(); print('CONFIG OK')"

# Create tables (SQLite file is created on first boot automatically)
cd $APP_DIR && runuser -u $APP_USER -- env $(grep -v '^#' $APP_DIR/.env | xargs) \
  $APP_DIR/venv/bin/python -c "from app import create_app; create_app(); print('DB OK')"
```

> If you use PostgreSQL: `apt install -y postgresql`, create DB/user,
> set `DATABASE_URL=postgresql://…`, and add `psycopg2-binary` to requirements.

---

## 8. Install + start the single systemd unit

```bash
cp $APP_DIR/hc-cdn-player.service /etc/systemd/system/hc-cdn-player.service
systemctl daemon-reload
systemctl enable hc-cdn-player.service
systemctl start hc-cdn-player.service
sleep 3
systemctl status hc-cdn-player.service --no-pager
```

Expect `active (running)`. If not: `journalctl -u hc-cdn-player -n 100 --no-pager`.

---

## 9. Nginx reverse proxy + firewall (+ TLS)

`/etc/nginx/sites-available/hc-cdn-player` (replace `videos.example.com`):

```nginx
upstream hc_cdn_player {
    server unix:/run/hc-cdn-player/gunicorn.sock fail_timeout=0;
}

server {
    listen 80;
    server_name videos.example.com;

    client_max_body_size 5G;          # must exceed MAX_UPLOAD_SIZE_GB
    proxy_read_timeout 300s;          # matches gunicorn timeout
    proxy_send_timeout 300s;

    location / {
        proxy_pass http://hc_cdn_player;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
ln -sf /etc/nginx/sites-available/hc-cdn-player /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
ufw status

# TLS (recommended): apt install -y certbot python3-certbot-nginx
# certbot --nginx -d videos.example.com
```

---

## 10. Verify the installation end-to-end

```bash
# Health probes (no login needed)
curl -sf --unix-socket /run/hc-cdn-player/gunicorn.sock http://localhost/up
curl -s --unix-socket /run/hc-cdn-player/gunicorn.sock http://localhost/ready; echo
curl -s --unix-socket /run/hc-cdn-player/gunicorn.sock http://localhost/health; echo
# /health → 200 healthy. 503 degraded is HONEST: check which sub-check failed
# (common: demo CDN key rejected, or ffmpeg missing — fix, don't ignore).

# Public check (after nginx/TLS)
curl -sf https://videos.example.com/up

# Scheduler is actually polling (crash-recovery lines appear once at boot)
journalctl -u hc-cdn-player -n 50 --no-pager | grep -i -E "supervisor|recover|valid"

# Log in via browser → https://videos.example.com/login → upload a video →
# watch it appear under Videos, job runs to "completed" on the Jobs page.
```

---

## 11. Day-to-day operations cheatsheet

```bash
# Logs (all app logs go to stderr → journald)
journalctl -u hc-cdn-player -f
journalctl -u hc-cdn-player --since "1 hour ago" --no-pager | grep -i error

# Restart / reload
systemctl restart hc-cdn-player.service
systemctl reload hc-cdn-player.service     # graceful gunicorn reload

# Update to a new version (restart ends in-flight encodes after the 90 s stop timeout)
cd $APP_DIR
runuser -u $APP_USER -- git pull --ff-only
runuser -u $APP_USER -- ./venv/bin/pip install -r requirements.txt
systemctl restart hc-cdn-player.service
curl -sf --unix-socket /run/hc-cdn-player/gunicorn.sock http://localhost/ready

# Backup (SQLite default: the DB file; also back up .env somewhere safe)
runuser -u $APP_USER -- cp $APP_DIR/instance/app.db ~/app.db.$(date +%F).bak 2>/dev/null || \
runuser -u $APP_USER -- cp $APP_DIR/app.db ~/app.db.$(date +%F).bak
cp $APP_DIR/.env ~/env.$(date +%F).bak && chmod 0600 ~/env.*.bak

# Disk pressure (uploads refuse below ~2 GB free by design)
df -h /tmp/video-processing
du -sh /tmp/video-processing
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Unit fails at boot with `StartupError: SECRET_KEY…` | `.env` still has placeholder secrets — generate real ones (§6) |
| `StartupError: No authentication configured` | Set `SUPABASE_URL` + `SUPABASE_ANON_KEY` (never enable dev auth on a server) |
| `StartupError: ffmpeg/ffprobe are missing` | `apt install -y ffmpeg`, or set `FFMPEG_BIN`/`FFPROBE_BIN` |
| `/health` → 503, `cdn` check fails | CDN key rejected/unreachable — Test the account in the UI; storage numbers shown are then honestly flagged, not faked |
| Uploads rejected with 507 | Disk full — free space or grow the volume (2 GB headroom required) |
| `502 Bad Gateway` from nginx | Gunicorn down — `systemctl status`, `journalctl`; check socket perms (`hc-cdn:www-data`, nginx runs as `www-data`) |
| Old `hc-cdn-worker` errors after upgrade | You still have the deleted unit installed — repeat §1 to remove it |
