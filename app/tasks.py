# =============================================================================
# app/tasks.py
# Celery task definitions — the asynchronous backbone of the booking engine.
#
# Architecture of a single booking task:
#
#   FastAPI                  RabbitMQ              Celery Worker
#   ──────                   ────────              ─────────────
#   acquire Redis lock  ──►  enqueue task   ──►   dequeue task
#   return 202 Accepted                            simulate payment (3s)
#                                                  open DB transaction
#                                                    UPDATE seat   (optimistic lock)
#                                                    INSERT booking
#                                                    UPDATE event inventory
#                                                  commit
#                                                  release Redis lock
#
# Failure modes handled:
#   - Payment failure       → rollback DB, release Redis lock, mark booking FAILED
#   - DB IntegrityError     → another worker beat us (unique constraint on seat_id)
#                             → release lock, mark FAILED (no double-booking)
#   - Optimistic lock miss  → retry up to MAX_RETRIES times with exponential backoff
#   - Unhandled exception   → Celery auto-retry (max_retries=3), then mark FAILED
# =============================================================================

import logging
import time
import uuid
from datetime import datetime, timezone

from celery import Celery, Task
from celery.utils.log import get_task_logger
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config import settings
from app.database import get_db_context
from app.models import Booking, BookingStatus, Event, Seat, SeatStatus
from app.redis_client import lock_manager

logger = get_task_logger(__name__)

# ---------------------------------------------------------------------------
# Celery application
# ---------------------------------------------------------------------------
celery_app = Celery(
    "lockdown",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    # -------------------------------------------------------------------------
    # Serialization — JSON is human-readable and safe (no pickle deserialization exploits)
    # -------------------------------------------------------------------------
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # -------------------------------------------------------------------------
    # Result backend
    # -------------------------------------------------------------------------
    result_expires=86400,          # keep results for 24 hours
    result_persistent=True,

    # -------------------------------------------------------------------------
    # Queue durability — messages survive RabbitMQ restarts
    # -------------------------------------------------------------------------
    task_default_queue="bookings",
    task_queues={
        "bookings": {
            "exchange": "bookings",
            "exchange_type": "direct",
            "routing_key": "bookings",
            "queue_arguments": {"x-queue-type": "quorum"},  # RabbitMQ quorum queue
        },
        "cancellations": {
            "exchange": "cancellations",
            "exchange_type": "direct",
            "routing_key": "cancellations",
        },
    },
    task_default_exchange="bookings",
    task_default_routing_key="bookings",

    # -------------------------------------------------------------------------
    # Reliability
    # -------------------------------------------------------------------------
    task_acks_late=True,           # ACK only after task completes (no lost messages on worker crash)
    task_reject_on_worker_lost=True,  # re-queue if worker dies mid-execution
    worker_prefetch_multiplier=1,  # each worker takes ONE task at a time (fair dispatch under load)

    # -------------------------------------------------------------------------
    # Timeouts
    # -------------------------------------------------------------------------
    task_soft_time_limit=60,       # SIGTERM at 60s — task should finish gracefully
    task_time_limit=90,            # SIGKILL at 90s — hard kill

    # -------------------------------------------------------------------------
    # Retry policy
    # -------------------------------------------------------------------------
    task_max_retries=3,
    task_default_retry_delay=5,

    # -------------------------------------------------------------------------
    # Beat schedule (optional periodic tasks)
    # -------------------------------------------------------------------------
    beat_schedule={
        "cleanup-stale-locks": {
            "task": "app.tasks.cleanup_stale_bookings",
            "schedule": 300.0,  # every 5 minutes
        }
    },

    # Timezone
    timezone="UTC",
    enable_utc=True,
)


# ---------------------------------------------------------------------------
# Base task class — adds structured logging and request tracing
# ---------------------------------------------------------------------------

class BaseBookingTask(Task):
    """Base class that adds retry-count logging and request-id propagation."""

    abstract = True

    def on_retry(self, exc, task_id, args, kwargs, einfo):
        logger.warning(
            "Task RETRY task_id=%s attempt=%s/%s exc=%s",
            task_id, self.request.retries, self.max_retries, exc,
        )
        super().on_retry(exc, task_id, args, kwargs, einfo)

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        logger.error(
            "Task FAILED task_id=%s exc=%s traceback=%s",
            task_id, exc, einfo,
        )
        super().on_failure(exc, task_id, args, kwargs, einfo)


# ---------------------------------------------------------------------------
# Payment simulation
# ---------------------------------------------------------------------------

def _simulate_payment(
    seat_id: int,
    user_id: int,
    amount: float,
    idempotency_key: str,
) -> str:
    """
    Stand-in for a real payment-gateway call (Stripe, PayPal, etc.).
    Replace this entire function body with your gateway SDK.

    Returns a payment_reference string on success.
    Raises RuntimeError on payment failure.
    """
    logger.info(
        "Payment PROCESSING seat_id=%s user_id=%s amount=%.2f idempotency_key=%s",
        seat_id, user_id, amount, idempotency_key,
    )

    # Simulate network latency / payment processor delay
    time.sleep(settings.PAYMENT_SIMULATION_DELAY)

    # In a real system: call your payment gateway here
    # stripe.PaymentIntent.create(amount=int(amount*100), currency="usd", ...)

    payment_reference = f"PAY-{uuid.uuid4().hex[:16].upper()}"
    logger.info("Payment SUCCESS reference=%s", payment_reference)
    return payment_reference


# ---------------------------------------------------------------------------
# Core booking task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    base=BaseBookingTask,
    name="app.tasks.process_booking",
    queue="bookings",
    max_retries=3,
    default_retry_delay=5,
    acks_late=True,
)
def process_booking(
    self: Task,
    seat_id: int,
    user_id: int,
    booking_id: int,
    lock_token: str,
    idempotency_key: str,
) -> dict:
    """
    The central Celery task that executes the full booking flow.

    Arguments (all JSON-serializable):
        seat_id         — ID of the seat to book
        user_id         — ID of the user making the booking
        booking_id      — Pre-created Booking row (status=PENDING)
        lock_token      — The Redis lock token acquired by FastAPI
        idempotency_key — Client-supplied unique key for safe retries

    Returns a dict with booking result details (stored in Celery backend).

    Race-condition protections in this function:
        1. Optimistic locking: UPDATE seats WHERE id=? AND version=?
           If version has changed, 0 rows affected → retry.
        2. DB UNIQUE constraint: INSERT INTO bookings with unique seat_id
           IntegrityError → another worker won → we release lock and bail.
        3. Atomic lock release: Lua script checks token before DEL.
    """
    task_id = self.request.id
    logger.info(
        "BookingTask START task_id=%s seat_id=%s user_id=%s booking_id=%s",
        task_id, seat_id, user_id, booking_id,
    )

    payment_reference: str | None = None

    try:
        # -------------------------------------------------------------------
        # PHASE 1: Simulate / execute payment
        # This happens OUTSIDE the DB transaction so that a slow payment
        # gateway doesn't hold a database row lock for 3+ seconds.
        # -------------------------------------------------------------------
        with get_db_context() as db:
            booking = db.get(Booking, booking_id)
            if booking is None:
                raise ValueError(f"Booking {booking_id} not found")

            seat = db.get(Seat, seat_id)
            if seat is None:
                raise ValueError(f"Seat {seat_id} not found")

            amount = float(seat.price)

        payment_reference = _simulate_payment(
            seat_id=seat_id,
            user_id=user_id,
            amount=amount,
            idempotency_key=idempotency_key,
        )

        # -------------------------------------------------------------------
        # PHASE 2: Commit the booking inside a DB transaction.
        #
        # We use get_db_context() (which auto-commits on success and
        # auto-rollbacks on exception) so we never leave a partial write.
        # -------------------------------------------------------------------
        with get_db_context() as db:
            # ---------------------------------------------------------------
            # Optimistic locking: read the current seat version
            # ---------------------------------------------------------------
            seat: Seat = db.execute(
                select(Seat).where(Seat.id == seat_id).with_for_update()
            ).scalar_one()

            current_version = seat.version

            # ---------------------------------------------------------------
            # Update seat status with version check.
            # If the version has been bumped by a concurrent worker, the
            # WHERE clause matches 0 rows — we catch that below and retry.
            # ---------------------------------------------------------------
            rows_updated = db.execute(
                update(Seat)
                .where(Seat.id == seat_id)
                .where(Seat.version == current_version)
                .where(Seat.status == SeatStatus.LOCKED)
                .values(
                    status=SeatStatus.BOOKED,
                    version=current_version + 1,
                )
            ).rowcount

            if rows_updated == 0:
                # Another worker updated this seat between our read and write.
                # Raise a custom exception to trigger a retry.
                raise StaleDataError(
                    f"Optimistic lock miss: seat {seat_id} version changed "
                    f"(expected {current_version})"
                )

            # ---------------------------------------------------------------
            # Update the Booking row to CONFIRMED
            # ---------------------------------------------------------------
            booking: Booking = db.get(Booking, booking_id)
            booking.status = BookingStatus.CONFIRMED
            booking.amount_paid = amount
            booking.payment_reference = payment_reference
            booking.booked_at = datetime.now(timezone.utc)
            booking.celery_task_id = task_id
            db.add(booking)

            # ---------------------------------------------------------------
            # Decrement denormalized available_seats on the Event row
            # ---------------------------------------------------------------
            db.execute(
                update(Event)
                .where(Event.id == seat.event_id)
                .where(Event.available_seats > 0)
                .values(available_seats=Event.available_seats - 1)
            )

        # -------------------------------------------------------------------
        # PHASE 3: Release the Redis lock now that DB is committed.
        # Using our Lua-script release guarantees we only delete our own lock.
        # -------------------------------------------------------------------
        lock_manager.release(seat_id=seat_id, token=lock_token)

        logger.info(
            "BookingTask SUCCESS task_id=%s seat_id=%s booking_id=%s ref=%s",
            task_id, seat_id, booking_id, payment_reference,
        )

        return {
            "status": "confirmed",
            "booking_id": booking_id,
            "seat_id": seat_id,
            "payment_reference": payment_reference,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }

    # -----------------------------------------------------------------------
    # Optimistic lock miss — retry with exponential back-off
    # -----------------------------------------------------------------------
    except StaleDataError as exc:
        logger.warning(
            "BookingTask OPTIMISTIC_LOCK_MISS task_id=%s seat_id=%s attempt=%s",
            task_id, seat_id, self.request.retries,
        )
        raise self.retry(
            exc=exc,
            countdown=2 ** self.request.retries,  # 1s, 2s, 4s
        )

    # -----------------------------------------------------------------------
    # DB unique-constraint violation — another worker already booked this seat
    # -----------------------------------------------------------------------
    except IntegrityError as exc:
        logger.error(
            "BookingTask INTEGRITY_ERROR task_id=%s seat_id=%s (double-booking prevented)",
            task_id, seat_id,
        )
        _mark_booking_failed(booking_id, "Seat already booked by another request")
        lock_manager.release(seat_id=seat_id, token=lock_token)
        return {
            "status": "failed",
            "booking_id": booking_id,
            "reason": "double_booking_prevented",
        }

    # -----------------------------------------------------------------------
    # Transient DB error (deadlock, connection drop) — retry
    # -----------------------------------------------------------------------
    except OperationalError as exc:
        logger.warning(
            "BookingTask DB_ERROR task_id=%s seat_id=%s exc=%s — retrying",
            task_id, seat_id, exc,
        )
        raise self.retry(exc=exc, countdown=5)

    # -----------------------------------------------------------------------
    # Payment failure — release lock, mark failed
    # -----------------------------------------------------------------------
    except RuntimeError as exc:
        logger.error(
            "BookingTask PAYMENT_FAILED task_id=%s seat_id=%s exc=%s",
            task_id, seat_id, exc,
        )
        _mark_booking_failed(booking_id, str(exc))
        _restore_seat_to_available(seat_id)
        lock_manager.release(seat_id=seat_id, token=lock_token)
        return {
            "status": "failed",
            "booking_id": booking_id,
            "reason": "payment_failed",
        }

    # -----------------------------------------------------------------------
    # Unexpected error — retry up to max_retries, then mark failed
    # -----------------------------------------------------------------------
    except Exception as exc:
        logger.exception(
            "BookingTask UNEXPECTED_ERROR task_id=%s seat_id=%s exc=%s",
            task_id, seat_id, exc,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=10)

        # All retries exhausted
        _mark_booking_failed(booking_id, f"Internal error: {exc}")
        _restore_seat_to_available(seat_id)
        lock_manager.release(seat_id=seat_id, token=lock_token)
        return {
            "status": "failed",
            "booking_id": booking_id,
            "reason": "internal_error",
        }


# ---------------------------------------------------------------------------
# Cancellation task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    base=BaseBookingTask,
    name="app.tasks.process_cancellation",
    queue="cancellations",
    max_retries=3,
)
def process_cancellation(self: Task, booking_id: int) -> dict:
    """
    Cancel a confirmed booking: reverse the seat to AVAILABLE and
    update booking/event records atomically.
    """
    logger.info("CancellationTask START booking_id=%s", booking_id)

    try:
        with get_db_context() as db:
            booking: Booking = db.get(Booking, booking_id)
            if booking is None:
                raise ValueError(f"Booking {booking_id} not found")

            if booking.status != BookingStatus.CONFIRMED:
                return {
                    "status": "no_action",
                    "reason": f"booking status is {booking.status}, not confirmed",
                }

            seat: Seat = db.get(Seat, booking.seat_id)

            # Mark booking as refunded
            booking.status = BookingStatus.REFUNDED
            db.add(booking)

            # Return seat to available inventory
            seat.status = SeatStatus.AVAILABLE
            seat.version += 1
            db.add(seat)

            # Restore denormalized counter
            db.execute(
                update(Event)
                .where(Event.id == seat.event_id)
                .values(available_seats=Event.available_seats + 1)
            )

        # Restore Redis inventory cache
        lock_manager.increment_inventory(seat.event_id)

        logger.info("CancellationTask SUCCESS booking_id=%s", booking_id)
        return {"status": "cancelled", "booking_id": booking_id}

    except Exception as exc:
        logger.exception("CancellationTask FAILED booking_id=%s exc=%s", booking_id, exc)
        raise self.retry(exc=exc, countdown=10)


# ---------------------------------------------------------------------------
# Periodic cleanup task — catches orphaned PENDING bookings whose locks expired
# ---------------------------------------------------------------------------

@celery_app.task(name="app.tasks.cleanup_stale_bookings")
def cleanup_stale_bookings() -> dict:
    """
    Periodic task (runs every 5 minutes via Celery Beat).

    Finds PENDING bookings where the associated Redis lock no longer exists
    (lock expired and worker never completed) and rolls them back to AVAILABLE.
    This is the safety net for edge cases like worker crashes or network partitions.
    """
    logger.info("CleanupTask START")
    cleaned = 0

    try:
        with get_db_context() as db:
            # Find all PENDING bookings
            pending_bookings = db.execute(
                select(Booking).where(Booking.status == BookingStatus.PENDING)
            ).scalars().all()

            for booking in pending_bookings:
                # If the Redis lock is gone, the TTL expired — clean up
                if not lock_manager.is_locked(booking.seat_id):
                    logger.warning(
                        "CleanupTask: stale booking found id=%s seat_id=%s",
                        booking.id, booking.seat_id,
                    )
                    booking.status = BookingStatus.FAILED
                    booking.failure_reason = "Lock expired before processing completed"
                    db.add(booking)

                    _restore_seat_to_available(booking.seat_id)
                    cleaned += 1

        logger.info("CleanupTask DONE cleaned=%s", cleaned)
        return {"cleaned": cleaned}

    except Exception as exc:
        logger.exception("CleanupTask FAILED exc=%s", exc)
        return {"cleaned": cleaned, "error": str(exc)}


# ---------------------------------------------------------------------------
# Helper functions (not Celery tasks)
# ---------------------------------------------------------------------------

def _mark_booking_failed(booking_id: int, reason: str) -> None:
    """Update a booking record to FAILED status."""
    try:
        with get_db_context() as db:
            booking = db.get(Booking, booking_id)
            if booking:
                booking.status = BookingStatus.FAILED
                booking.failure_reason = reason
                db.add(booking)
    except Exception:
        logger.exception("Failed to mark booking %s as failed", booking_id)


def _restore_seat_to_available(seat_id: int) -> None:
    """
    Reset seat status from LOCKED back to AVAILABLE on booking failure.
    Uses version increment to maintain optimistic-lock consistency.
    """
    try:
        with get_db_context() as db:
            seat = db.get(Seat, seat_id)
            if seat and seat.status in (SeatStatus.LOCKED, SeatStatus.BOOKED):
                event_id = seat.event_id
                seat.status = SeatStatus.AVAILABLE
                seat.version += 1
                db.add(seat)
            else:
                event_id = None

        # Restore Redis inventory cache
        if event_id:
            lock_manager.increment_inventory(event_id)

    except Exception:
        logger.exception("Failed to restore seat %s to available", seat_id)
