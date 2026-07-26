"""Liveness and readiness.

`/api/health` stays dependency-free so a load balancer can distinguish "the
process is up" from "the process can serve traffic" (`/api/health/ready`).
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.deps import SessionDep
from app.core.cache import get_redis
from app.core.config import settings
from app.core.logging import get_logger

router = APIRouter(prefix="/api/health", tags=["health"])
logger = get_logger(__name__)


@router.get("")
async def live() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name, "environment": settings.environment}


@router.get("/ready")
async def ready(session: SessionDep, response: Response) -> dict[str, object]:
    checks: dict[str, str] = {}

    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.error("health.database_down", error=str(exc))
        checks["database"] = "down"

    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.error("health.redis_down", error=str(exc))
        checks["redis"] = "down"

    # Redis degrades gracefully (cache and rate limits fail open), so only a
    # database outage makes the instance unready.
    healthy = checks["database"] == "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if healthy else "degraded", "checks": checks}
