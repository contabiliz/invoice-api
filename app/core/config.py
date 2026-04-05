from typing import Literal

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "invoice-api"
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"

    # Database — connect via PgBouncer (port 6543, transaction pooling)
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:6543/postgres"
    DATABASE_DIRECT_URL: str | None = None
    DB_POOL_MIN_SIZE: int = 5
    DB_POOL_MAX_SIZE: int = 20
    # Set DB_SSL=false for local Supabase (no SSL on the local pooler)
    DB_SSL: bool = True

    # Redis
    REDIS_URL: str = "redis://localhost:6379"
    QUEUE_HIGH_WATER_MARK: int = 10_000
    UPLOADS_CHANNEL: str = "uploads.confirmed"

    # Supabase
    SUPABASE_URL: str = ""
    SUPABASE_PUBLIC_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_STORAGE_BUCKET: str = "invoices"

    @computed_field
    @property
    def SUPABASE_JWKS_URL(self) -> str:
        return f"{self.SUPABASE_URL}/auth/v1/.well-known/jwks.json"

    # CORS — never use "*"
    FRONTEND_URL: str = "http://localhost:3000"

    # Rate limiting: POST /batches/presign
    RATE_LIMIT_PRESIGN_PER_MINUTE: int = 10

    # Presigned URL TTL (seconds)
    PRESIGN_EXPIRY_SECONDS: int = 900  # 15 minutes
    VIEW_URL_EXPIRY_SECONDS: int = 300  # 5 minutes

    @computed_field
    @property
    def all_cors_origins(self) -> list[str]:
        return [self.FRONTEND_URL]


settings = Settings()
