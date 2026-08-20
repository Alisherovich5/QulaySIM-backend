"""ATMOS Callback API — the inbound half, and the only place money is confirmed.

ATMOS asks *before* it debits the card: it POSTs the payment's facts to us and
only proceeds when we answer ``{"status": 1}``. That makes this handler the
last line of defence — a careless 1 here is a captured payment for an order we
cannot honour. So the rules mirror Payme's `_check_payable` discipline exactly:
the order must exist, be pending, and the amount must equal the som total
frozen at checkout, to the tiyin.

Replays are a fact of the protocol (ATMOS retries until it gets a clean
answer), so confirming is idempotent: the same transaction_id confirming the
same order answers 1 again and changes nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import ESIM, AtmosTransaction, Order, Payment
from app.db.models.enums import OrderStatus

logger = get_logger(__name__)

STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"

# The doc pins the concatenation — store_id+transaction_id+account+amount+api_key,
# no separators — but not the digest. ATMOS's public integrations use MD5; if the
# sandbox disagrees, this constant is the single thing to change.
_DIGEST = hashlib.md5


async def _ensure_fulfilment(session: AsyncSession, order_id: int) -> None:
    """Dispatch provisioning for an order that has no eSIM yet.

    Called on the replay paths as well as the first confirmation. A repeat
    callback is correct to answer OK for the *payment*, and it used to stop
    there — so if the first callback committed the charge and then lost its
    dispatch (Redis unreachable for a moment, or the task exhausting its
    retries), the retry that ATMOS helpfully sent changed nothing and the
    customer stayed paid-up with no eSIM until somebody read a daily report.

    Fulfilment is idempotent, so dispatching again costs a no-op at worst.
    """
    from app.workers.tasks.provisioning import fulfil_paid_order

    has_esim = (
        await session.execute(select(ESIM.id).where(ESIM.order_id == order_id).limit(1))
    ).first()
    if has_esim:
        return
    # Only when nothing has been ordered from a supplier yet. If a supplier order
    # exists but no profile has arrived, what is missing is the *sync*, not the
    # purchase — and dispatching a purchase alongside one already in flight is
    # the single way this safety net could cost money rather than save it. The
    # five-minute sweep picks that case up, by which time no task is running.
    order = await session.get(Order, order_id)
    if order is None or order.provider_order_no:
        return
    logger.info("atmos.refulfil", order_id=order_id)
    fulfil_paid_order.delay(order_id)


async def _alarm(reason: str, **facts: Any) -> None:
    """Say out loud that a payment was refused.

    The reason this exists: a customer paid 79 999 so‘m, the confirmation was
    refused because the caller’s address had changed to Cloudflare’s, and the
    only trace was one warning line in a log nobody was reading. The money was
    gone from the customer’s side and the order sat "pending". We found out
    because they wrote to support.

    A refusal is rare and always worth waking somebody for, so it goes to the
    operations chat immediately. Failing to send must never fail the callback:
    ATMOS is waiting on the answer, and a telegram outage is not a reason to
    turn a refusal into a timeout.
    """
    detail = " · ".join(f"{k}: {v}" for k, v in facts.items() if v not in (None, ""))
    try:
        from app.integrations.telegram import send_html

        await send_html(
            "⚠️ <b>To‘lov rad etildi</b>\n"
            f"Sabab: {reason}\n"
            f"{detail}\n\n"
            "Mijozdan pul yechilgan bo‘lishi mumkin — tekshirish kerak."
        )
    except Exception:  # noqa: BLE001 - never let the alarm break the answer
        logger.warning("atmos.alarm_failed", reason=reason)


def _ok(message: str = "Успешно") -> dict[str, Any]:
    return {"status": 1, "message": message}


def _refuse(message: str) -> dict[str, Any]:
    return {"status": 0, "message": message}


def expected_sign(store_id: str, transaction_id: str, account: str, amount: str) -> str:
    raw = f"{store_id}{transaction_id}{account}{amount}{settings.atmos_callback_api_key}"
    return _DIGEST(raw.encode()).hexdigest()


def caller_allowed(ip: str) -> bool:
    """Defence in depth on top of the signature, per the doc's own demand."""
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(settings.atmos_callback_cidr)
    except ValueError:
        return False


def _expected_tiyin(order: Order) -> int | None:
    if order.amount_uzs is None:
        return None
    return int((order.amount_uzs * 100).to_integral_value())


async def alarm_bad_ip(ip: str) -> None:
    """A callback from an address that is not the payment provider's."""
    await _alarm("noma‘lum manzildan keldi (IP ro‘yxatda yo‘q)", ip=ip)


async def handle_callback(session: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    """Answer ATMOS's pre-debit confirmation. Always returns a status body."""
    if not settings.atmos_callback_api_key:
        return _refuse("ATMOS is not configured")

    store_id = str(payload.get("store_id") or "")
    transaction_id = str(payload.get("transaction_id") or "")
    account = str(payload.get("account") or "")
    amount = str(payload.get("amount") or "")
    sign = str(payload.get("sign") or "")

    if not transaction_id or not account:
        return _refuse("Malformed callback")

    # Values are hashed exactly as received: re-formatting them (say, int()
    # round-tripping the amount) would make a signature that ATMOS computed
    # over "100000.0" unverifiable.
    if not hmac.compare_digest(expected_sign(store_id, transaction_id, account, amount), sign):
        logger.warning("atmos.bad_sign", transaction_id=transaction_id)
        await _alarm("imzo to‘g‘ri kelmadi", tranzaksiya=transaction_id, buyurtma=account)
        return _refuse("Invalid signature")

    if store_id != str(settings.atmos_store_id):
        return _refuse("Unknown store")

    # Replay? The unique transaction_id row is the memory of the first answer.
    existing = (
        (
            await session.execute(
                select(AtmosTransaction).where(AtmosTransaction.transaction_id == transaction_id)
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        if existing.status == STATUS_CONFIRMED:
            # The payment is settled; the eSIM may not be. See _ensure_fulfilment.
            if account.isdigit():
                await _ensure_fulfilment(session, int(account))
            return _ok()
        return _refuse("Transaction was rejected")

    if not account.isdigit():
        return _refuse(f"Account {account} is not recognised")
    order = await session.get(Order, int(account), with_for_update=True)
    if order is None:
        return _refuse(f"Account {account} is not recognised")

    async def record(status: str) -> None:
        session.add(
            AtmosTransaction(
                order_id=order.id,
                transaction_id=transaction_id,
                amount_tiyin=int(Decimal(amount)) if amount.replace(".", "", 1).isdigit() else 0,
                account=account,
                status=status,
            )
        )
        await session.commit()

    expected = _expected_tiyin(order)
    try:
        received = int(Decimal(amount))
    except ArithmeticError:
        received = -1
    if expected is None or received != expected:
        await record(STATUS_REJECTED)
        logger.warning(
            "atmos.amount_mismatch", order_id=order.id, expected=expected, received=amount
        )
        await _alarm(
            "summa buyurtmaga to‘g‘ri kelmadi",
            buyurtma=order.id,
            kutilgan=expected,
            kelgan=amount,
        )
        return _refuse("Amount does not match the order")

    if order.status != OrderStatus.PENDING:
        # Paid already (perhaps via a racing callback that committed first):
        # honest replay only if it was this very transaction.
        if order.status == OrderStatus.PAID and order.provider_transaction_id == transaction_id:
            await _ensure_fulfilment(session, order.id)
            return _ok()
        await record(STATUS_REJECTED)
        await _alarm(
            "buyurtma to‘lovga yaroqsiz holatda",
            buyurtma=order.id,
            holati=str(order.status),
            tranzaksiya=transaction_id,
        )
        return _refuse("Order is not payable")

    order.status = OrderStatus.PAID
    order.paid_at = utcnow()
    order.provider_transaction_id = transaction_id
    session.add(
        Payment(
            order_id=order.id,
            method="atmos",
            amount=Decimal(received) / 100,
            status="success",
            provider_ref=transaction_id,
        )
    )
    session.add(
        AtmosTransaction(
            order_id=order.id,
            transaction_id=transaction_id,
            amount_tiyin=received,
            account=account,
            status=STATUS_CONFIRMED,
        )
    )
    await session.commit()

    logger.info("atmos.confirmed", order_id=order.id, transaction_id=transaction_id)

    # Provisioning happens off the request, exactly as with Payme: ATMOS is
    # waiting on this response and a slow supplier must not turn a confirmed
    # charge into a timeout it will retry.
    from app.workers.tasks.provisioning import fulfil_paid_order

    fulfil_paid_order.delay(order.id)

    return _ok()
