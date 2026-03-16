# =============================================================================
# app/database.py
# SQLAlchemy engine, session factory, and Base declaration.
#
# Connection pool is tuned for high-concurrency flash-sale scenarios:
#   - pool_size=20 keeps persistent connections ready so we pay no TCP
#     handshake cost on each request.
#   - max_overflow=40 allows burst headroom during the initial sale rush.
#   - pool_pre_ping=True silently drops stale connections before handing
#     them to a worker — no "MySQL server has gone away" surprises.
# =============================================================================

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine — one per process, shared across all threads/coroutines
# ---------------------------------------------------------------------------
engine = create_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=True,   # liveness check before checkout — eliminates stale conn errors
    echo=settings.DEBUG,  # logs every SQL statement when DEBUG=True
)


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,   # we control commits explicitly
    autoflush=False,    # flush manually so we control when SQL hits the wire
    expire_on_commit=False,  # keep ORM objects usable after commit (important for async-style code)
)


# ---------------------------------------------------------------------------
# Declarative Base — all ORM models inherit from this
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a DB session per request and guarantees
    the session is closed even if the handler raises an exception.

    Usage:
        @router.get("/example")
        def example(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def get_db_context() -> Generator[Session, None, None]:
    """
    Context-manager version for use outside of FastAPI (e.g. Celery tasks).

    Usage:
        with get_db_context() as db:
            db.add(obj)
            db.commit()
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Database transaction rolled back")
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# MySQL-specific session hardening
# Fired once per new connection checkout from the pool.
# ---------------------------------------------------------------------------
@event.listens_for(engine, "connect")
def set_mysql_session_vars(dbapi_connection, connection_record):  # noqa: ARG001
    """
    Enforce strict isolation and character set for every raw connection.
    REPEATABLE READ is MySQL's default but we set it explicitly so it
    cannot be accidentally changed by a misconfigured server.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    cursor.execute("SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci")
    cursor.close()
