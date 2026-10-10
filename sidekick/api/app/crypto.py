"""Encrypt integration secrets at rest (Fernet / AES-128-CBC + HMAC)."""
import json
import os

from cryptography.fernet import Fernet, InvalidToken

_key = os.getenv("SIDEKICK_SECRET_KEY", "")
if not _key:
    raise SystemExit("SIDEKICK_SECRET_KEY is not set. Generate one with:\n"
                     "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
_f = Fernet(_key.encode())


def seal(data: dict) -> bytes:
    return _f.encrypt(json.dumps(data).encode())


def unseal(blob) -> dict:
    if not blob:
        return {}
    try:
        return json.loads(_f.decrypt(bytes(blob)))
    except InvalidToken:
        return {}  # key rotated / wrong key: treat as no secrets saved
