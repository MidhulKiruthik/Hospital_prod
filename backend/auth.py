import jwt
import datetime
from functools import wraps
from flask import request, jsonify, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from models import User, AuditLog, db
import json


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return check_password_hash(hashed, password)


def generate_token(user: User) -> str:
    payload = {
        'user_id': user.id,
        'username': user.username,
        'role': user.role,
        'exp': datetime.datetime.utcnow() + datetime.timedelta(
            hours=current_app.config['JWT_EXPIRATION_HOURS']
        )
    }
    return jwt.encode(payload, current_app.config['JWT_SECRET_KEY'], algorithm='HS256')


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
                return jsonify({'error': 'Insufficient permissions'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def write_audit(action: str, entity_type: str = '', entity_id: int = None,
                details: dict = None, user_id: int = None):
    """Write an immutable audit log entry."""
    try:
        log = AuditLog(
            user_id=user_id or getattr(getattr(request, 'current_user', {}), 'get', lambda k, d=None: d)('user_id'),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps(details or {}),
            ip_address=request.remote_addr or ''
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        pass   # audit must never break main flow
