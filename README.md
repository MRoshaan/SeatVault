# Lockdown — Enterprise Flash-Sale & Ticketing API

A production-grade, high-concurrency REST API for flash sales and event ticketing, engineered to handle thousands of simultaneous booking requests without race conditions or double-bookings.

---

## Architecture Overview

```
                        ┌─────────────────────────────────────────┐
                        │              FastAPI (API Gateway)       │
                        │                                         │
  Client Request ──────►│  1. Rate limit check  (Redis)           │
  POST /book/{seat_id}  │  2. Idempotency check (MySQL)           │
                        │  3. SETNX distributed lock (Redis) ─────┼──► 409 Conflict
                        │  4. Create PENDING booking (MySQL)       │
                        │  5. Dispatch task (RabbitMQ)             │
                        │  6. Return 202 Accepted immediately ◄────┘
                        └───────────────────┬─────────────────────┘
                                            │  task message
                                            ▼
                        ┌─────────────────────────────────────────┐
                        │          RabbitMQ (Durable Queue)        │
                        └───────────────────┬─────────────────────┘
                                            │
                                            ▼
                        ┌─────────────────────────────────────────┐
                        │           Celery Worker                  │
                        │                                         │
                        │  1. Simulate / call payment gateway      │
                        │  2. UPDATE seat   (optimistic lock)      │
                        │  3. INSERT booking (UNIQUE constraint)   │
                        │  4. UPDATE event inventory               │
                        │  5. COMMIT transaction                   │
                        │  6. Release Redis lock (Lua script)      │
                        └─────────────────────────────────────────┘
```

### Tech Stack

| Layer | Technology |
|---|---|
| Web Framework | FastAPI 0.111 |
| Database | MySQL 8 via SQLAlchemy 2.0 |
| Caching / Locks | Redis 7 |
| Message Broker | RabbitMQ 3.12 |
| Background Workers | Celery 5.4 |
| Infrastructure | Docker Compose |
| Settings | Pydantic-Settings |

---

## Race-Condition Protection: 6 Layered Defences

The system applies six independent guards. An attacker or a network burst must defeat **all six simultaneously** to cause a double-booking — which is cryptographically impossible.

```
Layer 1 — Redis SETNX Distributed Lock
  Atomic SET NX PX: only one request per seat_id wins. All others get 409 instantly.
  → app/redis_client.py : DistributedLockManager.acquire()

Layer 2 — Lua-Script Atomic Lock Release
  Lock is released only if the stored token matches the caller's token.
  A crashed/slow worker cannot accidentally unlock another worker's lock.
  → app/redis_client.py : _LUA_RELEASE_LOCK script

Layer 3 — Client Idempotency Keys
  Client supplies a UUID per booking attempt. Repeated retries with the same
  key return the original booking — never a duplicate charge.
  → app/main.py : POST /book/{seat_id}, idempotency_key check

Layer 4 — Redis Inventory Fast-Path
  Cached available-seat counter. If count ≤ 0, reject before even acquiring a lock.
  → app/redis_client.py : get_available_inventory() / decrement_inventory()

Layer 5 — Optimistic Locking (version counter)
  UPDATE seats WHERE id=? AND version=N. Zero rows affected → version changed
  → a concurrent worker won → Celery retries with exponential backoff.
  → app/tasks.py : process_booking(), rows_updated == 0 check

Layer 6 — Database UNIQUE Constraint
  bookings.seat_id has a UNIQUE index. Even if two workers race past all
  previous layers, only one INSERT succeeds. The loser gets IntegrityError → 409.
  → app/models.py : UniqueConstraint("seat_id", name="uq_booking_seat")
```

---

## Project Structure

```
lockdown/
├── docker-compose.yml          # Redis + RabbitMQ containers
├── requirements.txt            # Pinned Python dependencies
├── .env.example                # Environment variable template
├── init_db.py                  # One-shot DB schema + seed script
└── app/
    ├── __init__.py
    ├── config.py               # Pydantic-Settings (single source of truth)
    ├── database.py             # SQLAlchemy engine, pool, session factory
    ├── models.py               # User, Event, Seat, Booking ORM models
    ├── schemas.py              # Pydantic request / response contracts
    ├── dependencies.py         # FastAPI dependency injection providers
    ├── redis_client.py         # Distributed lock manager + rate limiter
    ├── tasks.py                # Celery tasks: book, cancel, cleanup
    └── main.py                 # FastAPI app + all API endpoints
```

---

## Prerequisites

- Python 3.11+
- MySQL 8 running natively (any host/port)
- Docker & Docker Compose (for Redis and RabbitMQ)
- Git

---

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/<your-username>/lockdown.git
cd lockdown
```

### 2. Start infrastructure (Redis + RabbitMQ)

```bash
docker compose up -d
```

Verify containers are healthy:

```bash
docker compose ps
```

### 3. Create a virtual environment and install dependencies

```bash
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and set at minimum:

```env
MYSQL_PASSWORD=your_actual_password
MYSQL_DATABASE=lockdown_db
```

> **Security note:** `.env` is in `.gitignore` and will never be committed.

### 5. Create the MySQL database

```sql
-- run in your MySQL client
CREATE DATABASE lockdown_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

### 6. Initialize the schema and seed sample data

```bash
python init_db.py
```

This creates all tables and seeds:
- 1 sample user (`alice@example.com`)
- 1 sample event (100 seats across VIP / A / B / GA sections)
- Redis inventory cache for the event

---

## Running the Application

Open **three separate terminals** (all with the virtual environment activated):

**Terminal 1 — FastAPI server**
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Terminal 2 — Celery worker**
```bash
celery -A app.tasks.celery_app worker --loglevel=info -Q bookings,cancellations --concurrency=4
```

**Terminal 3 — Celery Beat (periodic cleanup)**
```bash
celery -A app.tasks.celery_app beat --loglevel=info
```

---

## API Reference

### Base URL: `http://localhost:8000`

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/book/{seat_id}` | Acquire lock + enqueue booking task → **202** |
| `GET` | `/booking/status/{task_id}` | Poll Celery task result |
| `GET` | `/booking/{booking_id}` | Retrieve committed booking record |
| `DELETE` | `/booking/{booking_id}/cancel` | Cancel a confirmed booking |
| `GET` | `/events` | List all active events |
| `POST` | `/events` | Create a new event (admin) |
| `GET` | `/events/{event_id}/seats` | List seats with live status |
| `POST` | `/users` | Register a new user |
| `GET` | `/health` | Deep health check (MySQL + Redis + RabbitMQ) |

### Interactive Docs

| URL | Tool |
|---|---|
| `http://localhost:8000/docs` | Swagger UI |
| `http://localhost:8000/redoc` | ReDoc |

---

## Example: Full Booking Flow

**Step 1 — Book a seat**

```bash
curl -X POST http://localhost:8000/book/1 \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 1,
    "idempotency_key": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
  }'
```

Response `202 Accepted`:
```json
{
  "status": "accepted",
  "message": "Booking for seat 1 is being processed. Poll the status URL to confirm.",
  "task_id": "9f8e7d6c-...",
  "booking_id": 1,
  "seat_id": 1,
  "lock_ttl_seconds": 300,
  "poll_url": "/booking/status/9f8e7d6c-..."
}
```

**Step 2 — Poll for result** (after ~3 seconds)

```bash
curl http://localhost:8000/booking/status/9f8e7d6c-...
```

Response when confirmed:
```json
{
  "task_id": "9f8e7d6c-...",
  "celery_state": "SUCCESS",
  "status": "confirmed",
  "booking_id": 1,
  "seat_id": 1,
  "payment_reference": "PAY-A1B2C3D4E5F6G7H8",
  "confirmed_at": "2026-03-17T14:32:10.123456+00:00"
}
```

**Step 3 — Concurrent request on the same seat returns 409**

```bash
curl -X POST http://localhost:8000/book/1 \
  -H "Content-Type: application/json" \
  -d '{"user_id": 2, "idempotency_key": "different-key-here-xyz"}'
```

Response `409 Conflict`:
```json
{
  "code": "SEAT_LOCKED",
  "message": "Seat 1 is currently being processed by another request",
  "seat_id": 1,
  "lock_ttl_seconds": 297
}
```

---

## Observability

### RabbitMQ Management UI

```
URL:      http://localhost:15672
Username: lockdown
Password: lockdown_secret
```

Monitor queue depths, message rates, and consumer counts in real time.

### Health Check

```bash
curl http://localhost:8000/health | python -m json.tool
```

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "timestamp": "2026-03-17T14:30:00Z",
  "services": [
    {"service": "mysql",    "healthy": true, "latency_ms": 1.23},
    {"service": "redis",    "healthy": true, "latency_ms": 0.45},
    {"service": "rabbitmq", "healthy": true, "latency_ms": 2.10}
  ]
}
```

### Structured request logging

Every request logs: method, path, status code, latency (ms), and a unique `X-Request-ID` for distributed tracing.

```
2026-03-17 14:32:07 | INFO     | app.main | REQUEST method=POST path=/book/1 status=202 latency_ms=4.21 request_id=abc123
```

---

## Configuration Reference

All settings live in `.env`. Full reference in `.env.example`.

| Variable | Default | Description |
|---|---|---|
| `MYSQL_HOST` | `localhost` | MySQL host |
| `MYSQL_PORT` | `3306` | MySQL port |
| `MYSQL_DATABASE` | `lockdown_db` | Database name |
| `REDIS_HOST` | `localhost` | Redis host |
| `SEAT_LOCK_TTL_SECONDS` | `300` | How long a seat is held during payment (seconds) |
| `RATE_LIMIT_MAX_REQUESTS` | `10` | Max booking attempts per user |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate-limit rolling window (seconds) |
| `PAYMENT_SIMULATION_DELAY` | `3` | Payment delay in seconds (replace with real gateway) |
| `DB_POOL_SIZE` | `20` | SQLAlchemy persistent connection pool size |
| `DB_MAX_OVERFLOW` | `40` | Extra connections allowed under burst load |

---

## Production Checklist

Before deploying to production, address these items:

- [ ] Replace `hashed_password = f"hashed_{payload.password}"` in `main.py` with `bcrypt` or `argon2`
- [ ] Replace `_simulate_payment()` in `tasks.py` with your real payment gateway SDK
- [ ] Add JWT authentication (replace `user_id` in request body with a decoded JWT claim)
- [ ] Set `DEBUG=false` and tighten CORS `allow_origins` to your frontend domain
- [ ] Replace `Base.metadata.create_all()` in lifespan with Alembic migrations
- [ ] Set a strong `REDIS_PASSWORD` and configure RabbitMQ TLS
- [ ] Run Uvicorn behind a reverse proxy (nginx or Caddy) with TLS termination
- [ ] Set `RABBITMQ_DEFAULT_PASS` to a strong password in `.env`

---

## License

MIT
