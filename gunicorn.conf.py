# =============================================================================
# Gunicorn Production Configuration — HC CDN Player
# =============================================================================
# Tuned for a 2 GB RAM server with 48 cores / 92 threads.
#
# Philosophy:
#   - 2 worker processes: each idle Flask worker consumes ~80-120 MB RAM.
#     With 2 workers we have ~1.6 GB left for the OS, FFmpeg (run by the
#     *separate* worker process), and file caching.
#   - 4 threads per worker: handles bursts of concurrent HTTP requests
#     without spawning extra processes. SQLAlchemy sessions are thread-safe
#     per-request with scoped_session (Flask-SQLAlchemy default).
#   - preload_app = True: the application module is loaded once in the
#     master process, then forked into workers. Saves RAM and startup time.
#     SAFE here because the background worker is no longer started inside
#     create_app() — there is no thread to accidentally fork.
#   - Unix socket: lower latency than TCP loopback for Nginx proxy.
#   - timeout = 300: large file uploads (PUT streaming) may take minutes.
#     Nginx also needs proxy_read_timeout to match.
# =============================================================================

import multiprocessing

# --- Worker process count ---
# 2 workers × ~120 MB = ~240 MB for web processes.
# Leaves ~1.76 GB for OS + worker process + FFmpeg (max ~1.5 GB budget).
workers = 2

# --- Worker model ---
# 'sync' + threads is the right choice for I/O-bound Flask apps on CPython.
# gevent/eventlet are alternatives but require greenlet-aware SQLAlchemy.
worker_class = "sync"
threads = 4

# --- Binding ---
# Unix socket preferred over TCP for Nginx ↔ Gunicorn on the same host.
bind = "unix:/run/hc-cdn-player/gunicorn.sock"

# --- Timeouts ---
# 300 s = 5 min: needed for large streaming uploads.
# Nginx proxy_read_timeout must be >= this value.
timeout = 300
keepalive = 5
graceful_timeout = 30

# --- Memory optimisation ---
# Restart each worker after handling this many requests to reclaim memory.
# Helps prevent long-running workers from slowly leaking RAM.
max_requests = 500
max_requests_jitter = 50

# --- App preloading ---
# Load the Flask app once in master, fork to workers.
# DO NOT enable this if create_app() starts background threads — but we
# have removed that so it is safe.
preload_app = True

# --- Logging ---
accesslog = "/var/log/hc-cdn-player/access.log"
errorlog = "/var/log/hc-cdn-player/error.log"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# --- Process title ---
proc_name = "hc-cdn-player"
