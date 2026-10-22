"""The admin password: what the kill switch asks for before it fires.

Stored as a salted scrypt hash in `settings`, never the password.
"""

from __future__ import annotations

import hashlib
import hmac
import os

MIN_LENGTH = 12
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=_N, r=_R, p=_P)
    return hmac.compare_digest(candidate.hex(), digest)
