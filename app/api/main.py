from fastapi import APIRouter

from app.api.routes import batches, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(batches.router)
