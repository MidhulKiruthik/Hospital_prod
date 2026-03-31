import jwt
import datetime
import hashlib
import re
import secrets
from functools import wraps
from flask import request, jsonify, current_app, has_request_context
from werkzeug.security import generate_password_hash, check_password_hash
from models import User, AuditLog, AuthSession, SecurityEvent, db
import json
from urllib import request as urllib_request


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return check_password_hash(hashed, password)


def validate_password_policy(password: str) -> str:
    value = password or ''
    if len(value) < 12:
        return 'Password must be at least 12 characters long'
    if re.search(r'[A-Z]', value) is None:
        return 'Password must include at least one uppercase letter'
    if re.search(r'[a-z]', value) is None:
        return 'Password must include at least one lowercase letter'
    if re.search(r'\d', value) is None:
        return 'Password must include at least one number'
    if re.search(r'[^A-Za-z0-9]', value) is None:
        return 'Password must include at least one special character'
    return ''


def generate_token(user: User) -> str:
    payload = {
        'user_id': user.id,
        'username': user.username,
        'role': user.role,
        'token_version': user.token_version,
        'type': 'access',
        'exp': datetime.datetime.utcnow() + datetime.timedelta(
            hours=current_app.config['JWT_EXPIRATION_HOURS']
        )
    }
    return jwt.encode(payload, current_app.config['JWT_SECRET_KEY'], algorithm='HS256')


def _hash_token(token_value: str) -> str:
    return hashlib.sha256((token_value or '').encode('utf-8')).hexdigest()


def _request_context_details() -> dict:
    if not has_request_context():
        return {'ip': '', 'path': '', 'user_agent': ''}
    return {
        'ip': request.remote_addr or '',
        'path': request.path or '',
        'user_agent': request.headers.get('User-Agent', ''),
    }


def log_security_event(event_type: str, severity: str = 'medium', details: dict = None, user_id: int = None) -> None:
    payload_details = details or {}
    ctx = _request_context_details()
    event = SecurityEvent(
        event_type=event_type,
        severity=severity,
        user_id=user_id,
        source_ip=ctx['ip'],
        request_path=ctx['path'],
        details_json=json.dumps(payload_details, sort_keys=True),
    )
    db.session.add(event)
    db.session.commit()

    if current_app.config.get('SECURITY_EVENT_EXPORT_ENABLED', False):
        _export_security_event(event)


def _export_security_event(event: SecurityEvent) -> None:
    webhook_url = current_app.config.get('SECURITY_EVENT_WEBHOOK_URL', '').strip()
    if not webhook_url:
        return

    body = json.dumps(event.to_dict()).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    token = current_app.config.get('SECURITY_EVENT_WEBHOOK_TOKEN', '').strip()
    if token:
        headers['Authorization'] = f'Bearer {token}'

    req = urllib_request.Request(webhook_url, data=body, method='POST', headers=headers)
    timeout = max(1, int(current_app.config.get('SECURITY_EVENT_WEBHOOK_TIMEOUT_SECONDS', 3)))
    try:
        with urllib_request.urlopen(req, timeout=timeout):
            return
    except Exception:
        return


def create_refresh_session(user: User) -> str:
    raw_token = secrets.token_urlsafe(48)
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(
        days=current_app.config.get('REFRESH_TOKEN_EXPIRATION_DAYS', 7)
    )
    ctx = _request_context_details()
    session = AuthSession(
        user_id=user.id,
        refresh_token_hash=_hash_token(raw_token),
        expires_at=expires_at,
        ip_address=ctx['ip'],
        user_agent=ctx['user_agent'],
        last_seen_at=datetime.datetime.utcnow(),
    )
    db.session.add(session)
    db.session.commit()
    return raw_token


def refresh_access_token(refresh_token: str):
    token_hash = _hash_token(refresh_token)
    session = AuthSession.query.filter_by(refresh_token_hash=token_hash).first()
    if not session or not session.is_active():
        return None, 'Invalid or expired refresh token'

    user = User.query.get(session.user_id)
    if not user:
        return None, 'User not found'

    if user.token_version is None:
        user.token_version = 0

    session.last_seen_at = datetime.datetime.utcnow()
    db.session.commit()
    return generate_token(user), None


def revoke_refresh_session(refresh_token: str) -> bool:
    token_hash = _hash_token(refresh_token)
    session = AuthSession.query.filter_by(refresh_token_hash=token_hash).first()
    if not session:
        return False
    session.revoked_at = datetime.datetime.utcnow()
    db.session.commit()
    return True


def revoke_all_sessions_for_user(user: User) -> None:
    now = datetime.datetime.utcnow()
    AuthSession.query.filter_by(user_id=user.id, revoked_at=None).update({'revoked_at': now})
    user.token_version = (user.token_version or 0) + 1
    db.session.commit()


def decode_token(token: str) -> dict:
    return jwt.decode(token, current_app.config['JWT_SECRET_KEY'], algorithms=['HS256'])


def token_required(f):
    """Decorator: require valid JWT in Authorization header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        if not token:
            return jsonify({'error': 'Token missing'}), 401
        try:
            payload = decode_token(token)
            if payload.get('type') != 'access':
                return jsonify({'error': 'Invalid token type'}), 401
            user = User.query.get(payload.get('user_id'))
            if not user:
                return jsonify({'error': 'Invalid token'}), 401
            if payload.get('token_version', -1) != user.token_version:
                return jsonify({'error': 'Token revoked'}), 401
            request.current_user = payload
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Invalid token'}), 401
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    """Decorator: require specific roles."""
    def decorator(f):
        @wraps(f)
        @token_required
        def decorated(*args, **kwargs):
            if request.current_user.get('role') not in roles:
                try:
                    log_security_event(
                        'authorization_denied',
                        severity='high',
                        details={
                            'required_roles': list(roles),
                            'actual_role': request.current_user.get('role'),
                        },
                        user_id=request.current_user.get('user_id'),
                    )
                except Exception:
                    pass
                return jsonify({'error': 'Insufficient permissions'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def write_audit(action: str, entity_type: str = '', entity_id: int = None,
                details: dict = None, user_id: int = None):
    """Write an append-only audit log entry with hash chain integrity."""
    try:
        request_user_id = None
        remote_ip = ''
        if has_request_context():
            request_user_id = getattr(request, 'current_user', {}).get('user_id')
            remote_ip = request.remote_addr or ''

        details_json = json.dumps(details or {}, sort_keys=True)
        timestamp = datetime.datetime.utcnow()
        previous = AuditLog.query.order_by(AuditLog.id.desc()).first()
        previous_hash = previous.entry_hash if previous else ''
        payload = '|'.join([
            timestamp.isoformat(),
            str(user_id or request_user_id or ''),
            action or '',
            entity_type or '',
            str(entity_id or ''),
            details_json,
            remote_ip,
            previous_hash,
        ])
        entry_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()

        log = AuditLog(
            timestamp=timestamp,
            user_id=user_id or request_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details_json,
            ip_address=remote_ip,
            previous_hash=previous_hash,
            entry_hash=entry_hash,
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        pass   # audit must never break main flow


def verify_audit_integrity() -> dict:
    previous_hash = ''
    checked = 0
    legacy_rows = 0
    chain_started = False

    for row in AuditLog.query.order_by(AuditLog.id.asc()).all():
        if not (row.entry_hash or '').strip():
            if chain_started:
                return {
                    'ok': False,
                    'checked': checked,
                    'failed_id': row.id,
                    'reason': 'legacy hash gap found inside hashed audit chain',
                }
            legacy_rows += 1
            continue

        timestamp = row.timestamp.isoformat() if row.timestamp else ''
        payload = '|'.join([
            timestamp,
            str(row.user_id or ''),
            row.action or '',
            row.entity_type or '',
            str(row.entity_id or ''),
            row.details or '{}',
            row.ip_address or '',
            previous_hash,
        ])
        expected_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        checked += 1
        chain_started = True

        if row.previous_hash != previous_hash:
            return {
                'ok': False,
                'checked': checked,
                'failed_id': row.id,
                'reason': 'previous_hash mismatch',
            }

        if row.entry_hash != expected_hash:
            return {
                'ok': False,
                'checked': checked,
                'failed_id': row.id,
                'reason': 'entry_hash mismatch',
            }

        previous_hash = row.entry_hash

    return {'ok': True, 'checked': checked, 'legacy_rows_without_hash': legacy_rows}
