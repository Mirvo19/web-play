import os
import requests
from typing import Dict, Any, Tuple
from urllib.parse import quote, urlparse
from app.cdn.base import CDNProvider

class HackClubCDNProvider(CDNProvider):
    """
    Hack Club CDN Provider Implementation (v4 API).
    Endpoints:
      - Upload: POST https://cdn.hackclub.com/api/v4/upload
      - Headers: Authorization: Bearer <API_KEY>
      - Body: multipart/form-data with 'file' field
    """

    BASE_URL = "https://cdn.hackclub.com"
    MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB per file limit
    TOTAL_STORAGE_LIMIT = 50 * 1024 * 1024 * 1024  # 50 GB per account

    def test_connection(self) -> Tuple[bool, str]:
        """Probe the CDN API with the configured key.

        Every outcome reflects the actual HTTP response — there is no
        key-length heuristic fallback. A key that the API rejects (or that
        cannot be verified because the API is unreachable) reports failure.
        """
        if not self.api_key or not self.api_key.strip():
            return False, "API Key is required"
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            resp = requests.get(f"{self.BASE_URL}/api/v4/user", headers=headers, timeout=10)
            if resp.status_code in (200, 201):
                return True, "Connection successful"

            resp_stats = requests.get(f"{self.BASE_URL}/api/v4/stats", headers=headers, timeout=10)
            if resp_stats.status_code in (200, 201):
                return True, "Connection successful"

            if resp.status_code in (401, 403) or resp_stats.status_code in (401, 403):
                return False, "Invalid API Key (unauthorized by CDN API)"
            return False, (
                f"CDN API did not accept the key "
                f"(user={resp.status_code}, stats={resp_stats.status_code})"
            )
        except requests.RequestException as e:
            return False, f"Connection error: {str(e)}"

    def upload_file(self, local_file_path: str, remote_filename: str = None) -> Dict[str, Any]:
        if not os.path.exists(local_file_path):
            raise FileNotFoundError(f"Local file not found: {local_file_path}")

        file_size = os.path.getsize(local_file_path)
        if file_size > self.MAX_FILE_SIZE:
            raise ValueError(f"File size ({file_size} bytes) exceeds Hack Club CDN 100 MB limit!")

        filename = remote_filename or os.path.basename(local_file_path)
        headers = {"Authorization": f"Bearer {self.api_key}"}

        upload_url = f"{self.BASE_URL}/api/v4/upload"

        with open(local_file_path, "rb") as f:
            files = {"file": (filename, f)}
            response = requests.post(upload_url, headers=headers, files=files, timeout=120)

        if response.status_code in (200, 201):
            data = response.json()
            cdn_url = data.get("url") or data.get("file_url") or data.get("link")
            if not cdn_url and "id" in data:
                cdn_url = f"{self.BASE_URL}/{data['id']}"
            
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
        else:
            raise RuntimeError(f"CDN upload failed ({response.status_code}): {response.text}")

    def delete_file(self, remote_path_or_url: str):
        if not remote_path_or_url:
            return True, []

        headers = {"Authorization": f"Bearer {self.api_key}"}
        
        # Extract file path from the stored remote identifier or public URL.
        if remote_path_or_url.startswith('http'):
            parsed = urlparse(remote_path_or_url)
            remote_path = parsed.path.lstrip('/')
        else:
            remote_path = remote_path_or_url.lstrip('/')

        basename = os.path.basename(remote_path) or remote_path
        file_id = basename

        # Try both the full remote path and basename candidates.
        file_id_candidates = [basename]
        if remote_path != basename:
            file_id_candidates.insert(0, remote_path)
            first_segment = remote_path.split('/', 1)[0]
            if first_segment and first_segment not in file_id_candidates:
                file_id_candidates.append(first_segment)

        tried = []

        # If the caller provided a full URL, try deleting that exact URL first.
        if remote_path_or_url.startswith('http'):
            try:
                resp = requests.delete(remote_path_or_url, headers=headers, timeout=15)
                entry = {'endpoint': remote_path_or_url, 'status': resp.status_code, 'body': resp.text, 'headers': dict(resp.headers)}
                tried.append(entry)
                ct = resp.headers.get('content-type', '')
                if resp.status_code == 204:
                    return True, tried
                if resp.status_code == 404:
                    # Public URLs often return 404 for missing files; treat as deleted.
                    return True, tried
                if resp.status_code == 200 and 'application/json' in ct:
                    try:
                        j = resp.json()
                        if isinstance(j, dict) and j.get('deleted'):
                            return True, tried
                    except Exception:
                        pass
            except Exception as e:
                tried.append({'endpoint': remote_path_or_url, 'error': str(e)})

        # Try API deletion endpoints that accept an ID/key (include documented /api/v4/upload/:id)
        delete_endpoints = []
        for candidate in file_id_candidates:
            encoded_candidate = quote(candidate, safe='')
            delete_endpoints.extend([
                f"{self.BASE_URL}/api/v4/upload/{encoded_candidate}",
                f"{self.BASE_URL}/api/v4/files/{encoded_candidate}",
                f"{self.BASE_URL}/api/v4/delete/{encoded_candidate}",
                f"{self.BASE_URL}/api/v4/{encoded_candidate}",
            ])

        for endpoint in delete_endpoints:
            try:
                tried.append(endpoint)
                resp = requests.delete(endpoint, headers=headers, timeout=15)
                # Optionally log response details for debugging
                try:
                    from flask import current_app
                    if current_app.config.get('LOG_TO_STDOUT', False):
                        try:
                            print(f"[CDN DELETE] Tried {endpoint} -> {resp.status_code} | {resp.text}")
                        except Exception:
                            print(f"[CDN DELETE] Tried {endpoint} -> {resp.status_code}")
                except Exception:
                    pass

                # Treat 200/204/404 as success (404 = already removed)
                tried[-1] = {'endpoint': endpoint, 'status': resp.status_code, 'body': resp.text, 'headers': dict(resp.headers)}
                ct = resp.headers.get('content-type', '')
                # Accept only explicit confirmations
                if resp.status_code == 204:
                    return True, tried
                if resp.status_code == 200 and 'application/json' in ct:
                    try:
                        j = resp.json()
                        if isinstance(j, dict) and j.get('deleted'):
                            return True, tried
                        else:
                            continue
                    except Exception:
                        continue
                # do not treat 404 or HTML 200 as success
            except Exception as e:
                try:
                    from flask import current_app
                    if current_app.config.get('LOG_TO_STDOUT', False):
                        print(f"[CDN DELETE] Exception trying {endpoint}: {e}")
                except Exception:
                    pass
                continue

        # Try POST-based deletion endpoint (some providers expect POST)
        try:
            post_endpoint = f"{self.BASE_URL}/api/v4/delete"
            tried.append(post_endpoint)
            resp = requests.post(post_endpoint, headers=headers, json={"id": file_id}, timeout=15)
            tried[-1] = {'endpoint': post_endpoint, 'status': resp.status_code, 'body': resp.text, 'headers': dict(resp.headers)}
            ct = resp.headers.get('content-type', '')
            if resp.status_code == 204:
                return True, tried
            if resp.status_code == 200 and 'application/json' in ct:
                try:
                    j = resp.json()
                    if isinstance(j, dict) and j.get('deleted'):
                        return True, tried
                except Exception:
                    pass
        except Exception as e:
            tried.append({'endpoint': post_endpoint, 'error': str(e)})
            pass

        # As a last resort, try deleting by the basename-only under the API root
        try:
            fallback = f"{self.BASE_URL}/{file_id}"
            tried.append(fallback)
            resp = requests.delete(fallback, headers=headers, timeout=15)
            tried[-1] = {'endpoint': fallback, 'status': resp.status_code, 'body': resp.text, 'headers': dict(resp.headers)}
            ct = resp.headers.get('content-type', '')
            if resp.status_code == 204:
                return True, tried
            if resp.status_code == 200 and 'application/json' in ct:
                try:
                    j = resp.json()
                    if isinstance(j, dict) and j.get('deleted'):
                        return True, tried
                except Exception:
                    pass
        except Exception as e:
            tried.append({'endpoint': fallback, 'error': str(e)})
            pass

        # If none of the attempts returned a definitive success, return False with attempt details
        return False, tried

    def get_storage_info(self) -> Dict[str, int]:
        """Query live storage usage from the CDN API.

        Raises:
            RuntimeError: when the API cannot be reached or does not return
                usable data. Callers must surface this failure — returning
                fake "0 used / 50 GB free" here previously hid outages and
                let operators believe capacity existed that did not.
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = requests.get(f"{self.BASE_URL}/api/v4/stats", headers=headers, timeout=10)
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
            used = int(data.get("used_bytes", 0))
        except (TypeError, ValueError):
            raise RuntimeError("CDN storage lookup returned an invalid used_bytes value")
        total = self.TOTAL_STORAGE_LIMIT
        return {
            "used_bytes": max(0, used),
            "available_bytes": max(0, total - used),
            "total_bytes": total,
        }
