import base64
import hashlib
import logging

from cryptography.fernet import Fernet
from flask import current_app

log = logging.getLogger(__name__)


def _get_fernet_instance(raw_secret: str = None) -> Fernet:
    """Build a Fernet instance from the configured CDN encryption key.

    Raises:
        RuntimeError: if no key is configured. There is deliberately NO
            hard-coded fallback key — a static fallback would let any
            holder of the source decrypt every stored CDN API key.
    """
    if not raw_secret:
        try:
            raw_secret = current_app.config.get('CDN_ENCRYPTION_KEY', '')
        except RuntimeError:
            raw_secret = ''

    if not raw_secret:
        raise RuntimeError(
            "CDN_ENCRYPTION_KEY is not configured. Set it to a Fernet key "
            "(generate with: python -c \"from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())\") and restart."
        )

    # Hash raw secret with SHA-256 to guarantee 32 url-safe base64 bytes for Fernet
    key_bytes = hashlib.sha256(raw_secret.encode('utf-8')).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt_api_key(api_key: str, secret: str = None) -> str:
    """Encrypt a raw API key using Fernet symmetric encryption."""
    if not api_key:
        return ""
    f = _get_fernet_instance(secret)
    encrypted_bytes = f.encrypt(api_key.encode('utf-8'))
    return encrypted_bytes.decode('utf-8')


def decrypt_api_key(encrypted_api_key: str, secret: str = None) -> str:
    """Decrypt an encrypted API key ("" on failure, e.g. after key rotation)."""
    if not encrypted_api_key:
        return ""
    try:
        f = _get_fernet_instance(secret)
        decrypted_bytes = f.decrypt(encrypted_api_key.encode('utf-8'))
        return decrypted_bytes.decode('utf-8')
    except RuntimeError:
        raise
    except Exception as exc:
        # Wrong key / corrupted payload — do not leak details to callers.
        log.warning("Failed to decrypt a stored CDN credential: %s", type(exc).__name__)
        return ""


def mask_api_key(api_key: str) -> str:
    """Mask an API key for safe UI display (e.g. ••••••••••••abcd)."""
    if not api_key:
        return "••••••••"
    if len(api_key) <= 8:
        return "••••" + api_key[-2:] if len(api_key) > 2 else "••••"
    return "••••••••••••" + api_key[-4:]
