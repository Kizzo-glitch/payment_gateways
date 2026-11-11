"""Encryption for provider credentials stored in the database (Fernet / AES)."""
import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet():
    key = getattr(settings, "PAYMENTS_ENCRYPTION_KEY", None)
    if not key:
        raise ImproperlyConfigured("PAYMENTS_ENCRYPTION_KEY is not set.")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_dict(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data or {}).encode()).decode()


def decrypt_dict(token: str) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except InvalidToken as exc:
        raise ImproperlyConfigured(
            "Could not decrypt payment credentials. Was PAYMENTS_ENCRYPTION_KEY changed?"
        ) from exc
