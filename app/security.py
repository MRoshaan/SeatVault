# =============================================================================
# app/security.py
# Password hashing and verification helpers.
# =============================================================================

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    """Return a salted bcrypt hash for a plaintext password."""
    return _pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a stored hash."""
    return _pwd_context.verify(plain_password, hashed_password)
