import logging
import os
import random
import requests
import time
from typing import Dict, Any, Tuple
from urllib.parse import quote, urlparse
from app.cdn.base import CDNProvider

_logger = logging.getLogger(__name__)

# Retry policy for single-file uploads (transient network/TLS blips must not
# fail a whole 17-file job). Tunable without a code change:
#   CDN_UPLOAD_RETRIES=4 CDN_UPLOAD_BACKOFF_SEC=1.5
_UPLOAD_RETRIES = int(os.environ.get("CDN_UPLOAD_RETRIES", "4"))
_UPLOAD_BACKOFF_SEC = float(os.environ.get("CDN_UPLOAD_BACKOFF_SEC", "1.5"))

class HackClubCDNProvider(CDNProvider):
    """
    Hack Club CDN Provider Implementation (v4 API, per openapi.json 4.0.0).
    Documented endpoints:
      - Upload:        POST   https://cdn.hackclub.com/api/v4/upload
      - Delete one:    DELETE https://cdn.hackclub.com/api/v4/upload/{id}
      - Delete batch:  DELETE https://cdn.hackclub.com/api/v4/uploads/batch  {"ids": [...]}
      - Who am I/quota: GET   https://cdn.hackclub.com/api/v4/me
    Auth: Authorization: Bearer sk_cdn_...
    NOTE: uploads belong to the key owner — a different (even valid) key
    gets 404 "no matching resource belongs to the API key's owner" on
    delete. That is an ownership fact, not a wrong URL; don't guess routes.
    """

    BASE_URL = "https://cdn.hackclub.com"
    MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB per file limit

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def test_connection(self) -> Tuple[bool, str]:
        """Validate the key against the documented GET /api/v4/me endpoint.

        Every outcome reflects the actual HTTP response — no heuristics.
        """
        if not self.api_key or not self.api_key.strip():
            return False, "API Key is required"
        try:
            resp = requests.get(f"{self.BASE_URL}/api/v4/me",
                                headers=self._headers(), timeout=10)
        except requests.RequestException as e:
            return False, f"Connection error: {str(e)}"
        if resp.status_code == 200:
            try:
                owner = (resp.json() or {}).get("email", "")
            except ValueError:
                owner = ""
            return True, f"Connection successful{(' as ' + owner) if owner else ''}"
        if resp.status_code in (401, 403):
            return False, "Invalid API Key (unauthorized by CDN API)"
        return False, f"CDN API rejected the check (HTTP {resp.status_code}): {resp.text[:200]}"

    def upload_file(self, local_file_path: str, remote_filename: str = None) -> Dict[str, Any]:
        if not os.path.exists(local_file_path):
            raise FileNotFoundError(f"Local file not found: {local_file_path}")

        file_size = os.path.getsize(local_file_path)
        if file_size > self.MAX_FILE_SIZE:
            raise ValueError(f"File size ({file_size} bytes) exceeds Hack Club CDN 100 MB limit!")

        filename = remote_filename or os.path.basename(local_file_path)
        headers = {"Authorization": f"Bearer {self.api_key}"}

        upload_url = f"{self.BASE_URL}/api/v4/upload"

        # Retry transient failures (dropped connections, TLS EOF, timeouts,
        # 429/5xx) with exponential backoff + jitter. Permanent failures
        # (other 4xx, malformed success bodies) raise immediately.
        # Note: requests' SSLError subclasses ConnectionError, so it is
        # covered by the transient branch below.
        last_error = f"exhausted {_UPLOAD_RETRIES} attempts"
        for attempt in range(1, max(1, _UPLOAD_RETRIES) + 1):
            try:
                with open(local_file_path, "rb") as f:
                    files = {"file": (filename, f)}
                    response = requests.post(upload_url, headers=headers, files=files, timeout=120)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_error = f"network error: {e}"
                transient = True
            else:
                if response.status_code in (200, 201):
                    return self._parse_upload_response(response, file_size)
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    transient = True
                else:
                    raise RuntimeError(f"CDN upload failed ({response.status_code}): {response.text}")

            if attempt < max(1, _UPLOAD_RETRIES):
                delay = _UPLOAD_BACKOFF_SEC * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                _logger.warning("CDN upload %s: attempt %d failed (%s); retrying in %.1fs",
                                filename, attempt, last_error, delay)
                time.sleep(delay)

        raise RuntimeError(f"CDN upload failed for {filename!r}: {last_error}")

    @staticmethod
    def _parse_upload_response(response, file_size: int) -> Dict[str, Any]:
        data = response.json()
        cdn_url = data.get("url") or data.get("file_url") or data.get("link")
        if not cdn_url and "id" in data:
            cdn_url = f"{HackClubCDNProvider.BASE_URL}/{data['id']}"

        if not cdn_url:
            raise RuntimeError(f"Upload succeeded but no URL in CDN response: {response.text}")

        remote_path = data.get("id") or data.get("key")
        if not remote_path:
            remote_path = urlparse(cdn_url).path.lstrip('/')

        return {
            "url": cdn_url,
            "remote_path": remote_path,
            "file_size": file_size
        }

    @staticmethod
    def extract_upload_id(remote_path_or_url: str) -> str:
        """Extract the upload ID from a stored remote identifier or public URL.

        Stored identifiers are the raw ``id`` from the upload response;
        public URLs look like ``https://cdn.hackclub.com/<id>/<filename>``.
        Returns "" when no plausible ID can be determined.
        """
        if not remote_path_or_url:
            return ""
        text = remote_path_or_url.strip()
        if text.startswith("http"):
            try:
                segments = [s for s in urlparse(text).path.split("/") if s]
                return segments[0] if segments else ""
            except Exception:
                return ""
        # Raw identifier — may itself be a path; the ID is the first segment.
        segments = [s for s in text.strip("/").split("/") if s]
        return segments[0] if segments else ""

    def delete_file(self, remote_path_or_url: str):
        """Delete one upload via the documented DELETE /api/v4/upload/{id}.

        Returns (True, attempts) only on an explicit ``{"deleted": true}``.
        A 404 means the file is already gone OR belongs to a different key
        owner — either way this key did not delete it, so it is False with
        the reason preserved (callers must not mark it deleted silently).
        """
        upload_id = self.extract_upload_id(remote_path_or_url)
        if not upload_id:
            return False, [{"endpoint": "n/a",
                            "error": "could not determine upload ID from stored identifier"}]

        endpoint = f"{self.BASE_URL}/api/v4/upload/{quote(upload_id, safe='')}"
        try:
            resp = requests.delete(endpoint, headers=self._headers(), timeout=15)
        except requests.RequestException as e:
            _logger.debug("CDN DELETE %s raised %s", endpoint, e)
            return False, [{"endpoint": endpoint, "error": str(e)}]

        tried = [{"endpoint": endpoint, "status": resp.status_code,
                  "body": (resp.text or "")[:800]}]
        if resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError:
                return False, tried
            if isinstance(body, dict) and body.get("deleted"):
                return True, tried
            return False, tried
        return False, tried

    def delete_files_batch(self, remote_paths_or_urls) -> Dict[str, Any]:
        """Delete several uploads in one call (DELETE /api/v4/uploads/batch).

        Returns {"deleted": [...], "not_found": [...]}. IDs the key does not
        own come back in ``not_found`` rather than failing the request.
        """
        ids = [i for i in (self.extract_upload_id(x) for x in remote_paths_or_urls or []) if i]
        if not ids:
            return {"deleted": [], "not_found": []}
        try:
            resp = requests.delete(f"{self.BASE_URL}/api/v4/uploads/batch",
                                   headers=self._headers(), json={"ids": ids}, timeout=60)
        except requests.RequestException as e:
            raise RuntimeError(f"CDN batch delete failed: {e}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"CDN batch delete failed (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(f"CDN batch delete returned invalid JSON: {e}")
        return {"deleted": data.get("deleted", []),
                "not_found": data.get("not_found", [])}

    def get_storage_info(self) -> Dict[str, int]:
        """Query live quota from the documented GET /api/v4/me endpoint.

        Returns the account's REAL storage_used/storage_limit (tier-aware),
        replacing the old hard-coded 50 GB assumption.

        Raises:
            RuntimeError: when the API cannot be reached or does not return
                usable data. Callers must surface this failure.
        """
        try:
            resp = requests.get(f"{self.BASE_URL}/api/v4/me",
                                headers=self._headers(), timeout=10)
        except requests.RequestException as e:
            raise RuntimeError(f"CDN storage lookup failed: {str(e)}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"CDN storage lookup failed (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(f"CDN storage lookup returned invalid JSON: {e}")
        try:
            used = max(0, int(data.get("storage_used", 0)))
            total = max(1, int(data.get("storage_limit", 0)))
        except (TypeError, ValueError):
            raise RuntimeError("CDN storage lookup returned invalid quota values")
        return {
            "used_bytes": used,
            "available_bytes": max(0, total - used),
            "total_bytes": total,
        }
