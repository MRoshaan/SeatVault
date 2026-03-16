# =============================================================================
# app/models.py
# SQLAlchemy ORM models for the Lockdown flash-sale system.
#
# Tables:
#   users    — registered platform users
#   events   — a concert, sports game, or flash-sale event
#   seats    — individual seat/ticket slots inside an event
#   bookings — the confirmed assignment of a seat to a user
#
# Race-condition protections baked into the schema:
#   1. seats.status uses a DB-level ENUM so invalid states are impossible.
#   2. seats.version is an optimistic-lock counter — a stale concurrent
#      UPDATE will affect 0 rows and can be detected & retried.
#   3. bookings has a UNIQUE constraint on seat_id so the database itself
#      is the final arbiter: even if two workers somehow slip past Redis
#      at the exact same instant, only one INSERT will succeed.
# =============================================================================

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class SeatStatus(str, enum.Enum):
    """
    Lifecycle of a single seat.

    AVAILABLE  → LOCKED (Redis distributed lock acquired, Celery task enqueued)
    LOCKED     → BOOKED (Celery worker committed the DB transaction)
    LOCKED     → AVAILABLE (Celery worker rolled back / lock expired)
    BOOKED     → CANCELLED (admin or user cancellation flow)
    """
    AVAILABLE = "available"
    LOCKED    = "locked"      # held by Redis lock, pending payment
    BOOKED    = "booked"      # payment confirmed, committed to DB
    CANCELLED = "cancelled"   # released back to inventory


class BookingStatus(str, enum.Enum):
    PENDING   = "pending"     # Celery task accepted, processing
    CONFIRMED = "confirmed"   # payment succeeded, seat committed
    FAILED    = "failed"      # payment failed or task error
    REFUNDED  = "refunded"    # confirmed but later refunded


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    bookings: Mapped[list["Booking"]] = relationship("Booking", back_populates="user")

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"


# ---------------------------------------------------------------------------
# Events  (a concert, sports game, product drop, etc.)
# ---------------------------------------------------------------------------

class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    venue: Mapped[str] = mapped_column(String(255), nullable=False)
    event_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    # Total capacity is the count of Seat rows; this column is a fast-path
    # denormalized cache so we can answer "is this event sold out?" without
    # a COUNT(*) join on every request.
    total_seats: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_seats: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    seats: Mapped[list["Seat"]] = relationship("Seat", back_populates="event")

    def __repr__(self) -> str:
        return f"<Event id={self.id} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Seats  (individual ticket slots)
# ---------------------------------------------------------------------------

class Seat(Base):
    __tablename__ = "seats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )

    # Human-readable seat identifier, e.g. "A-12", "VIP-3", "GA-0042"
    seat_code: Mapped[str] = mapped_column(String(50), nullable=False)

    section: Mapped[str | None] = mapped_column(String(100), nullable=True)
    row: Mapped[str | None] = mapped_column(String(10), nullable=True)
    seat_number: Mapped[str | None] = mapped_column(String(10), nullable=True)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    # -----------------------------------------------------------------------
    # Race-condition protection #1 — DB-enforced ENUM
    # The database rejects any UPDATE that tries to set an invalid status,
    # providing a hard constraint beneath the application layer.
    # -----------------------------------------------------------------------
    status: Mapped[SeatStatus] = mapped_column(
        Enum(SeatStatus),
        default=SeatStatus.AVAILABLE,
        nullable=False,
        index=True,
    )

    # -----------------------------------------------------------------------
    # Race-condition protection #2 — Optimistic Locking via version counter
    # Every UPDATE to a seat must include WHERE version = <current_version>.
    # If two workers both read version=5 and both try to UPDATE, only the
    # first write wins (version becomes 6); the second affects 0 rows and
    # must retry. This catches the narrow window where Redis lock expiry
    # and a slow worker overlap.
    # -----------------------------------------------------------------------
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    event: Mapped["Event"] = relationship("Event", back_populates="seats")
    booking: Mapped["Booking | None"] = relationship(
        "Booking", back_populates="seat", uselist=False
    )

    __table_args__ = (
        # Compound unique: one seat_code per event
        UniqueConstraint("event_id", "seat_code", name="uq_event_seat_code"),
        # Covering index for the most common hot query: available seats for an event
        Index("ix_seats_event_status", "event_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Seat id={self.id} code={self.seat_code!r} status={self.status}>"


# ---------------------------------------------------------------------------
# Bookings  (the permanent, committed record of a sale)
# ---------------------------------------------------------------------------

class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    seat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("seats.id", ondelete="RESTRICT"), nullable=False
    )

    # -----------------------------------------------------------------------
    # Race-condition protection #3 — Database-level UNIQUE constraint
    # Even if two Celery workers race past Redis and both attempt to INSERT
    # a booking for the same seat, MySQL's UNIQUE index guarantees exactly
    # one will succeed; the loser gets an IntegrityError which we catch and
    # convert to a clean 409 response.
    # -----------------------------------------------------------------------
    __table_args__ = (
        UniqueConstraint("seat_id", name="uq_booking_seat"),
        Index("ix_bookings_user_status", "user_id", "status"),
    )

    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus),
        default=BookingStatus.PENDING,
        nullable=False,
        index=True,
    )

    # -----------------------------------------------------------------------
    # Idempotency key — clients generate a UUID per booking attempt.
    # If a network retry sends the same key twice, we return the original
    # booking instead of creating a duplicate charge.
    # -----------------------------------------------------------------------
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )

    # Celery task ID — used to poll job status via GET /booking/status/{task_id}
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    amount_paid: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    booked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="bookings")
    seat: Mapped["Seat"] = relationship("Seat", back_populates="booking")

    def __repr__(self) -> str:
        return f"<Booking id={self.id} seat_id={self.seat_id} status={self.status}>"
