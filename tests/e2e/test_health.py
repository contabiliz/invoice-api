import pytest

from tests.e2e.conftest import API_PREFIX


@pytest.mark.asyncio
async def test_health_returns_200(http):
    resp = await http.get(f"{API_PREFIX}/health")
    assert resp.status_code == 200
