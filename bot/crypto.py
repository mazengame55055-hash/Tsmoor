"""Symmetric encryption for Kaggle tokens stored in SQLite (Fernet = AES-128-CBC + HMAC)."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from . import config

_fernet = Fernet(config.ENCRYPTION_KEY)


def encrypt(plain: str) -> bytes:
    return _fernet.encrypt(plain.encode("utf-8"))


def decrypt(blob: bytes) -> str:
    """Raises InvalidToken if the key changed or the data was tampered with."""
    return _fernet.decrypt(blob).decode("utf-8")


__all__ = ["encrypt", "decrypt", "InvalidToken"]
