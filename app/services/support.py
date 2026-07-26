from __future__ import annotations

from app.integrations.telegram import send_support_message


async def submit_support_message(
    *, name: str, email: str, phone: str, locale: str, message: str, client_ip: str
) -> None:
    await send_support_message(
        name=name, email=email, phone=phone, locale=locale, message=message, client_ip=client_ip
    )
