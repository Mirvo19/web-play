import time
import uuid
import jwt
import requests
from functools import wraps
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from flask import Blueprint, request, jsonify, redirect, url_for, make_response, render_template, current_app

auth_bp = Blueprint('auth', __name__)

_LOOPBACK_ADDRS = {'127.0.0.1', '::1', 'localhost'}


def _jwt_secret() -> str:
    secret = current_app.config.get('JWT_SECRET_KEY', '')
    if not secret:
        raise RuntimeError(
            "JWT_SECRET_KEY is not configured. Set a strong random value and restart."
        )
    return secret


def generate_session_jwt(user_id: str, email: str) -> Tuple[str, int]:
    """Generate a 24-hour JWT session token and return (token, exp_timestamp)."""
    secret = _jwt_secret()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=24)
    exp_timestamp = int(exp.timestamp())

    payload = {
        'sub': user_id,
        'email': email,
        'iat': int(now.timestamp()),
        'exp': exp_timestamp
    }

    token = jwt.encode(payload, secret, algorithm='HS256')
    return token, exp_timestamp


def verify_session_jwt(token: str) -> Optional[dict]:
    """Verify session JWT and return decoded payload if valid."""
    if not token:
        return None
    # Strip Bearer prefix if present
    if token.startswith("Bearer "):
        token = token[7:]
    try:
        secret = _jwt_secret()
    except RuntimeError:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=['HS256'])
        return payload
    except Exception:
        return None


def get_current_user():
    """Extract current user payload from cookie or Authorization header."""
    token = request.cookies.get('access_token')
    if not token:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
    return verify_session_jwt(token)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized', 'code': 'JWT_EXPIRED'}), 401
            return redirect(url_for('auth.login_page'))
        return f(*args, **kwargs)
    return decorated_function


def supabase_configured() -> bool:
    """True only when a real (non-placeholder) Supabase project is configured."""
    url = (current_app.config.get('SUPABASE_URL', '') or '').strip().rstrip('/')
    key = (current_app.config.get('SUPABASE_ANON_KEY', '') or '').strip()
    if not url or not key:
        return False
    placeholders = ('your-project-ref', 'your-supabase', 'example', 'changeme', 'placeholder')
    if any(p in url.lower() for p in placeholders):
        return False
    return True


def insecure_dev_auth_enabled() -> bool:
    return bool(current_app.config.get('ALLOW_INSECURE_DEV_AUTH', False))


def auth_mode() -> str:
    """'supabase' when real auth is configured, else 'insecure-dev' or 'disabled'."""
    if supabase_configured():
        return 'supabase'
    if insecure_dev_auth_enabled():
        return 'insecure-dev'
    return 'disabled'


def _is_loopback_request() -> bool:
    return (request.remote_addr or '') in _LOOPBACK_ADDRS


@auth_bp.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'GET':
        user = get_current_user()
        if user:
            return redirect(url_for('views.dashboard'))
        return render_template('login.html', auth_mode=auth_mode())

    # Handle POST login
    data = request.get_json() if request.is_json else request.form
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    if supabase_configured():
        supabase_url = current_app.config.get('SUPABASE_URL', '').rstrip('/')
        supabase_key = current_app.config.get('SUPABASE_ANON_KEY', '')
        try:
            auth_endpoint = f"{supabase_url}/auth/v1/token?grant_type=password"
            headers = {
                'apikey': supabase_key,
                'Content-Type': 'application/json'
            }
            payload = {'email': email, 'password': password}
            resp = requests.post(auth_endpoint, headers=headers, json=payload, timeout=10)

            if resp.status_code == 200:
                resp_data = resp.json()
                user_info = resp_data.get('user', {})
                user_id = user_info.get('id', email)
            else:
                try:
                    err_body = resp.json()
                except Exception:
                    err_body = {}
                err_msg = (err_body.get('error_description')
                           or err_body.get('msg')
                           or 'Invalid credentials')
                return jsonify({'error': err_msg}), 401
        except requests.RequestException as e:
            return jsonify({'error': f'Supabase Auth service error: {str(e)}'}), 502

        authenticated_user_id = user_id
    elif insecure_dev_auth_enabled() and _is_loopback_request():
        # FAIL-CLOSED fallback: only when the operator explicitly opted in
        # AND the request comes from loopback. Bla — non-loopback clients
        # always get 503 (see below), even with the flag on.
        if len(password) < 6:
            return jsonify({'error': 'Invalid credentials'}), 401
        current_app.logger.warning(
            "Insecure dev-mode login used for %s from %s — enable real Supabase auth.",
            email, request.remote_addr,
        )
        authenticated_user_id = f"dev_{uuid.uuid4().hex[:12]}"
    else:
        # FAIL CLOSED: no real auth configured (or non-localhost client
        # hitting a dev-mode instance). Never accept arbitrary credentials.
        if insecure_dev_auth_enabled():
            return jsonify({
                'error': 'Insecure dev-mode auth only permits loopback clients; '
                         'configure Supabase Auth for network access.'
            }), 403
        return jsonify({
            'error': 'Authentication is not configured on this server. '
                     'Set SUPABASE_URL / SUPABASE_ANON_KEY and restart.'
        }), 503

    # Issue 24-hour JWT session token
    token, exp_timestamp = generate_session_jwt(authenticated_user_id, email)

    response = make_response(jsonify({
        'message': 'Login successful',
        'access_token': token,
        'expires_at': exp_timestamp,
        'email': email
    }))

    # Set 24-hour session cookie
    secure_cookie = current_app.config.get('FLASK_ENV', 'production') == 'production'
    response.set_cookie(
        'access_token',
        token,
        max_age=86400,
        httponly=True,
        samesite='Lax',
        secure=secure_cookie,
    )
    return response


@auth_bp.route('/logout', methods=['POST', 'GET'])
def logout():
    response = make_response(redirect(url_for('auth.login_page')))
    response.delete_cookie('access_token')
    return response


@auth_bp.route('/api/auth/session', methods=['GET'])
def get_session_info():
    user = get_current_user()
    if not user:
        return jsonify({'authenticated': False}), 401

    now = int(datetime.now(timezone.utc).timestamp())
    exp = user.get('exp', 0)
    remaining_seconds = max(0, exp - now)

    # 3-hour warning trigger (10800 seconds)
    warning_3h = remaining_seconds <= 10800

    return jsonify({
        'authenticated': True,
        'user_id': user.get('sub'),
        'email': user.get('email'),
        'expires_at': exp,
        'remaining_seconds': remaining_seconds,
        'warning_3h': warning_3h
    })
