"""Symmetric encryption for secrets stored in the database.

Per-site WordPress application passwords are stored encrypted, never in plain
text. The key is derived from ENCRYPTION_KEY (or SECRET_KEY as a fallback) so no
extra configuration is required to get started. If that key changes, previously
stored passwords can no longer be decrypted and must be re-entered.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    raw = os.getenv("ENCRYPTION_KEY") or os.getenv("SECRET_KEY") or "dev-only-insecure"
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
