"""Supabase mirror for CDN account credentials.

Local SQLite/Postgres is the always-working store: every mutation commits
locally first, then best-effort mirrors to Supabase. Reads try Supabase
first (5 s timeout) and fall back to the local copy when it is unreachable
or unconfigured — callers MUST surface which source served the data.

Setup (one time, in the Supabase SQL editor):

    create table if not exists cdn_accounts (
      id text primary key,
      name text not null,
      provider text not null default 'Hack Club CDN',
      encrypted_credentials text not null,
      enabled boolean not null default true,
      created_at timestamptz default now(),
      updated_at timestamptz default now()
    );

Requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY. Only the Fernet-encrypted
blob is ever sent — plaintext keys never leave this process.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from flask import current_app

_log = logging.getLogger(__name__)

TABLE = "cdn_accounts"
TIMEOUT = 5  # seconds — fallback must be fast, not a hang


def configured() -> bool:
    """True when a real Supabase URL + service-role key are set.

    Only the service-role key is needed (server-side REST bypasses RLS);
    the anon key used for user login is irrelevant here.
    """
    url = (current_app.config.get("SUPABASE_URL", "") or "").strip().rstrip("/")
    if not url:
        return False
    placeholders = ("your-project-ref", "your-supabase", "example", "changeme", "placeholder")
    if any(p in url.lower() for p in placeholders):
        return False
    key = (current_app.config.get("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()
    return bool(key)


def _request(method: str, path: str, **kwargs):
    """Low-level REST call. Returns (ok, payload|message); never raises."""
    base = (current_app.config.get("SUPABASE_URL", "") or "").rstrip("/")
    service_key = (current_app.config.get("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation,resolution=merge-duplicates",
    }
    try:
        resp = requests.request(method, f"{base}/rest/v1/{path}",
                                headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        return False, f"Supabase unreachable: {exc}"
    if resp.status_code in (200, 201, 204):
        if resp.status_code == 204 or not resp.content:
            return True, None
        try:
            return True, resp.json()
        except ValueError:
            return False, "Supabase returned invalid JSON"
    return False, f"Supabase error HTTP {resp.status_code}: {resp.text[:200]}"


def to_remote_row(account) -> dict:
    """Serialize a local CDNAccount to a Supabase row (encrypted blob only)."""
    row = {
        "id": account.id,
        "name": account.name,
        "provider": account.provider,
        "encrypted_credentials": account.encrypted_credentials or "",
        "enabled": bool(account.enabled),
    }
    if getattr(account, "created_at", None):
        row["created_at"] = account.created_at.isoformat()
    if getattr(account, "updated_at", None):
        row["updated_at"] = account.updated_at.isoformat()
    return row


def push_account(account) -> tuple[bool, str]:
    """Upsert one account to Supabase. Returns (ok, message)."""
    if not configured():
        return False, "Supabase mirror not configured"
    ok, result = _request("POST", TABLE, json=to_remote_row(account))
    if ok:
        return True, "mirrored"
    _log.warning("Supabase mirror push failed for account %s: %s", account.id, result)
    return False, str(result)


def delete_remote(account_id: str) -> tuple[bool, str]:
    """Delete one mirrored account. Returns (ok, message)."""
    if not configured():
        return False, "Supabase mirror not configured"
    ok, result = _request("DELETE", f"{TABLE}?id=eq.{account_id}")
    if ok:
        return True, "mirror deleted"
    _log.warning("Supabase mirror delete failed for %s: %s", account_id, result)
    return False, str(result)


def fetch_remote() -> tuple[bool, Any]:
    """Fetch all mirrored accounts. Returns (True, [rows]) or (False, reason).

    Any failure (not configured, unreachable, HTTP error) returns False —
    callers treat that as "serve the local copy".
    """
    if not configured():
        return False, "Supabase mirror not configured"
    ok, result = _request("GET", f"{TABLE}?select=*")
    if ok:
        rows = result if isinstance(result, list) else []
        return True, rows
    return False, str(result)


def sync_local_to_remote(accounts) -> dict:
    """Push every local account; returns {pushed, failed, errors}."""
    stats = {"pushed": 0, "failed": 0, "errors": []}
    for acc in accounts:
        ok, msg = push_account(acc)
        if ok:
            stats["pushed"] += 1
        else:
            stats["failed"] += 1
            stats["errors"].append(f"{acc.id}: {msg}")
    return stats


def remote_ids(rows) -> set:
    """Extract account ids from fetched Supabase rows (defensive)."""
    ids = set()
    for r in rows or []:
        if isinstance(r, dict) and r.get("id"):
            ids.add(r["id"])
    return ids
