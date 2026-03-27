"""
T-CRIT-001: Every protected endpoint returns 401 without a valid JWT.
A missing middleware registration is a silent full exposure.
"""

import pytest

from tests.e2e.conftest import API_PREFIX

_PROTECTED_ENDPOINTS = [
    ("POST", f"{API_PREFIX}/batches/presign"),
    ("POST", f"{API_PREFIX}/batches/00000000-0000-0000-0000-000000000000/confirm"),
    ("GET",  f"{API_PREFIX}/batches/00000000-0000-0000-0000-000000000000"),
    ("GET",  f"{API_PREFIX}/batches/00000000-0000-0000-0000-000000000000/documents"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,url", _PROTECTED_ENDPOINTS)
async def test_no_token_returns_401(http, method, url):
    resp = await http.request(method, url, json={})
    assert resp.status_code == 401, f"{method} {url} returned {resp.status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,url", _PROTECTED_ENDPOINTS)
async def test_invalid_token_returns_401(http, method, url):
    resp = await http.request(
        method, url,
        headers={"Authorization": "Bearer this.is.invalid"},
        json={},
    )
    assert resp.status_code == 401, f"{method} {url} returned {resp.status_code}"


@pytest.mark.asyncio
async def test_expired_token_returns_401(http):
    # A well-formed but expired JWT (signed with wrong secret — same effect)
    expired = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwiZXhwIjoxfQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    resp = await http.post(
        f"{API_PREFIX}/batches/presign",
        headers={"Authorization": f"Bearer {expired}"},
        json={"documents": []},
    )
    assert resp.status_code == 401
