"""The backoffice API.

Everything lives under /api/v1/backoffice and every route requires a signed-in
member of staff. It shares the database and the domain code with the storefront
API and adds no second source of truth — which is the whole reason the admin
could move here without a data migration.
"""

from fastapi import APIRouter

from app.api.v1.routers.backoffice import (
    auth,
    catalogue,
    content,
    dashboard,
    esims,
    grants,
    growth,
    money,
    orders,
    people,
    system,
)

router = APIRouter()
for module in (
    auth,
    dashboard,
    orders,
    esims,
    grants,
    catalogue,
    people,
    money,
    system,
    growth,
    content,
):
    router.include_router(module.router)

__all__ = ["router"]
