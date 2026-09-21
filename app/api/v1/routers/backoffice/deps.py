"""Who is asking, and are they allowed.

Every backoffice endpoint takes `CurrentStaff`. There is no anonymous variant
on purpose: an endpoint that forgets the dependency fails to compile its
signature rather than quietly serving the world.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.api.deps import SessionDep
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.security import decode_token
from app.db.models import Staff
from app.services.backoffice import auth as service


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token if scheme.lower() == "bearer" and token else None


async def get_current_staff(request: Request, session: SessionDep) -> Staff:
    token = _bearer(request)
    if not token:
        raise AuthenticationError("Kirish talab qilinadi")
    payload = decode_token(token, "access")
    staff = await service.load(session, service.staff_id_from(str(payload["sub"])))
    if staff is None:
        raise AuthenticationError("Hisob topilmadi yoki o‘chirilgan")
    return staff


CurrentStaff = Annotated[Staff, Depends(get_current_staff)]


async def require_owner(staff: CurrentStaff) -> Staff:
    """For the handful of actions an operator must not take on their own:
    spending the supplier wallet, switching tariffs on and off, running an
    import. Everything else an operator can do."""
    if staff.role != "owner":
        raise PermissionDeniedError("Bu amal faqat egasi uchun")
    return staff


OwnerOnly = Annotated[Staff, Depends(require_owner)]
