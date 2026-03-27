"""
JWT validation via Supabase JWKS endpoint.

Supabase (v2) issues ES256 tokens signed with an asymmetric key pair.
We verify them by:
  1. Extracting the `kid` from the token header.
  2. Looking up the matching public key from the JWKS cache.
  3. Refetching the JWKS if the kid is unknown (handles key rotation).
"""

import asyncio
from typing import Any
from uuid import UUID

import httpx
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError


class AuthError(Exception):
    pass


class JwksVerifier:
    def __init__(self, jwks_url: str, http_client: httpx.AsyncClient) -> None:
        self._jwks_url = jwks_url
        self._http = http_client
        self._keys: dict[str, Any] = {}  # kid -> JWK dict
        self._lock = asyncio.Lock()

    async def _fetch_jwks(self) -> None:
        resp = await self._http.get(self._jwks_url)
        resp.raise_for_status()
        self._keys = {k["kid"]: k for k in resp.json()["keys"]}

    async def _get_key(self, kid: str) -> dict:
        if kid not in self._keys:
            async with self._lock:
                if kid not in self._keys:
                    await self._fetch_jwks()
        if kid not in self._keys:
            raise AuthError(f"Unknown key id: {kid}")
        return self._keys[kid]

    async def decode(self, token: str) -> dict:
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise AuthError(f"Invalid token header: {exc}") from exc

        kid = header.get("kid")
        if not kid:
            raise AuthError("Token missing kid claim")

        alg = header.get("alg", "ES256")
        key = await self._get_key(kid)

        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=[alg],
                options={"verify_aud": False},
            )
        except ExpiredSignatureError:
            raise AuthError("Token has expired")
        except JWTError as exc:
            raise AuthError(f"Invalid token: {exc}") from exc

        if not payload.get("sub"):
            raise AuthError("Token missing subject claim")

        return payload


def get_user_id(payload: dict) -> UUID:
    try:
        return UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthError("Invalid user_id in token") from exc
