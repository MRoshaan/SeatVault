# =============================================================================
# app/schemas.py
# Pydantic v2 request/response models — the API's public contract.
#
# These are separate from SQLAlchemy models so we never accidentally leak
# internal fields (hashed passwords, version counters, etc.) to clients.
# =============================================================================

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# ---------------------------------------------------------------------------
# Common base
# ---------------------------------------------------------------------------

class OrmBase(BaseModel):
    """Enables ORM mode for all response models."""
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)


class UserResponse(OrmBase):
    id: int
    email: str
    full_name: str
    is_active: bool
    is_verified: bool
    created_at: datetime


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: int


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class EventCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=255)
    description: Optional[str] = None
    venue: str = Field(..., min_length=2, max_length=255)
    event_date: datetime
    total_seats: int = Field(..., ge=1, le=100_000)


class EventResponse(OrmBase):
    id: int
    name: str
    description: Optional[str]
    venue: str
    event_date: datetime
    total_seats: int
    available_seats: int
    is_active: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------

class SeatResponse(OrmBase):
    id: int
    event_id: int
    seat_code: str
    section: Optional[str]
    row: Optional[str]
    seat_number: Optional[str]
    price: float
    status: str


class SeatListResponse(BaseModel):
    event_id: int
    total: int
    available: int
    seats: list[SeatResponse]


# ---------------------------------------------------------------------------
# Booking — Request
# ---------------------------------------------------------------------------

class BookingRequest(BaseModel):
    """
    Body for POST /book/{seat_id}.

    idempotency_key: Client-generated UUID for safe retries.
                     If you retry after a network timeout, use the SAME key.
                     The API will return the original booking instead of
                     creating a duplicate.
    """
    idempotency_key: str = Field(
        ...,
        min_length=16,
        max_length=64,
        description="Client-generated unique key for idempotent retries (UUID recommended)",
        examples=["a1b2c3d4-e5f6-7890-abcd-ef1234567890"],
    )
    user_id: int = Field(
        ...,
        ge=1,
        description="ID of the user making the booking",
    )

    @field_validator("idempotency_key")
    @classmethod
    def sanitize_idempotency_key(cls, v: str) -> str:
        # Strip whitespace and enforce alphanumeric + hyphens only
        stripped = v.strip()
        if not all(c.isalnum() or c == "-" for c in stripped):
            raise ValueError("idempotency_key must be alphanumeric with optional hyphens")
        return stripped


# ---------------------------------------------------------------------------
# Booking — Response shapes
# ---------------------------------------------------------------------------

class BookingAcceptedResponse(BaseModel):
    """
    202 Accepted — returned immediately when a booking task is enqueued.
    The client should poll /booking/status/{task_id} for the final result.
    """
    status: str = "accepted"
    message: str
    task_id: str
    booking_id: int
    seat_id: int
    lock_ttl_seconds: int = Field(description="How long the seat is held while processing")
    poll_url: str = Field(description="URL to poll for the final booking result")


class BookingStatusResponse(BaseModel):
    """
    Response for GET /booking/status/{task_id}.
    Maps Celery task states to client-friendly statuses.
    """
    task_id: str
    celery_state: str           # RAW Celery state: PENDING, STARTED, SUCCESS, FAILURE
    status: str                 # Human-friendly: queued, processing, confirmed, failed
    booking_id: Optional[int] = None
    seat_id: Optional[int] = None
    payment_reference: Optional[str] = None
    confirmed_at: Optional[str] = None
    failure_reason: Optional[str] = None


class BookingDetailResponse(OrmBase):
    """Full booking detail for GET /booking/{booking_id}."""
    id: int
    user_id: int
    seat_id: int
    status: str
    idempotency_key: Optional[str]
    celery_task_id: Optional[str]
    amount_paid: Optional[float]
    payment_reference: Optional[str]
    failure_reason: Optional[str]
    booked_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class ServiceHealth(BaseModel):
    service: str
    healthy: bool
    latency_ms: Optional[float] = None
    detail: Optional[str] = None


class HealthResponse(BaseModel):
    status: str               # "healthy" | "degraded" | "unhealthy"
    version: str
    timestamp: datetime
    services: list[ServiceHealth]


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------

class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: Optional[str] = None


class ConflictResponse(BaseModel):
    """409 Conflict — seat is locked by another request."""
    code: str = "SEAT_LOCKED"
    message: str
    seat_id: int
    lock_ttl_seconds: int


class RateLimitResponse(BaseModel):
    """429 Too Many Requests."""
    code: str = "RATE_LIMIT_EXCEEDED"
    message: str = "Too many booking attempts. Please wait before trying again."
    retry_after_seconds: int
