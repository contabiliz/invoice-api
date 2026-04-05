"""
Invoice API — FastAPI application entry point.

Lifespan:
  - Creates asyncpg connection pool (PostgreSQL via PgBouncer).
  - Creates redis.asyncio client (Redis).
  - Creates httpx.AsyncClient (Supabase Storage).
  - Instantiates adapters and binds them to app.state.
  - Closes all connections on shutdown.

Security:
  - CORS restricted to FRONTEND_URL — never "*".
  - Global exception handler: 500s return a generic message; full
    traceback is logged internally, never exposed to the client.
"""

import traceback
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.middleware.cors import CORSMiddleware

from app.adapters.cache import RedisAdapter
from app.adapters.database import AsyncpgDatabaseAdapter
from app.adapters.storage import SupabaseStorageAdapter
from app.adapters.host_transport import RewriteHostTransport
from app.api.main import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.security import JwksVerifier

configure_logging(json_logs=settings.ENVIRONMENT != "local")

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # --- startup ---
    logger.info("startup.begin", environment=settings.ENVIRONMENT)

    db_pool: asyncpg.Pool = await asyncpg.create_pool(
        settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN_SIZE,
        max_size=settings.DB_POOL_MAX_SIZE,
        ssl=settings.DB_SSL,
    )

    redis_client: Redis = Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
    )

    storage_http = httpx.AsyncClient(
        timeout=10.0,
        # Transport layer for self-hosted supabase.
        transport=RewriteHostTransport(
            wrapped=httpx.AsyncHTTPTransport(),
            public_base=settings.SUPABASE_PUBLIC_URL,
            internal_base=settings.SUPABASE_URL,
        ),
        limits=httpx.Limits(
            max_connections=50,
            max_keepalive_connections=20,
        ),
    )
    jwks_http = httpx.AsyncClient(timeout=10.0)

    app.state.db = AsyncpgDatabaseAdapter(db_pool)
    app.state.jwks_verifier = JwksVerifier(
        jwks_url=settings.SUPABASE_JWKS_URL,
        http_client=jwks_http,
    )
    app.state.storage = SupabaseStorageAdapter(
        supabase_url=settings.SUPABASE_PUBLIC_URL,  # for self-hosted
        service_role_key=settings.SUPABASE_SERVICE_ROLE_KEY,
        bucket=settings.SUPABASE_STORAGE_BUCKET,
        client=storage_http,
    )
    app.state.cache = RedisAdapter(
        client=redis_client,
        channel=settings.UPLOADS_CHANNEL,
        high_water_mark=settings.QUEUE_HIGH_WATER_MARK,
        rate_limit=settings.RATE_LIMIT_PRESIGN_PER_MINUTE,
    )

    logger.info("startup.complete")

    yield

    # --- shutdown ---
    logger.info("shutdown.begin")
    await db_pool.close()
    await redis_client.aclose()
    await storage_http.aclose()
    await jwks_http.aclose()
    logger.info("shutdown.complete")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
    lifespan=lifespan,
)

# SEC-016: restrict CORS to the Vercel frontend domain only
if settings.all_cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.all_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

app.include_router(api_router, prefix=settings.API_V1_STR)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """
    Global fallback handler.
    Log the full traceback internally; return a generic 500 to the client.
    Never expose internal error details (SEC-006).
    """
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        traceback=traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred"},
    )
