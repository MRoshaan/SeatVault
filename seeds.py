import datetime
from sqlalchemy.orm import Session
from app.database import engine, Base
from app.models import Event, Seat, SeatStatus
from app.redis_client import lock_manager

# 1. Nuke and Rebuild Tables to reset all IDs back to 1
print("Wiping slate clean and resetting tables...")
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

db = Session(engine)

try:
    # 2. Create the Main Event (Forcing ID to be exactly 1)
    print("Creating Event #1...")
    event = Event(
        id=1,  # <-- Forcing ID 1
        name="VIP Stage Concert",
        description="The ultimate comeback show.",
        venue="Main Arena",
        event_date=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
        total_seats=100,
        available_seats=100,
        is_active=True
    )
    db.add(event)
    db.commit()

    # 3. Generate 100 Seats (Forcing IDs 1-100)
    print("Generating 100 perfectly aligned seats...")
    for i in range(1, 101):
        if i <= 30:
            seat_price = 120.00
        elif i <= 70:
            seat_price = 200.00
        else:
            seat_price = 60.00

        seat = Seat(
            id=i,  # <-- Forcing Seat ID to match the frontend perfectly
            event_id=1,
            seat_code=f"SEAT-{i}",
            status=SeatStatus.AVAILABLE,
            price=seat_price
        )
        db.add(seat)
    
    db.commit()

    # 4. Seed Redis
    lock_manager.seed_inventory(1, 100)

    print("✅ SUCCESS: Database completely reset and perfectly seeded!")

except Exception as e:
    db.rollback()
    print(f"❌ Error seeding database: {e}")
finally:
    db.close()