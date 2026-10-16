"""
Security primitives:
  - Password hashing (bcrypt)
  - Session tokens (JWT)
  - Symmetric encryption for tenant secrets (Fernet / MultiFernet for rotation)

FERNET_KEY env: one or more comma-separated urlsafe-base64 32-byte keys.
The first is used to encrypt; all are tried for decrypt (enables key rotation).
If unset in dev, a key is derived from JWT_SECRET so the app still runs locally —
but production MUST set a real FERNET_KEY.
"""

import base64
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from cryptography.fernet import Fernet, MultiFernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)


# ----------------------------- passwords ----------------------------------- #
# Using the bcrypt library directly (passlib 1.7 is incompatible with bcrypt 4.x).
# bcrypt has a hard 72-byte input limit, so we pre-hash with SHA-256 → base64
# (keeps full entropy of long passwords, stays under the limit).

def _prehash(plain: str) -> bytes:
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_prehash(plain), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(plain), hashed.encode("utf-8"))
    except Exception:
        return False


# ----------------------------- JWT sessions -------------------------------- #

def create_access_token(user_id: str, tenant_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None


# --------------------------- secret encryption ----------------------------- #

def _build_fernet() -> MultiFernet:
    keys = []
    if settings.fernet_key:
        for k in settings.fernet_key.split(","):
            k = k.strip()
            if k:
                keys.append(Fernet(k.encode() if isinstance(k, str) else k))
    if not keys:
        # Dev fallback: derive a stable key from JWT secret. NOT for production.
        digest = hashlib.sha256(settings.jwt_secret.encode()).digest()
        derived = base64.urlsafe_b64encode(digest)
        keys.append(Fernet(derived))
        logger.warning("FERNET_KEY not set — using derived dev key. Set FERNET_KEY in production!")
    return MultiFernet(keys)


_fernet = _build_fernet()


def encrypt_secret(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt secret — wrong/rotated FERNET_KEY?")
        raise


def generate_fernet_key() -> str:
    """Helper to mint a new key for FERNET_KEY env (run once, save the output)."""
    return Fernet.generate_key().decode()
