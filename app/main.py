# =============================================================================
# app/main.py
# FastAPI application — the public-facing API gateway.
#
# Endpoints:
#   POST   /book/{seat_id}               — acquire lock + enqueue Celery task
#   GET    /booking/status/{task_id}     — poll Celery task result
#   GET    /booking/{booking_id}         — retrieve committed booking record
#   DELETE /booking/{booking_id}/cancel  — enqueue cancellation task
#   GET    /events                       — list all active events
#   GET    /events/{event_id}/seats      — list seats with status
#   POST   /events                       — create event (admin)
#   POST   /users                        — register user
#   POST   /auth/login                   — issue JWT access token
#   GET    /health                       — deep health check (Redis, RabbitMQ, MySQL)
# =============================================================================

import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from app.config import settings
from app.database import Base, engine, get_db_context
from app.dependencies import (
    CurrentUserDep,
    DbSession,
    LockManagerDep,
    RequestIdDep,
    SettingsDep,
    create_access_token,
)
from app.models import Booking, BookingStatus, Event, Seat, SeatStatus, User
from app.redis_client import lock_manager
from app.schemas import (
    BookingAcceptedResponse,
    BookingDetailResponse,
    BookingRequest,
    BookingStatusResponse,
    ConflictResponse,
    EventCreate,
    EventResponse,
    HealthResponse,
    LoginRequest,
    SeatListResponse,
    SeatResponse,
    ServiceHealth,
    TokenResponse,
    UserCreate,
    UserResponse,
)
from app.tasks import celery_app, process_booking, process_cancellation

# ---------------------------------------------------------------------------
# Logging — structured JSON-style format with request-id support
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application lifespan — startup / shutdown hooks
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once at startup and once at shutdown.
    Good place for: table creation, cache warm-up, connection pool pre-fill.
    """
    logger.info("=== Lockdown API starting up ===")

    # Create tables if they don't exist yet (idempotent)
    # In production: use Alembic migrations instead
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables verified/created")

    # Verify Redis connectivity at startup
    if not lock_manager.ping():
        logger.error("REDIS IS UNREACHABLE — lock manager will not work!")
    else:
        logger.info("Redis connection verified")

    yield  # ← application runs here

    logger.info("=== Lockdown API shutting down ===")


# ---------------------------------------------------------------------------
# FastAPI application instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "High-concurrency flash-sale / ticketing API with distributed locks, "
        "optimistic concurrency control, and async payment processing."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

# CORS — tighten origins in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # lock down to your frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_and_timing_middleware(request: Request, call_next):
    """
    Attach a unique request ID to every request for distributed tracing
    and measure end-to-end latency for every response.
    """
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    start = time.perf_counter()
    response: Response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.2f}"

    logger.info(
        "REQUEST method=%s path=%s status=%s latency_ms=%.2f request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        request_id,
    )
    return response


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("Unhandled exception request_id=%s", request_id)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected error occurred",
            "request_id": request_id,
        },
    )


# =============================================================================
# CORE BOOKING ENDPOINT
# =============================================================================

@app.post(
    "/book/{seat_id}",
    response_model=BookingAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Book a seat",
    description=(
        "Atomically acquires a distributed Redis lock for the seat, creates a "
        "PENDING booking record, and dispatches an async Celery task for payment "
        "processing. Returns 202 immediately so the client is never blocked. "
        "Poll `/booking/status/{task_id}` for the final result."
    ),
    responses={
        202: {"description": "Booking accepted and queued for processing"},
        409: {"description": "Seat is currently locked by another request", "model": ConflictResponse},
        404: {"description": "Seat or user not found"},
        422: {"description": "Validation error"},
        429: {"description": "Rate limit exceeded"},
    },
    tags=["Bookings"],
)
def book_seat(
    seat_id: int,
    payload: BookingRequest,
    db: DbSession,
    lm: LockManagerDep,
    cfg: SettingsDep,
    request_id: RequestIdDep,
    current_user: CurrentUserDep,
) -> BookingAcceptedResponse:
    """
    The "Lockdown" booking flow — the crown jewel of this API.

    RACE-CONDITION PROTECTION LAYERS (in order of application):
    ─────────────────────────────────────────────────────────────
    1. Redis inventory fast-path  (cached counter check — O(1), no lock needed)
    2. Idempotency key check      (DB unique constraint — safe retry, no double-charge)
    3. Redis SETNX distributed lock (prevents concurrent processing of same seat)
    4. DB ENUM + status check     (seat must still be AVAILABLE when Celery runs)
    5. Optimistic locking (version) (Celery detects stale DB state and retries)
    6. DB UNIQUE constraint on bookings.seat_id (final hard database guardrail)
    """

    if current_user.id != payload.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "USER_MISMATCH",
                "message": "Token user does not match payload user_id",
            },
        )

    # -----------------------------------------------------------------------
    # Layer 0: Rate limiting (sliding window, pure Redis)
    # -----------------------------------------------------------------------
    allowed, _ = lm.check_rate_limit(user_id=payload.user_id)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(cfg.RATE_LIMIT_WINDOW_SECONDS)},
            detail={
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "Too many booking attempts. Please wait before retrying.",
                "retry_after_seconds": cfg.RATE_LIMIT_WINDOW_SECONDS,
            },
        )

    # -----------------------------------------------------------------------
    # Layer 1: Idempotency check — safe client retry guard
    # If this exact idempotency_key has been seen before, return the
    # original booking immediately without re-processing.
    # -----------------------------------------------------------------------
    existing_booking = db.execute(
        select(Booking).where(Booking.idempotency_key == payload.idempotency_key)
    ).scalar_one_or_none()

    if existing_booking is not None:
        logger.info(
            "Idempotent replay: booking_id=%s idempotency_key=%s request_id=%s",
            existing_booking.id, payload.idempotency_key, request_id,
        )
        return BookingAcceptedResponse(
            status="accepted",
            message="Idempotent replay: original booking returned",
            task_id=existing_booking.celery_task_id or "N/A",
            booking_id=existing_booking.id,
            seat_id=existing_booking.seat_id,
            lock_ttl_seconds=cfg.SEAT_LOCK_TTL_SECONDS,
            poll_url=f"/booking/status/{existing_booking.celery_task_id}",
        )

    # -----------------------------------------------------------------------
    # Validate: seat and user must exist
    # -----------------------------------------------------------------------
    seat: Seat | None = db.get(Seat, seat_id)
    if seat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SEAT_NOT_FOUND", "message": f"Seat {seat_id} not found"},
        )

    user: User | None = db.get(User, payload.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "USER_NOT_FOUND", "message": f"User {payload.user_id} not found"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "USER_INACTIVE", "message": "Account is deactivated"},
        )

    # -----------------------------------------------------------------------
    # Quick status check — if the seat is already BOOKED or LOCKED, fail fast
    # before even attempting a Redis lock
    # -----------------------------------------------------------------------
    if seat.status == SeatStatus.BOOKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SEAT_ALREADY_BOOKED",
                "message": f"Seat {seat_id} has already been booked",
                "seat_id": seat_id,
            },
        )

    # -----------------------------------------------------------------------
    # Layer 2: Redis inventory fast-path
    # If we have a cached counter and it's ≤ 0, reject before acquiring a lock
    # -----------------------------------------------------------------------
    cached_inventory = lm.get_available_inventory(seat.event_id)
    if cached_inventory is not None and cached_inventory <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "EVENT_SOLD_OUT",
                "message": "This event is sold out",
                "seat_id": seat_id,
            },
        )

    # -----------------------------------------------------------------------
    # Layer 3: Acquire distributed Redis lock (SETNX — atomic, single round-trip)
    #
    # Only ONE request per seat_id will pass this gate simultaneously.
    # All others receive a 409 Conflict immediately — no waiting, no queuing.
    # This is the "Lockdown" — the core distributed systems primitive.
    # -----------------------------------------------------------------------
    lock_result = lm.acquire(seat_id=seat_id, ttl_seconds=cfg.SEAT_LOCK_TTL_SECONDS)

    if lock_result.failed:
        ttl = lm.get_lock_ttl(seat_id)
        logger.info(
            "Lock CONFLICT seat_id=%s ttl=%ss request_id=%s",
            seat_id, ttl, request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SEAT_LOCKED",
                "message": f"Seat {seat_id} is currently being processed by another request",
                "seat_id": seat_id,
                "lock_ttl_seconds": max(ttl, 0),
            },
        )

    # -----------------------------------------------------------------------
    # Lock acquired — from this point on we MUST release the lock if anything
    # goes wrong before the Celery task takes over responsibility.
    # -----------------------------------------------------------------------
    try:
        # Update seat status to LOCKED in the DB (visible to other workers)
        seat.status = SeatStatus.LOCKED
        seat.version += 1
        db.add(seat)

        # Pre-create the Booking record as PENDING so we have an ID to track
        booking = Booking(
            user_id=payload.user_id,
            seat_id=seat_id,
            status=BookingStatus.PENDING,
            idempotency_key=payload.idempotency_key,
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

    except Exception as exc:
        # Something went wrong writing to the DB — release the lock immediately
        db.rollback()
        _token = lock_result.token
        if _token:
            lm.release(seat_id=seat_id, token=_token)
        logger.exception("Failed to create pending booking seat_id=%s request_id=%s", seat_id, request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "BOOKING_CREATE_FAILED", "message": "Failed to initiate booking"},
        ) from exc

    # -----------------------------------------------------------------------
    # Dispatch to Celery via RabbitMQ — fire-and-forget
    # The task carries the lock_token so the worker can release the lock
    # atomically after the DB transaction commits (or on failure).
    # -----------------------------------------------------------------------
    task = process_booking.apply_async(
        kwargs={
            "seat_id": seat_id,
            "user_id": payload.user_id,
            "booking_id": booking.id,
            "lock_token": lock_result.token,
            "idempotency_key": payload.idempotency_key,
        },
        task_id=str(uuid.uuid4()),
        # Celery routing
        queue="bookings",
        # Make this task expire if it hasn't started within the lock TTL
        expires=cfg.SEAT_LOCK_TTL_SECONDS,
    )

    # Store the task ID on the booking for polling
    booking.celery_task_id = task.id
    db.add(booking)
    db.commit()

    logger.info(
        "Booking ENQUEUED task_id=%s booking_id=%s seat_id=%s user_id=%s request_id=%s",
        task.id, booking.id, seat_id, payload.user_id, request_id,
    )

    return BookingAcceptedResponse(
        status="accepted",
        message=(
            f"Booking for seat {seat_id} is being processed. "
            f"Poll the status URL to confirm."
        ),
        task_id=task.id,
        booking_id=booking.id,
        seat_id=seat_id,
        lock_ttl_seconds=cfg.SEAT_LOCK_TTL_SECONDS,
        poll_url=f"/booking/status/{task.id}",
    )


# =============================================================================
# BOOKING STATUS POLLING
# =============================================================================

@app.get(
    "/booking/status/{task_id}",
    response_model=BookingStatusResponse,
    summary="Poll Celery task status",
    description=(
        "Poll this endpoint after receiving a 202 from POST /book/{seat_id}. "
        "Returns the live Celery task state and, once complete, the full booking result."
    ),
    tags=["Bookings"],
)
def get_booking_status(task_id: str) -> BookingStatusResponse:
    """
    Maps raw Celery task states to client-friendly status strings:
      PENDING  → queued    (task has been received by broker, not yet started)
      STARTED  → processing
      SUCCESS  → confirmed | failed  (read from task result payload)
      FAILURE  → failed
      RETRY    → processing
    """
    result = celery_app.AsyncResult(task_id)
    celery_state = result.state

    # Map Celery states to user-friendly labels
    state_map: dict[str, str] = {
        "PENDING": "queued",
        "RECEIVED": "queued",
        "STARTED": "processing",
        "RETRY": "processing",
        "SUCCESS": "completed",
        "FAILURE": "failed",
        "REVOKED": "cancelled",
    }
    friendly_status = state_map.get(celery_state, celery_state.lower())

    response = BookingStatusResponse(
        task_id=task_id,
        celery_state=celery_state,
        status=friendly_status,
    )

    if celery_state == "SUCCESS" and isinstance(result.result, dict):
        payload: dict[str, Any] = result.result
        response.status = payload.get("status", friendly_status)
        response.booking_id = payload.get("booking_id")
        response.seat_id = payload.get("seat_id")
        response.payment_reference = payload.get("payment_reference")
        response.confirmed_at = payload.get("confirmed_at")

    elif celery_state == "FAILURE":
        response.failure_reason = str(result.result)

    return response


# =============================================================================
# BOOKING DETAIL
# =============================================================================

@app.get(
    "/booking/{booking_id}",
    response_model=BookingDetailResponse,
    summary="Get booking details",
    tags=["Bookings"],
)
def get_booking(booking_id: int, db: DbSession) -> BookingDetailResponse:
    booking: Booking | None = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "BOOKING_NOT_FOUND", "message": f"Booking {booking_id} not found"},
        )
    return BookingDetailResponse.model_validate(booking)


# =============================================================================
# CANCELLATION
# =============================================================================

@app.delete(
    "/booking/{booking_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel a confirmed booking",
    tags=["Bookings"],
)
def cancel_booking(booking_id: int, db: DbSession, current_user: CurrentUserDep) -> dict:
    booking: Booking | None = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "BOOKING_NOT_FOUND", "message": f"Booking {booking_id} not found"},
        )
    if booking.status != BookingStatus.CONFIRMED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "BOOKING_NOT_CANCELLABLE",
                "message": f"Booking {booking_id} is in status '{booking.status}' and cannot be cancelled",
            },
        )
    if booking.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "BOOKING_FORBIDDEN",
                "message": "You can only cancel your own bookings",
            },
        )

    task = process_cancellation.apply_async(
        kwargs={"booking_id": booking_id},
        queue="cancellations",
    )

    logger.info("Cancellation ENQUEUED booking_id=%s task_id=%s", booking_id, task.id)
    return {
        "status": "accepted",
        "message": f"Cancellation for booking {booking_id} is being processed",
        "task_id": task.id,
    }


# =============================================================================
# EVENTS
# =============================================================================

@app.get(
    "/events",
    response_model=list[EventResponse],
    summary="List all active events",
    tags=["Events"],
)
def list_events(db: DbSession) -> list[EventResponse]:
    events = db.execute(
        select(Event)
        .where(Event.is_active == True)
        .order_by(Event.event_date)
    ).scalars().all()
    return [EventResponse.model_validate(e) for e in events]


@app.post(
    "/events",
    response_model=EventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new event (admin)",
    tags=["Events"],
)
def create_event(payload: EventCreate, db: DbSession) -> EventResponse:
    event = Event(
        name=payload.name,
        description=payload.description,
        venue=payload.venue,
        event_date=payload.event_date,
        total_seats=payload.total_seats,
        available_seats=payload.total_seats,
        is_active=True,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    # Seed Redis inventory cache for this event
    lock_manager.seed_inventory(event.id, payload.total_seats)

    logger.info("Event CREATED id=%s name=%r seats=%s", event.id, event.name, payload.total_seats)
    return EventResponse.model_validate(event)


@app.get(
    "/events/{event_id}/seats",
    response_model=SeatListResponse,
    summary="List seats for an event",
    tags=["Events"],
)
def list_seats(event_id: int, db: DbSession) -> SeatListResponse:
    event: Event | None = db.get(Event, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "EVENT_NOT_FOUND", "message": f"Event {event_id} not found"},
        )

    seats = db.execute(
        select(Seat)
        .where(Seat.event_id == event_id)
        .order_by(Seat.seat_code)
    ).scalars().all()

    available_count = sum(1 for s in seats if s.status == SeatStatus.AVAILABLE)

    return SeatListResponse(
        event_id=event_id,
        total=len(seats),
        available=available_count,
        seats=[SeatResponse.model_validate(s) for s in seats],
    )


# =============================================================================
# USERS
# =============================================================================

@app.post(
    "/auth/login",
    response_model=TokenResponse,
    summary="Authenticate and receive JWT access token",
    tags=["Auth"],
)
def login(payload: LoginRequest, db: DbSession, cfg: SettingsDep) -> TokenResponse:
    user = db.execute(
        select(User).where(User.email == payload.email)
    ).scalar_one_or_none()

    # Placeholder check for this demo project's fake password format.
    if user is None or user.hashed_password != f"hashed_{payload.password}":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CREDENTIALS", "message": "Incorrect email or password"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "USER_INACTIVE", "message": "This account is deactivated"},
        )

    token = create_access_token(user=user, settings=cfg)
    return TokenResponse(
        access_token=token,
        expires_in=cfg.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user_id=user.id,
    )

@app.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    tags=["Users"],
)
def create_user(payload: UserCreate, db: DbSession) -> UserResponse:
    # Check for duplicate email
    existing = db.execute(
        select(User).where(User.email == payload.email)
    ).scalar_one_or_none()

    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "EMAIL_TAKEN", "message": "This email address is already registered"},
        )

    # In production: use bcrypt or argon2 to hash the password
    # import bcrypt; hashed = bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt())
    hashed_password = f"hashed_{payload.password}"  # ← REPLACE with real hashing

    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hashed_password,
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info("User CREATED id=%s email=%r", user.id, user.email)
    return UserResponse.model_validate(user)


# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Deep health check",
    description="Pings Redis, MySQL, and checks RabbitMQ connectivity via Celery.",
    tags=["Operations"],
)
def health_check(db: DbSession) -> HealthResponse:
    """
    Returns the health of every dependency.
    Status is 'healthy' only if ALL services respond.
    Otherwise 'degraded' (some services up) or 'unhealthy' (all down).
    """
    services: list[ServiceHealth] = []
    now = datetime.now(timezone.utc)

    # --- MySQL ---
    try:
        t0 = time.perf_counter()
        db.execute(text("SELECT 1"))
        latency = (time.perf_counter() - t0) * 1000
        services.append(ServiceHealth(service="mysql", healthy=True, latency_ms=round(latency, 2)))
    except Exception as e:
        services.append(ServiceHealth(service="mysql", healthy=False, detail=str(e)))

    # --- Redis ---
    try:
        t0 = time.perf_counter()
        redis_ok = lock_manager.ping()
        latency = (time.perf_counter() - t0) * 1000
        services.append(ServiceHealth(
            service="redis",
            healthy=redis_ok,
            latency_ms=round(latency, 2),
            detail=None if redis_ok else "ping failed",
        ))
    except Exception as e:
        services.append(ServiceHealth(service="redis", healthy=False, detail=str(e)))

    # --- RabbitMQ / Celery broker ---
    try:
        t0 = time.perf_counter()
        conn = celery_app.connection_for_read()
        conn.ensure_connection(max_retries=1, timeout=3)
        conn.release()
        latency = (time.perf_counter() - t0) * 1000
        services.append(ServiceHealth(service="rabbitmq", healthy=True, latency_ms=round(latency, 2)))
    except Exception as e:
        services.append(ServiceHealth(service="rabbitmq", healthy=False, detail=str(e)[:120]))

    healthy_count = sum(1 for s in services if s.healthy)
    if healthy_count == len(services):
        overall = "healthy"
    elif healthy_count > 0:
        overall = "degraded"
    else:
        overall = "unhealthy"

    return HealthResponse(
        status=overall,
        version=settings.APP_VERSION,
        timestamp=now,
        services=services,
    )


# =============================================================================
# Root
# =============================================================================

@app.get("/", include_in_schema=False)
def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "health": "/health",
    }
