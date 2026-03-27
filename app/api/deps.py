"""
FastAPI dependencies — injected into route handlers.

Auth dependency (get_current_user) validates the JWT and returns the user_id.
All other dependencies pull adapters from app.state (set during lifespan).
"""

from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.adapters.cache import CachePort
from app.adapters.database import DatabasePort
from app.adapters.storage import StoragePort
from app.core.security import AuthError, get_user_id

_bearer = HTTPBearer(auto_error=False)


def get_db(request: Request) -> DatabasePort:
    return request.app.state.db


def get_storage(request: Request) -> StoragePort:
    return request.app.state.storage


def get_cache(request: Request) -> CachePort:
    return request.app.state.cache


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> UUID:
    """
    Validate the Bearer JWT and return the authenticated user_id.
    Returns 401 on any auth failure — never exposes internal details.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = await request.app.state.jwks_verifier.decode(credentials.credentials)
        return get_user_id(payload)
    except AuthError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
