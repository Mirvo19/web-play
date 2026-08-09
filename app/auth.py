import time
import jwt
import requests
from functools import wraps
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify, redirect, url_for, make_response, render_template, current_app

auth_bp = Blueprint('auth', __name__)

def generate_session_jwt(user_id: str, email: str) -> Tuple[str, int]:
    """Generate a 24-hour JWT session token and return (token, exp_timestamp)."""
    secret = current_app.config.get('JWT_SECRET_KEY', 'default-jwt-secret-key')
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
    secret = current_app.config.get('JWT_SECRET_KEY', 'default-jwt-secret-key')
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


@auth_bp.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'GET':
        user = get_current_user()
        if user:
            return redirect(url_for('views.dashboard'))
        return render_template('login.html')

    # Handle POST login
    data = request.get_json() if request.is_json else request.form
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    supabase_url = current_app.config.get('SUPABASE_URL', '').rstrip('/')
    supabase_key = current_app.config.get('SUPABASE_ANON_KEY', '')

    user_id = None
    authenticated = False

    # 1. Try Supabase Auth API login
    if supabase_url and supabase_key and "your-project-ref" not in supabase_url:
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
                authenticated = True
            else:
                err_msg = resp.json().get('error_description') or resp.json().get('msg') or 'Invalid Supabase credentials'
                return jsonify({'error': err_msg}), 401
        except Exception as e:
            return jsonify({'error': f'Supabase Auth service error: {str(e)}'}), 500

    # 2. Fallback test credentials if Supabase URL is not configured yet
    if not authenticated:
        if email and len(password) >= 6:
            user_id = f"user_{hash(email)}"
            authenticated = True
        else:
            return jsonify({'error': 'Invalid credentials'}), 401

    # Issue 24-hour JWT session token
    token, exp_timestamp = generate_session_jwt(user_id, email)

    response = make_response(jsonify({
        'message': 'Login successful',
        'access_token': token,
        'expires_at': exp_timestamp,
        'email': email
    }))

    # Set 24-hour session cookie
    response.set_cookie(
        'access_token',
        token,
        max_age=86400,
        httponly=True,
        samesite='Lax'
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
