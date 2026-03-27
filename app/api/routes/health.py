from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()


@router.get("/health", include_in_schema=False)
async def health() -> Response:
    """Liveness probe for Railway / ECS health checks."""
    return Response(status_code=200)
