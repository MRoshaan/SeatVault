# =============================================================================
# app/dependencies.py
# FastAPI dependency injection providers.
#
# Centralizing dependencies here means:
#   - Every endpoint declares what it needs via type hints — no hidden globals
#   - Mocking for tests is trivial: just override the dependency
#   - Resource lifetimes (DB sessions, Redis connections) are managed in one place
# =============================================================================

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Generator

from fastapi import Depends, Header, HTTPException, Request, status
import jwt
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import User
from app.redis_client import DistributedLockManager, lock_manager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_app_settings() -> Settings:
    """Inject the application settings singleton."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


# ---------------------------------------------------------------------------
# Database session
# ---------------------------------------------------------------------------

def get_database_session() -> Generator[Session, None, None]:
    """
    Yields a SQLAlchemy session scoped to the current request.
    Auto-rollback on exception, always closes on exit.
    """
    yield from get_db()


DbSession = Annotated[Session, Depends(get_database_session)]


# ---------------------------------------------------------------------------
# Lock manager
# ---------------------------------------------------------------------------

def get_lock_manager() -> DistributedLockManager:
    """Inject the module-level Redis lock manager singleton."""
    return lock_manager


LockManagerDep = Annotated[DistributedLockManager, Depends(get_lock_manager)]


# ---------------------------------------------------------------------------
# Request ID tracing
# ---------------------------------------------------------------------------

def get_request_id(
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    request: Request = None,
) -> str:
    """
    Extract the X-Request-ID header for distributed tracing.
    If the client doesn't supply one, fall back to the FastAPI request's
    unique state id (set in the middleware).
    """
    if x_request_id:
        return x_request_id
    if request and hasattr(request.state, "request_id"):
        return request.state.request_id
    import uuid
    return str(uuid.uuid4())


RequestIdDep = Annotated[str, Depends(get_request_id)]


def create_access_token(user: User, settings: Settings) -> str:
    """Create a signed JWT token for the authenticated user."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "iat": now,
        "exp": expires_at,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def get_current_user(
    db: DbSession,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> User:
    """
    Resolve the authenticated user from a Bearer JWT token.
    Returns 401 for missing/invalid tokens and 403 for inactive users.
    """
    unauthorized_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "INVALID_TOKEN", "message": "Missing or invalid Bearer token"},
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not authorization:
        raise unauthorized_exc

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized_exc

    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except InvalidTokenError as exc:
        raise unauthorized_exc from exc

    sub = payload.get("sub")
    try:
        user_id = int(sub)
    except (TypeError, ValueError) as exc:
        raise unauthorized_exc from exc

    user = db.get(User, user_id)
    if user is None:
        raise unauthorized_exc
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "USER_INACTIVE", "message": "This account is deactivated"},
        )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# User resolution
# get_current_user now validates JWT Bearer tokens.
# get_user_by_id remains as a utility for non-auth lookup flows.
# ---------------------------------------------------------------------------

def get_user_by_id(user_id: int, db: DbSession) -> User:
    """
    Resolve a user from the DB. Raises 404 if not found, 403 if inactive.

    In production: replace with JWT decode + DB lookup.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "USER_NOT_FOUND", "message": f"User {user_id} not found"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "USER_INACTIVE", "message": "This account is deactivated"},
        )
    return user


# ---------------------------------------------------------------------------
# Rate limit enforcement
# ---------------------------------------------------------------------------

def enforce_rate_limit(
    user_id: int,
    lm: LockManagerDep,
    settings: SettingsDep,
) -> None:
    """
    Reusable dependency that enforces the sliding-window rate limit.
    Raise 429 if the user has exceeded their booking request quota.
    """
    allowed, count = lm.check_rate_limit(
        user_id=user_id,
        max_requests=settings.RATE_LIMIT_MAX_REQUESTS,
        window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
    )
    if not allowed:
        logger.warning("Rate limit exceeded for user_id=%s count=%s", user_id, count)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(settings.RATE_LIMIT_WINDOW_SECONDS)},
            detail={
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "Too many booking attempts. Please wait before trying again.",
                "retry_after_seconds": settings.RATE_LIMIT_WINDOW_SECONDS,
            },
        )
