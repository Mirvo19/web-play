import base64
import hashlib
from cryptography.fernet import Fernet
from flask import current_app

def _get_fernet_instance(raw_secret: str = None) -> Fernet:
    """Generate a valid Fernet instance from the raw secret string."""
    if not raw_secret:
        try:
            raw_secret = current_app.config.get('CDN_ENCRYPTION_KEY', '')
        except RuntimeError:
            raw_secret = "default-fallback-secret-key-32bytes-long!!"
    
    if not raw_secret:
        raw_secret = "default-fallback-secret-key-32bytes-long!!"

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
    """Decrypt an encrypted API key."""
    if not encrypted_api_key:
        return ""
    try:
        f = _get_fernet_instance(secret)
        decrypted_bytes = f.decrypt(encrypted_api_key.encode('utf-8'))
        return decrypted_bytes.decode('utf-8')
    except Exception as e:
        # If decryption fails (e.g. key changed), return empty or log error
        return ""

def mask_api_key(api_key: str) -> str:
    """Mask an API key for safe UI display (e.g. ••••••••••••abcd)."""
    if not api_key:
        return "••••••••"
    if len(api_key) <= 8:
        return "••••" + api_key[-2:] if len(api_key) > 2 else "••••"
    return "••••••••••••" + api_key[-4:]
