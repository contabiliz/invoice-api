from fastapi import APIRouter

from app.api.routes import analytics, batches, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(batches.router)
api_router.include_router(analytics.router)
