import base64
import os
from typing import Optional, Union

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENC_PREFIX = 'enc::'


def _load_data_key() -> bytes:
    raw = os.environ.get('DATA_ENCRYPTION_KEY', '').strip()
    if not raw:
        raise RuntimeError('DATA_ENCRYPTION_KEY is required and must be base64 of 32 bytes')

    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise RuntimeError('DATA_ENCRYPTION_KEY is not valid base64') from exc

    if len(key) != 32:
        raise RuntimeError('DATA_ENCRYPTION_KEY must decode to exactly 32 bytes for AES-256-GCM')

    return key


def _aesgcm() -> AESGCM:
    return AESGCM(_load_data_key())


def encrypt_text(value: Optional[Union[str, int, float]]) -> Optional[str]:
    if value is None:
        return None

    text = value if isinstance(value, str) else str(value)
    if text == '':
        return ''
    if text.startswith(ENC_PREFIX):
        return text

    nonce = os.urandom(12)
    ciphertext = _aesgcm().encrypt(nonce, text.encode('utf-8'), None)
    token = base64.b64encode(nonce + ciphertext).decode('utf-8')
    return ENC_PREFIX + token


def decrypt_text(value: Optional[Union[str, int, float]]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    if value == '':
        return ''
    if not value.startswith(ENC_PREFIX):
        return value

    token = value[len(ENC_PREFIX):]
    try:
        blob = base64.b64decode(token, validate=True)
        if len(blob) < 13:
            return value
        nonce = blob[:12]
        ciphertext = blob[12:]
        plaintext = _aesgcm().decrypt(nonce, ciphertext, None)
        return plaintext.decode('utf-8')
    except Exception:
        return value
