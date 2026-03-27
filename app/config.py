# =============================================================================
# app/config.py
# Single source of truth for all environment-driven configuration.
# Uses pydantic-settings so every value is type-validated at startup.
# Missing required variables raise a loud ValidationError — no silent failures.
# =============================================================================

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All settings are read from environment variables (or a .env file).
    Pydantic-Settings handles casting, validation, and defaults automatically.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # -------------------------------------------------------------------------
    # Application
    # -------------------------------------------------------------------------
    APP_NAME: str = "Lockdown Flash-Sale API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # -------------------------------------------------------------------------
    # MySQL  (running natively on the host)
    # -------------------------------------------------------------------------
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = "password"
    MYSQL_DATABASE: str = "lockdown_db"

    # SQLAlchemy pool settings — tuned for high-concurrency flash sales
    DB_POOL_SIZE: int = 20          # persistent connections kept open
    DB_MAX_OVERFLOW: int = 40       # extra connections allowed under burst load
    DB_POOL_TIMEOUT: int = 30       # seconds to wait for a connection
    DB_POOL_RECYCLE: int = 1800     # recycle connections older than 30 min

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"mysql+pymysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
            f"?charset=utf8mb4"
        )

    # -------------------------------------------------------------------------
    # Redis
    # -------------------------------------------------------------------------
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None

    # Distributed lock TTL — how long a seat is "held" pending payment.
    # 5 minutes gives the payment processor enough time while preventing
    # indefinite blocking if the Celery worker crashes.
    SEAT_LOCK_TTL_SECONDS: int = 300  # 5 minutes

    # Rate limiting — max booking attempts per user per window
    RATE_LIMIT_MAX_REQUESTS: int = 10
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    @property
    def REDIS_URL(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    # -------------------------------------------------------------------------
    # RabbitMQ / Celery
    # -------------------------------------------------------------------------
    RABBITMQ_USER: str = "lockdown"
    RABBITMQ_PASS: str = "lockdown_secret"
    RABBITMQ_HOST: str = "localhost"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_VHOST: str = "lockdown_vhost"

    # Celery — separate Redis DB (db=1) for results to avoid key collisions
    CELERY_RESULT_BACKEND_DB: int = 1

    @property
    def CELERY_BROKER_URL(self) -> str:
        return (
            f"amqp://{self.RABBITMQ_USER}:{self.RABBITMQ_PASS}"
            f"@{self.RABBITMQ_HOST}:{self.RABBITMQ_PORT}/{self.RABBITMQ_VHOST}"
        )

    @property
    def CELERY_RESULT_BACKEND(self) -> str:
        if self.REDIS_PASSWORD:
            return (
                f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}"
                f":{self.REDIS_PORT}/{self.CELERY_RESULT_BACKEND_DB}"
            )
        return (
            f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}"
            f"/{self.CELERY_RESULT_BACKEND_DB}"
        )

    # Payment simulation delay (seconds) — replace with real gateway call
    PAYMENT_SIMULATION_DELAY: int = 3

    # -------------------------------------------------------------------------
    # JWT authentication
    # -------------------------------------------------------------------------
    JWT_SECRET_KEY: str = "change_me_in_env"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60


@lru_cache
def get_settings() -> Settings:
    """
    Returns a cached singleton Settings instance.
    The @lru_cache means the .env file is only read once per process,
    which is exactly what we want for a high-throughput API.
    """
    return Settings()


# Convenience alias so other modules can do:
#   from app.config import settings
settings = get_settings()
