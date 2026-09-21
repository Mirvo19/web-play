"""Fail-loud startup validation.

Called from :func:`app.create_app` BEFORE the app serves traffic or starts
the job supervisor. Misconfiguration raises :class:`StartupError`
immediately instead of degrading silently at request time.

Rules (production = FLASK_ENV != development/test, TESTING off):
* SECRET_KEY / JWT_SECRET_KEY must be set, non-placeholder, >= 32 chars.
* CDN_ENCRYPTION_KEY must be set (needed to decrypt stored CDN keys).
* If the job scheduler is enabled, ffmpeg + ffprobe must be resolvable.
* DATABASE_URL must be a supported scheme and the DB must answer SELECT 1.
* Auth: real Supabase config required, unless ALLOW_INSECURE_DEV_AUTH is
  set (dev only — enabling it in production is itself a startup error).
"""

from __future__ import annotations

import logging
import os
import shutil

log = logging.getLogger(__name__)


class StartupError(RuntimeError):
    """Raised when the app must not boot with the current configuration."""


_PLACEHOLDER_SECRETS = frozenset({
    "",
    "default-dev-secret-key-change-in-prod",
    "default-jwt-secret-key-change-in-prod",
    "default-jwt-secret-key",
    "change-this-to-a-secure-random-secret-key",
    "change-this-to-a-secure-jwt-secret-key",
    "changeme",
    "secret",
})


def _is_production(app) -> bool:
    if app.config.get("TESTING"):
        return False
    return str(app.config.get("FLASK_ENV", "production")).lower() not in (
        "development", "dev", "test", "testing",
    )


def _check_secret(app, key: str, minimum: int = 32) -> None:
    value = app.config.get(key, "")
    if not value or value in _PLACEHOLDER_SECRETS:
        raise StartupError(
            f"{key} is missing or still a placeholder. Set a strong random "
            f"value in the environment and restart. "
            f"(Generate with: python -c \"import secrets; print(secrets.token_hex(32))\")"
        )
    if len(str(value)) < minimum:
        raise StartupError(f"{key} is too short (minimum {minimum} characters).")


def _check_auth(app, production: bool) -> None:
    url = (app.config.get("SUPABASE_URL", "") or "").strip().rstrip("/")
    key = (app.config.get("SUPABASE_ANON_KEY", "") or "").strip()
    placeholders = ("your-project-ref", "your-supabase", "example", "changeme", "placeholder")
    real = bool(url and key and not any(p in url.lower() for p in placeholders))
    dev_flag = bool(app.config.get("ALLOW_INSECURE_DEV_AUTH", False))
    if real:
        return
    if dev_flag and production:
        raise StartupError(
            "ALLOW_INSECURE_DEV_AUTH is enabled in a production environment. "
            "Disable it and configure SUPABASE_URL / SUPABASE_ANON_KEY."
        )
    if dev_flag:
        log.warning(
            "INSECURE DEV MODE: no Supabase auth configured; loopback-only "
            "dev login is active. Never expose this instance to a network."
        )
        return
    if production:
        raise StartupError(
            "No authentication configured: set SUPABASE_URL and "
            "SUPABASE_ANON_KEY (or explicitly opt into loopback-only dev "
            "mode with ALLOW_INSECURE_DEV_AUTH=true, development only)."
        )
    log.warning("No Supabase auth configured (non-production — login will refuse).")


def _check_binaries(app, scheduler_enabled: bool) -> None:
    ffmpeg = resolve_binary(app.config.get("FFMPEG_BINARY"))
    ffprobe = resolve_binary(app.config.get("FFPROBE_BINARY"))
    if scheduler_enabled and (not ffmpeg or not ffprobe):
        raise StartupError(
            f"Job scheduler is enabled but ffmpeg/ffprobe are missing "
            f"(ffmpeg={ffmpeg or 'not found'}, ffprobe={ffprobe or 'not found'}). "
            f"Install ffmpeg or set FFMPEG_BIN / FFPROBE_BIN."
        )
    if not ffmpeg or not ffprobe:
        log.warning(
            "ffmpeg/ffprobe not found (ffmpeg=%s, ffprobe=%s). "
            "Transcoding jobs will fail until installed.",
            ffmpeg or "missing",
            ffprobe or "missing",
        )


def resolve_binary(candidate) -> str | None:
    """Resolve a configured binary name/path to a usable executable, or None."""
    if not candidate:
        return None
    if os.path.isabs(str(candidate)):
        return str(candidate) if os.access(str(candidate), os.X_OK) else None
    return shutil.which(str(candidate))


def _check_database(app) -> None:
    from sqlalchemy import text

    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not uri or not str(uri).startswith(("sqlite:", "postgresql:", "postgres:")):
        raise StartupError(
            f"DATABASE_URL has an unsupported scheme: {str(uri)[:60]!r}. "
            "Use sqlite:///app.db or postgresql://..."
        )
    try:
        with app.app_context():
            from app.models import db

            with db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise StartupError(f"Database is not reachable ({uri[:60]!r}): {exc}")


def validate_config(app, scheduler_enabled: bool) -> None:
    """Run all startup checks; raise StartupError on any fatal problem."""
    production = _is_production(app)
    log.info("Startup validation (production=%s)...", production)

    if production:
        _check_secret(app, "SECRET_KEY")
        _check_secret(app, "JWT_SECRET_KEY")
        if not app.config.get("CDN_ENCRYPTION_KEY"):
            raise StartupError(
                "CDN_ENCRYPTION_KEY is not set. Stored CDN credentials cannot "
                "be decrypted without it. Generate one with: python -c "
                "\"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
    else:
        for key in ("SECRET_KEY", "JWT_SECRET_KEY", "CDN_ENCRYPTION_KEY"):
            if not app.config.get(key) or app.config.get(key) in _PLACEHOLDER_SECRETS:
                log.warning("%s is unset/placeholder (non-production — continuing).", key)

    _check_auth(app, production)
    _check_binaries(app, scheduler_enabled)
    _check_database(app)
    log.info("Startup validation passed.")
