"""
E2E test fixtures.

Requires local services running:
  supabase start
  docker compose up invoice-api redis

Environment variables (all have sensible local defaults):
  API_BASE_URL          default: http://localhost:8000
  SUPABASE_URL          default: http://localhost:54321
  SUPABASE_ANON_KEY     from: supabase status
  TEST_USER_EMAIL       default: e2e_primary@test.local
  TEST_USER_PASSWORD    default: e2eTestPassword1!
  TEST_USER2_EMAIL      default: e2e_secondary@test.local
  TEST_USER2_PASSWORD   default: e2eTestPassword2!

Users are created automatically on first run if they don't exist.
"""

import os

import httpx
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
SUPABASE_URL = os.getenv("SUPABASE_URL", "http://localhost:54321")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

TEST_USER_EMAIL = os.getenv("TEST_USER_EMAIL", "e2e_primary@test.local")
TEST_USER_PASSWORD = os.getenv("TEST_USER_PASSWORD", "e2eTestPassword1!")
TEST_USER2_EMAIL = os.getenv("TEST_USER2_EMAIL", "e2e_secondary@test.local")
TEST_USER2_PASSWORD = os.getenv("TEST_USER2_PASSWORD", "e2eTestPassword2!")

API_PREFIX = "/api/v1"


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_jwt(client: httpx.AsyncClient, email: str, password: str) -> str:
    """Sign in via Supabase Auth and return the access token."""
    resp = await client.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Content-Type": "application/json",
        },
        json={"email": email, "password": password},
    )
    if resp.status_code == 400:
        # User doesn't exist — sign up first
        await _sign_up(client, email, password)
        resp = await client.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Content-Type": "application/json",
            },
            json={"email": email, "password": password},
        )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def _sign_up(client: httpx.AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        f"{SUPABASE_URL}/auth/v1/signup",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Content-Type": "application/json",
        },
        json={"email": email, "password": password},
    )
    resp.raise_for_status()


# ── Session-scoped fixtures ───────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session")
async def http() -> httpx.AsyncClient:
    """Raw httpx client (no auth) — shared for the entire session."""
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0) as client:
        yield client


@pytest_asyncio.fixture(scope="session")
async def supabase_http() -> httpx.AsyncClient:
    async with httpx.AsyncClient(timeout=15.0) as client:
        yield client


@pytest_asyncio.fixture(scope="session")
async def token(supabase_http: httpx.AsyncClient) -> str:
    return await _get_jwt(supabase_http, TEST_USER_EMAIL, TEST_USER_PASSWORD)


@pytest_asyncio.fixture(scope="session")
async def token2(supabase_http: httpx.AsyncClient) -> str:
    return await _get_jwt(supabase_http, TEST_USER2_EMAIL, TEST_USER2_PASSWORD)


@pytest_asyncio.fixture(scope="session")
async def authed(token: str) -> httpx.AsyncClient:
    """Authenticated API client for user 1."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=API_BASE_URL, headers=headers, timeout=30.0
    ) as client:
        yield client


@pytest_asyncio.fixture(scope="session")
async def authed2(token2: str) -> httpx.AsyncClient:
    """Authenticated API client for user 2 (cross-tenant tests)."""
    headers = {"Authorization": f"Bearer {token2}"}
    async with httpx.AsyncClient(
        base_url=API_BASE_URL, headers=headers, timeout=30.0
    ) as client:
        yield client


# ── Reusable helpers ──────────────────────────────────────────────────────────

SAMPLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc versao="4.00" xmlns="http://www.portalfiscal.inf.br/nfe">
  <NFe><infNFe versao="4.00" Id="NFe12345678901234567890123456789012345678901234">
    <ide><cUF>35</cUF><mod>55</mod></ide>
  </infNFe></NFe>
</nfeProc>"""


async def presign_one_doc(client: httpx.AsyncClient, filename: str = "nota.xml") -> dict:
    """Helper: presign a single XML document and return the full response JSON."""
    resp = await client.post(
        f"{API_PREFIX}/batches/presign",
        json={
            "documents": [
                {"filename": filename, "mime_type": "application/xml", "file_size": len(SAMPLE_XML)}
            ]
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


async def upload_to_presigned_url(upload_url: str, content: bytes = SAMPLE_XML) -> None:
    """PUT the file content directly to the presigned URL (bypasses API).

    Replaces host.docker.internal with localhost so host-side tests can reach
    Supabase Storage when the API runs in Docker.
    """
    upload_url = upload_url.replace("host.docker.internal", "localhost")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.put(
            upload_url,
            content=content,
            headers={"Content-Type": "application/xml"},
        )
        assert resp.status_code in (200, 201), f"Upload failed: {resp.status_code} {resp.text}"
