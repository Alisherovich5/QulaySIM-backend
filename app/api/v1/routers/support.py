from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from app.core.config import settings
from app.core.ratelimit import RateLimit, client_ip
from app.schemas.content import SupportMessageIn, SupportMessageOut
from app.services import support as service

router = APIRouter(prefix="/api/support", tags=["support"])


@router.post(
    "/message",
    response_model=SupportMessageOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(RateLimit("support", settings.rate_limit_support))],
)
async def create_support_message(payload: SupportMessageIn, request: Request) -> SupportMessageOut:
    await service.submit_support_message(
        name=payload.name,
        contact=payload.contact,
        message=payload.message,
        client_ip=client_ip(request),
    )
    return SupportMessageOut()
