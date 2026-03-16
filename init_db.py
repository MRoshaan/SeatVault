#!/usr/bin/env python3
# =============================================================================
# init_db.py
# One-shot script to create the database schema and seed sample data.
#
# Run once before starting the API:
#   python init_db.py
#
# It is idempotent: running it multiple times will not duplicate data.
# In production, replace this with Alembic migration scripts.
# =============================================================================

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger(__name__)


def create_tables() -> None:
    from app.database import Base, engine
    logger.info("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("Tables created (or already exist).")


def seed_sample_data() -> None:
    from decimal import Decimal

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Event, Seat, SeatStatus, User
    from app.redis_client import lock_manager

    db = SessionLocal()

    try:
        # ---------------------------------------------------------------
        # Create a sample user (skip if already exists)
        # ---------------------------------------------------------------
        existing_user = db.execute(
            select(User).where(User.email == "alice@example.com")
        ).scalar_one_or_none()

        if existing_user is None:
            user = User(
                email="alice@example.com",
                full_name="Alice Wonderland",
                hashed_password="hashed_supersecret123",  # use bcrypt in production
                is_active=True,
                is_verified=True,
            )
            db.add(user)
            db.flush()  # get the user.id before committing
            logger.info("Created sample user: alice@example.com (id=%s)", user.id)
        else:
            user = existing_user
            logger.info("Sample user already exists (id=%s)", user.id)

        # ---------------------------------------------------------------
        # Create a sample event (skip if already exists)
        # ---------------------------------------------------------------
        from datetime import datetime, timedelta, timezone

        existing_event = db.execute(
            select(Event).where(Event.name == "Lockdown Music Festival 2026")
        ).scalar_one_or_none()

        if existing_event is None:
            event_date = datetime.now(timezone.utc) + timedelta(days=30)
            event = Event(
                name="Lockdown Music Festival 2026",
                description="The most anticipated flash-sale concert of the year.",
                venue="Madison Square Garden, New York",
                event_date=event_date,
                total_seats=100,
                available_seats=100,
                is_active=True,
            )
            db.add(event)
            db.flush()
            logger.info("Created sample event: id=%s seats=100", event.id)
        else:
            event = existing_event
            logger.info("Sample event already exists (id=%s)", event.id)

        # ---------------------------------------------------------------
        # Create seats for the event (skip if already exist)
        # ---------------------------------------------------------------
        existing_seats_count = db.execute(
            select(Seat).where(Seat.event_id == event.id)
        ).scalars().all()

        if not existing_seats_count:
            sections = [
                ("VIP",     10,  Decimal("250.00")),
                ("A",       30,  Decimal("120.00")),
                ("B",       30,  Decimal("80.00")),
                ("GA",      30,  Decimal("45.00")),
            ]

            seat_num = 1
            for section, count, price in sections:
                for i in range(1, count + 1):
                    seat_code = f"{section}-{i:03d}"
                    seat = Seat(
                        event_id=event.id,
                        seat_code=seat_code,
                        section=section,
                        row=None,
                        seat_number=str(i),
                        price=price,
                        status=SeatStatus.AVAILABLE,
                        version=0,
                    )
                    db.add(seat)
                    seat_num += 1

            logger.info("Created %s seats for event id=%s", seat_num - 1, event.id)
        else:
            logger.info(
                "Seats already exist for event id=%s (%s seats)",
                event.id, len(existing_seats_count),
            )

        db.commit()

        # ---------------------------------------------------------------
        # Seed Redis inventory cache
        # ---------------------------------------------------------------
        available_count = db.execute(
            select(Seat).where(
                Seat.event_id == event.id,
                Seat.status == SeatStatus.AVAILABLE,
            )
        ).scalars().all()

        lock_manager.seed_inventory(event.id, len(available_count))
        logger.info(
            "Redis inventory seeded: event_id=%s count=%s",
            event.id, len(available_count),
        )

        # ---------------------------------------------------------------
        # Print a quick summary
        # ---------------------------------------------------------------
        print("\n" + "=" * 60)
        print(" Lockdown DB Initialized Successfully!")
        print("=" * 60)
        print(f"  User:   alice@example.com  (id={user.id})")
        print(f"  Event:  '{event.name}' (id={event.id})")
        print(f"  Seats:  100 seats created")
        print(f"  Redis:  inventory cache seeded")
        print("=" * 60)
        print("\n  Start the API:    uvicorn app.main:app --reload")
        print("  Start worker:     celery -A app.tasks.celery_app worker --loglevel=info")
        print("  Start beat:       celery -A app.tasks.celery_app beat --loglevel=info")
        print("  API Docs:         http://localhost:8000/docs")
        print("  RabbitMQ UI:      http://localhost:15672  (lockdown / lockdown_secret)")
        print()

    except Exception:
        db.rollback()
        logger.exception("Seeding failed — database rolled back")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    try:
        create_tables()
        seed_sample_data()
    except Exception as e:
        logger.error("Initialization failed: %s", e)
        sys.exit(1)
