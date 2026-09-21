# Cheatsheet — daily ops for HC CDN Player (root on the server)

Short version of `controlpanel.md` for things you do repeatedly. All commands
assume the §0 variables are set:

```bash
APP_DIR=/opt/hc-cdn-player
APP_USER=hc-cdn
```

---

## Deploy after pushing to the repo

```bash
cd $APP_DIR
runuser -u $APP_USER -- git pull --ff-only
runuser -u $APP_USER -- ./venv/bin/pip install -r requirements.txt
systemctl restart hc-cdn-player.service
sleep 3
curl -sf --unix-socket /run/hc-cdn-player/gunicorn.sock http://localhost/ready
```

> Restart ends in-flight encodes (90 s grace, then SIGKILL). Queued jobs
> survive (DB-backed); `processing` jobs are re-queued automatically on boot.
> For zero interrupted encodes, deploy when the Jobs page shows no active job.

## Restart / reload / status

```bash
systemctl restart hc-cdn-player.service     # full restart (web + scheduler)
systemctl reload hc-cdn-player.service      # graceful gunicorn reload only
systemctl status hc-cdn-player --no-pager   # active (running)?
```

## Logs

```bash
journalctl -u hc-cdn-player -f                                   # follow
journalctl -u hc-cdn-player --since "1 hour ago" --no-pager | grep -i error
journalctl -u hc-cdn-player -n 50 --no-pager | grep -i -E "supervisor|recover|valid"
```

## Health (no login needed)

```bash
curl -sf --unix-socket /run/hc-cdn-player/gunicorn.sock http://localhost/up      # 200 = alive
curl -s  --unix-socket /run/hc-cdn-player/gunicorn.sock http://localhost/ready   # 200 = DB + scheduler ok
curl -s  --unix-socket /run/hc-cdn-player/gunicorn.sock http://localhost/health  # 200 healthy / 503 degraded (reasons in JSON)
```

## Authenticated API (purge etc. via curl)

```bash
# 1. Log in, save cookies:
curl -s -c /tmp/hc-cookies.txt -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"YOUR_PASSWORD"}' \
  https://fuddi.me/login > /dev/null
# 2. Use the cookie jar:
curl -s -b /tmp/hc-cookies.txt https://fuddi.me/api/settings
```

## Manually remove video metadata

**Option A — UI (easiest):** Dashboard → **Purge** button on any non-ready row.
Deletes the video record + its jobs/logs + leftover work dirs, and attempts to
delete every tracked CDN file first (half-uploaded orphans included). The result
toast reports exactly what left the CDN; files the current key doesn't own are
named so you can remove them from the Hack Club dashboard. `ready` rows refuse
purge — use **Delete** (removes CDN files too).

**Option B — API:**

```bash
curl -s -b /tmp/hc-cookies.txt -X DELETE https://fuddi.me/api/videos/<VIDEO_ID>/metadata
```

**Option C — direct DB surgery (SQLite default, service keeps running):**

```bash
DB=$(ls $APP_DIR/instance/app.db $APP_DIR/app.db 2>/dev/null | head -1)
cp "$DB" ~/app.db.$(date +%F-%H%M).bak   # always back up first

# List videos with stuck statuses:
sqlite3 "$DB" "SELECT id, title, status FROM videos;"

# Delete one video and everything attached (variants, files, jobs, logs):
sqlite3 "$DB" "PRAGMA foreign_keys=ON;
DELETE FROM videos WHERE id='<VIDEO_ID>';"

# Bulk-delete all failed/processing leftovers (keeps ready + delete_pending):
sqlite3 "$DB" "PRAGMA foreign_keys=ON;
DELETE FROM videos WHERE status IN ('failed','processing');"

# Mark jobs stuck in 'receiving' (dead uploads) as failed:
sqlite3 "$DB" "UPDATE jobs SET status='failed', stage='failed' WHERE status='receiving';"

# Reclaim space + verify:
sqlite3 "$DB" "VACUUM;"
du -h "$DB"
```

> `PRAGMA foreign_keys=ON` matters — without it, deleting a video orphans
> variant/file/job rows. The API purge endpoint handles this for you; raw SQL
> needs the pragma. Then remove orphaned work dirs: `du -sh /tmp/video-processing`
> and delete job-id folders with no matching job
> (`sqlite3 "$DB" "SELECT id FROM jobs;"` to compare).

**PostgreSQL variant:** replace `sqlite3 "$DB" "..."` with
`runuser -u postgres -- psql -d DBNAME -c "..."` (same SQL).

## CDN accounts with undecryptable keys

After a fresh `.env` (new `CDN_ENCRYPTION_KEY`), old accounts show
`API Key is required` and `/health` reports `degraded`. Fix in UI:
**CDN → Delete** stale accounts → **Add** with the real key → **Test**.
Or restore the old `CDN_ENCRYPTION_KEY` from your `.env` backup.

## Supabase mirror for CDN keys (cloud copy + local fallback)

Keys live in the local DB always; Supabase holds a cloud mirror of the
*encrypted* blobs. If Supabase is unreachable, the app serves the local copy
and says so on the CDN page. Plaintext keys never leave the server.

**One-time setup** — run in the Supabase SQL editor, then set
`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` in `.env` and restart:

```sql
create table if not exists cdn_accounts (
  id text primary key,
  name text not null,
  provider text not null default 'Hack Club CDN',
  encrypted_credentials text not null,
  enabled boolean not null default true,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
```

**Daily use** — on the CDN page: **Show** reveals one key (confirm first,
audit-logged server-side with your login email; Hide clears it),
**Export backup** downloads all accounts as an *encrypted* JSON file (save it
in your password manager/offline disk — it restores only together with the
`CDN_ENCRYPTION_KEY` from the same era), **Sync to Supabase** re-pushes
everything after an outage. There is deliberately no bulk plaintext export.

## Backup / restore

```bash
# Backup:
runuser -u $APP_USER -- cp $APP_DIR/instance/app.db ~/app.db.$(date +%F).bak 2>/dev/null || \
runuser -u $APP_USER -- cp $APP_DIR/app.db ~/app.db.$(date +%F).bak
cp $APP_DIR/.env ~/env.$(date +%F).bak && chmod 0600 ~/env.*.bak

# Restore DB (stop service first):
systemctl stop hc-cdn-player.service
cp ~/app.db.YYYY-MM-DD.bak $APP_DIR/instance/app.db
chown $APP_USER:www-data $APP_DIR/instance/app.db
systemctl start hc-cdn-player.service
```

## Disk pressure

```bash
df -h /tmp/video-processing
du -sh /tmp/video-processing
```

Uploads refuse below ~2 GB free (507 by design). The pipeline deletes local
files after CDN verification — leftover job folders mean crashed/interrupted
jobs; cross-check against the jobs table before deleting by hand.

## Full reset to zero

See `controlpanel.md` §1 (stop units, delete unit files, wipe
`/opt/hc-cdn-player`, logs, temp dirs, user, nginx site).

## Torrent ingestion

- Requires `aria2c` on the server (`apt install -y aria2`); without it the
  Torrents tab submits fail closed with "engine unavailable".
- Flow: Torrents tab → paste magnet / drop `.torrent` → metadata appears →
  tick files + pick CDN account → Download → progress → validated files hand
  into normal transcode jobs (linked from the torrent card).
- Limits live in Settings (concurrency, per-torrent MB, peers, bandwidth
  cap, wall-clock + metadata timeouts). Downloads run leech-only in a
  quarantined dir that is wiped on completion, cancel, failure, or timeout.
- Only pipeline-processable media passes validation (extension + magic
  bytes + metadata size match); anything else fails the torrent loudly.
