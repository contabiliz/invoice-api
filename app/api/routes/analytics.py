"""
Analytics endpoints:

  GET /analytics/kpis         — aggregated KPIs (total, success rate, failures)
  GET /analytics/timeseries   — daily time-series with presets
  GET /analytics/documents    — paginated document breakdown
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.adapters.database import DatabasePort
from app.api.deps import get_current_user, get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


# ─── kpis ─────────────────────────────────────────────────────────────────────


@router.get(
    "/kpis",
    response_model=None,
    summary="Get analytics KPIs",
)
async def get_analytics_kpis(
    user_id: UUID = Depends(get_current_user),
    db: DatabasePort = Depends(get_db),
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    result = await db.get_analytics_kpis(user_id, days=days)
    return result.model_dump()


# ─── timeseries ───────────────────────────────────────────────────────────────


_PRESET_DAYS = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "mtd": 0,  # calculated
}


@router.get(
    "/timeseries",
    response_model=None,
    summary="Get analytics time-series",
)
async def get_analytics_timeseries(
    user_id: UUID = Depends(get_current_user),
    db: DatabasePort = Depends(get_db),
    preset: str = Query(default="mtd", pattern="^(7d|30d|90d|mtd)$"),
) -> dict:
    days = _PRESET_DAYS[preset]
    if days == 0:
        # mtd = days remaining in current month
        from datetime import date

        today = date.today()
        days = today.day
    result = await db.get_analytics_timeseries(user_id, days=days)
    return result.model_dump()


# ─── documents breakdown ──────────────────────────────────────────────────────


@router.get(
    "/documents",
    response_model=None,
    summary="Get paginated document breakdown",
)
async def get_analytics_documents(
    user_id: UUID = Depends(get_current_user),
    db: DatabasePort = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
) -> dict:
    result = await db.get_analytics_documents(
        user_id,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return result.model_dump()
