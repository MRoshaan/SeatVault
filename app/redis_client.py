# =============================================================================
# app/redis_client.py
# Distributed lock manager and rate-limiter backed by Redis.
#
# WHY LUA SCRIPTS?
# ────────────────
# A naive Python sequence like:
#   value = redis.GET(key)
#   if value == my_token: redis.DEL(key)
# has a TOCTOU (time-of-check / time-of-use) race: another process could
# delete the key between our GET and DEL.  By running the check-and-delete
# as a single atomic Lua script we eliminate that window entirely.
# Redis guarantees that Lua scripts execute atomically — no other command
# can interleave while the script is running.
#
# WHY A UNIQUE TOKEN PER LOCK?
# ─────────────────────────────
# Consider:
#   1. Worker A acquires lock with TTL=300s.
#   2. Worker A stalls (GC pause, slow DB) for >300s.
#   3. Lock expires.  Worker B acquires the same lock.
#   4. Worker A resumes and calls "release lock".
# Without a unique token, step 4 would delete Worker B's lock — a disaster.
# With a token, our Lua script checks the value matches before deleting,
# so Worker A's stale release is a safe no-op.
# =============================================================================

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

import redis
from redis import Redis

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis connection pool — shared across all threads in the process
# ---------------------------------------------------------------------------
_pool = redis.ConnectionPool.from_url(
    settings.REDIS_URL,
    max_connections=50,
    decode_responses=True,   # keys/values come back as str, not bytes
    socket_timeout=5,
    socket_connect_timeout=5,
    retry_on_timeout=True,
)


def get_redis() -> Redis:
    """Return a Redis client using the shared connection pool."""
    return Redis(connection_pool=_pool)


# ---------------------------------------------------------------------------
# Lua scripts — loaded once, executed atomically on the server
# ---------------------------------------------------------------------------

# RELEASE LOCK — only deletes the key if the stored value matches our token.
# Returns 1 if deleted, 0 if the token did not match (i.e. lock expired and
# was re-acquired by someone else — do NOT delete).
_LUA_RELEASE_LOCK = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""

# SLIDING-WINDOW RATE LIMIT — counts events inside a rolling time window.
# Returns the current count after incrementing. The caller checks against
# the configured max_requests threshold.
_LUA_RATE_LIMIT = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])

-- Remove events that have fallen outside the window
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window * 1000)

-- Count remaining events in the window
local count = redis.call('ZCARD', key)

if count < limit then
    -- Add this event with the current timestamp as both score and member
    redis.call('ZADD', key, now, now .. ':' .. math.random())
    -- Set the key to expire after the window so it self-cleans
    redis.call('PEXPIRE', key, window * 1000)
    return count + 1
else
    return -1
end
"""


# ---------------------------------------------------------------------------
# Lock result dataclass
# ---------------------------------------------------------------------------

@dataclass
class LockResult:
    acquired: bool
    token: Optional[str] = field(default=None)
    key: Optional[str] = field(default=None)

    @property
    def failed(self) -> bool:
        return not self.acquired


# ---------------------------------------------------------------------------
# DistributedLockManager
# ---------------------------------------------------------------------------

class DistributedLockManager:
    """
    Manages distributed seat locks using Redis SETNX (SET NX PX).

    Lock lifecycle:
      acquire() → Redis SET key token NX PX <ttl_ms>
                   returns LockResult(acquired=True, token=<uuid>)
      release() → Lua script: if GET key == token: DEL key
                   returns True if the lock was ours and was deleted
    """

    LOCK_KEY_PREFIX = "seat_lock:"
    INVENTORY_KEY_PREFIX = "seat_inventory:"

    def __init__(self, redis_client: Optional[Redis] = None):
        self._redis = redis_client or get_redis()
        self._release_script = self._redis.register_script(_LUA_RELEASE_LOCK)
        self._rate_limit_script = self._redis.register_script(_LUA_RATE_LIMIT)

    # -----------------------------------------------------------------------
    # Lock operations
    # -----------------------------------------------------------------------

    def acquire(self, seat_id: int, ttl_seconds: Optional[int] = None) -> LockResult:
        """
        Attempt to acquire a distributed lock for the given seat.

        Uses SET ... NX PX which is atomic in a single Redis command —
        no pipeline or Lua script needed here.

        Returns:
            LockResult with acquired=True and a unique token if successful.
            LockResult with acquired=False if the seat is already locked.
        """
        ttl = ttl_seconds or settings.SEAT_LOCK_TTL_SECONDS
        key = f"{self.LOCK_KEY_PREFIX}{seat_id}"
        token = str(uuid.uuid4())

        # SET key value NX PX milliseconds
        # NX  — only set if key does NOT exist (atomic check-and-set)
        # PX  — TTL in milliseconds
        acquired = self._redis.set(
            key,
            token,
            nx=True,
            px=ttl * 1000,
        )

        if acquired:
            logger.info("Lock ACQUIRED seat_id=%s token=%s ttl=%ss", seat_id, token, ttl)
            return LockResult(acquired=True, token=token, key=key)

        logger.info("Lock REJECTED seat_id=%s (already locked)", seat_id)
        return LockResult(acquired=False)

    def release(self, seat_id: int, token: str) -> bool:
        """
        Release the lock ONLY if the stored token matches ours.
        Uses a Lua script to ensure the check-and-delete is atomic.

        Returns True if the lock was released, False if it had already
        expired or been acquired by a different caller.
        """
        key = f"{self.LOCK_KEY_PREFIX}{seat_id}"
        result = self._release_script(keys=[key], args=[token])
        released = bool(result)

        if released:
            logger.info("Lock RELEASED seat_id=%s token=%s", seat_id, token)
        else:
            logger.warning(
                "Lock RELEASE SKIPPED seat_id=%s token=%s (expired or stolen)",
                seat_id, token,
            )
        return released

    def extend(self, seat_id: int, token: str, ttl_seconds: int) -> bool:
        """
        Extend the TTL of an existing lock (only if we still own it).
        Useful for long-running payment processors that need more time.
        """
        key = f"{self.LOCK_KEY_PREFIX}{seat_id}"
        current_token = self._redis.get(key)

        if current_token != token:
            logger.warning("Lock EXTEND DENIED seat_id=%s (token mismatch)", seat_id)
            return False

        self._redis.pexpire(key, ttl_seconds * 1000)
        logger.info("Lock EXTENDED seat_id=%s new_ttl=%ss", seat_id, ttl_seconds)
        return True

    def is_locked(self, seat_id: int) -> bool:
        """Fast check — does a lock currently exist for this seat?"""
        key = f"{self.LOCK_KEY_PREFIX}{seat_id}"
        return self._redis.exists(key) == 1

    def get_lock_ttl(self, seat_id: int) -> int:
        """Return remaining TTL in seconds, or -2 if key does not exist."""
        key = f"{self.LOCK_KEY_PREFIX}{seat_id}"
        return self._redis.ttl(key)

    # -----------------------------------------------------------------------
    # Rate limiting (sliding window)
    # -----------------------------------------------------------------------

    def check_rate_limit(
        self,
        user_id: int,
        max_requests: Optional[int] = None,
        window_seconds: Optional[int] = None,
    ) -> tuple[bool, int]:
        """
        Sliding-window rate limiter.

        Returns:
            (allowed: bool, current_count: int)

        How it works:
          - Each user gets a Redis sorted set keyed by user_id.
          - Each attempt is scored by its millisecond timestamp.
          - The Lua script atomically removes old entries, counts current
            ones, and conditionally adds the new attempt — all in one
            round-trip with no race conditions.
        """
        max_req = max_requests or settings.RATE_LIMIT_MAX_REQUESTS
        window  = window_seconds or settings.RATE_LIMIT_WINDOW_SECONDS

        import time
        now_ms = int(time.time() * 1000)
        key = f"rate_limit:user:{user_id}"

        result = self._rate_limit_script(
            keys=[key],
            args=[now_ms, window, max_req],
        )

        if result == -1:
            logger.warning("Rate limit EXCEEDED user_id=%s", user_id)
            return False, max_req

        return True, int(result)

    # -----------------------------------------------------------------------
    # Inventory cache (fast-path rejection before acquiring a lock)
    # -----------------------------------------------------------------------

    def get_available_inventory(self, event_id: int) -> Optional[int]:
        """
        Read the cached available seat count for an event.
        Returns None if the cache has not been populated yet.
        """
        key = f"{self.INVENTORY_KEY_PREFIX}{event_id}"
        value = self._redis.get(key)
        return int(value) if value is not None else None

    def decrement_inventory(self, event_id: int) -> int:
        """
        Atomically decrement the cached inventory counter.
        Returns the new count. If it goes negative, the caller should
        reject the request and re-seed from the DB.
        """
        key = f"{self.INVENTORY_KEY_PREFIX}{event_id}"
        return self._redis.decr(key)

    def increment_inventory(self, event_id: int) -> int:
        """Restore inventory on booking failure or cancellation."""
        key = f"{self.INVENTORY_KEY_PREFIX}{event_id}"
        return self._redis.incr(key)

    def seed_inventory(self, event_id: int, count: int, ttl_seconds: int = 3600) -> None:
        """Populate the inventory cache from a fresh DB read."""
        key = f"{self.INVENTORY_KEY_PREFIX}{event_id}"
        self._redis.set(key, count, ex=ttl_seconds)
        logger.info("Inventory SEEDED event_id=%s count=%s", event_id, count)

    # -----------------------------------------------------------------------
    # Health check
    # -----------------------------------------------------------------------

    def ping(self) -> bool:
        """Returns True if Redis responds to PING."""
        try:
            return self._redis.ping()
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ---------------------------------------------------------------------------
lock_manager = DistributedLockManager()
