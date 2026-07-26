"""Payme Merchant API handlers.

Every method runs inside one transaction and takes a row lock before it reads
state it is about to change: Payme retries aggressively and may call
PerformTransaction twice concurrently, which without a lock would capture the
same order twice.

The account value is the order id. `CheckPerformTransaction` therefore also
answers "is this order still payable", which is what stops a second payment
against an order that is already paid.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models import Order, Payment, PaymeTransaction
from app.db.models.enums import OrderStatus
from app.integrations.payme import (
    ACCOUNT_NOT_FOUND,
    ACCOUNT_NOT_PAYABLE,
    CANNOT_PERFORM,
    INVALID_AMOUNT,
    METHOD_NOT_ALLOWED,
    METHOD_NOT_FOUND,
    STATE_CANCELLED,
    STATE_CANCELLED_AFTER_PERFORM,
    STATE_CREATED,
    STATE_PERFORMED,
    TIMEOUT_CANCEL_REASON,
    TRANSACTION_NOT_FOUND,
    TRANSACTION_TIMEOUT_MS,
    error,
    now_ms,
    result,
)

logger = get_logger(__name__)


def _account_value(params: dict[str, Any]) -> str:
    account = params.get("account") or {}
    return str(account.get(settings.payme_account_field, "")).strip()


async def _find_order(session: AsyncSession, account: str, *, lock: bool = False) -> Order | None:
    if not account.isdigit():
        return None
    stmt = select(Order).where(Order.id == int(account))
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalars().first()


def _expected_tiyin(order: Order) -> int | None:
    """The som amount frozen at checkout, in tiyin.

    An order without a frozen amount was not created for Payme, so there is
    nothing to validate against and it must not be payable.
    """
    if order.amount_uzs is None:
        return None
    return int((order.amount_uzs * 100).to_integral_value())


async def _check_payable(
    session: AsyncSession, request_id: Any, params: dict[str, Any]
) -> dict[str, Any] | None:
    """Shared by CheckPerformTransaction and CreateTransaction.

    Returns an error response, or None when the order can be paid.
    """
    account = _account_value(params)
    order = await _find_order(session, account)
    if order is None:
        return error(request_id, ACCOUNT_NOT_FOUND, settings.payme_account_field)

    expected = _expected_tiyin(order)
    if expected is None or order.status != OrderStatus.PENDING:
        return error(request_id, ACCOUNT_NOT_PAYABLE, settings.payme_account_field)

    if int(params.get("amount") or 0) != expected:
        return error(request_id, INVALID_AMOUNT, "amount")
    return None


async def check_perform_transaction(
    session: AsyncSession, request_id: Any, params: dict[str, Any]
) -> dict[str, Any]:
    rejection = await _check_payable(session, request_id, params)
    return rejection or result(request_id, {"allow": True})


async def create_transaction(
    session: AsyncSession, request_id: Any, params: dict[str, Any]
) -> dict[str, Any]:
    transaction_id = str(params.get("id") or "")

    existing = (
        (
            await session.execute(
                select(PaymeTransaction)
                .where(PaymeTransaction.transaction_id == transaction_id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )

    if existing is not None:
        # A retry. Payme expects the original transaction back, unchanged.
        if existing.state != STATE_CREATED:
            return error(request_id, CANNOT_PERFORM, "id")
        return result(
            request_id,
            {
                "create_time": existing.create_time,
                "transaction": str(existing.id),
                "state": existing.state,
            },
        )

    rejection = await _check_payable(session, request_id, params)
    if rejection is not None:
        return rejection

    account = _account_value(params)
    order = await _find_order(session, account, lock=True)
    assert order is not None  # _check_payable already proved it exists

    # A second, different transaction against an order that already has a live
    # one would let the customer be charged twice.
    live = (
        (
            await session.execute(
                select(PaymeTransaction).where(
                    PaymeTransaction.order_id == order.id,
                    PaymeTransaction.state.in_([STATE_CREATED, STATE_PERFORMED]),
                )
            )
        )
        .scalars()
        .first()
    )
    if live is not None:
        return error(request_id, ACCOUNT_NOT_PAYABLE, settings.payme_account_field)

    created = now_ms()
    transaction = PaymeTransaction(
        order_id=order.id,
        transaction_id=transaction_id,
        amount_tiyin=int(params["amount"]),
        account=account,
        state=STATE_CREATED,
        create_time=created,
    )
    session.add(transaction)
    try:
        await session.commit()
    except IntegrityError:
        # Two concurrent CreateTransaction calls for the same id; the loser
        # re-reads the winner's row.
        await session.rollback()
        winner = (
            (
                await session.execute(
                    select(PaymeTransaction).where(
                        PaymeTransaction.transaction_id == transaction_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if winner is None:
            return error(request_id, CANNOT_PERFORM, "id")
        return result(
            request_id,
            {
                "create_time": winner.create_time,
                "transaction": str(winner.id),
                "state": winner.state,
            },
        )

    await session.refresh(transaction)
    logger.info("payme.created", order_id=order.id, transaction_id=transaction_id)
    return result(
        request_id,
        {"create_time": created, "transaction": str(transaction.id), "state": STATE_CREATED},
    )


async def perform_transaction(
    session: AsyncSession, request_id: Any, params: dict[str, Any]
) -> dict[str, Any]:
    transaction_id = str(params.get("id") or "")
    transaction = (
        (
            await session.execute(
                select(PaymeTransaction)
                .where(PaymeTransaction.transaction_id == transaction_id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )

    if transaction is None:
        return error(request_id, TRANSACTION_NOT_FOUND, "id")

    if transaction.state == STATE_PERFORMED:
        # Idempotent replay: return the original timestamps.
        return result(
            request_id,
            {
                "transaction": str(transaction.id),
                "perform_time": transaction.perform_time,
                "state": transaction.state,
            },
        )

    if transaction.state != STATE_CREATED:
        return error(request_id, CANNOT_PERFORM, "id")

    moment = now_ms()
    if moment - transaction.create_time > TRANSACTION_TIMEOUT_MS:
        # Payme requires an expired transaction to be cancelled, not performed.
        transaction.state = STATE_CANCELLED
        transaction.reason = TIMEOUT_CANCEL_REASON
        transaction.cancel_time = moment
        await session.commit()
        logger.info("payme.expired", transaction_id=transaction_id)
        return error(request_id, CANNOT_PERFORM, "id")

    order = await session.get(Order, transaction.order_id, with_for_update=True)
    if order is None:
        return error(request_id, CANNOT_PERFORM, "id")

    transaction.state = STATE_PERFORMED
    transaction.perform_time = moment
    order.status = OrderStatus.PAID
    order.paid_at = utcnow()
    order.provider_transaction_id = transaction_id

    session.add(
        Payment(
            order_id=order.id,
            method="payme",
            amount=Decimal(transaction.amount_tiyin) / 100,
            status="success",
            provider_ref=transaction_id,
        )
    )
    await session.commit()

    logger.info("payme.performed", order_id=order.id, transaction_id=transaction_id)

    # Provisioning and rewards happen off the request: Payme is waiting on this
    # response and a slow supplier must not turn a captured payment into a
    # timeout it will retry.
    from app.workers.tasks.provisioning import fulfil_paid_order

    fulfil_paid_order.delay(order.id)

    return result(
        request_id,
        {"transaction": str(transaction.id), "perform_time": moment, "state": STATE_PERFORMED},
    )


async def cancel_transaction(
    session: AsyncSession, request_id: Any, params: dict[str, Any]
) -> dict[str, Any]:
    transaction_id = str(params.get("id") or "")
    transaction = (
        (
            await session.execute(
                select(PaymeTransaction)
                .where(PaymeTransaction.transaction_id == transaction_id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )

    if transaction is None:
        return error(request_id, TRANSACTION_NOT_FOUND, "id")

    if transaction.state in (STATE_CANCELLED, STATE_CANCELLED_AFTER_PERFORM):
        return result(
            request_id,
            {
                "transaction": str(transaction.id),
                "cancel_time": transaction.cancel_time,
                "state": transaction.state,
            },
        )

    moment = now_ms()
    order = await session.get(Order, transaction.order_id, with_for_update=True)

    if transaction.state == STATE_CREATED:
        transaction.state = STATE_CANCELLED
        if order is not None:
            order.status = OrderStatus.CANCELLED
    else:
        # Cancelling a performed transaction is a refund.
        transaction.state = STATE_CANCELLED_AFTER_PERFORM
        if order is not None:
            order.status = OrderStatus.REFUNDED

    transaction.reason = params.get("reason")
    transaction.cancel_time = moment
    await session.commit()

    logger.info(
        "payme.cancelled",
        transaction_id=transaction_id,
        state=transaction.state,
        reason=transaction.reason,
    )
    return result(
        request_id,
        {"transaction": str(transaction.id), "cancel_time": moment, "state": transaction.state},
    )


async def check_transaction(
    session: AsyncSession, request_id: Any, params: dict[str, Any]
) -> dict[str, Any]:
    transaction_id = str(params.get("id") or "")
    transaction = (
        (
            await session.execute(
                select(PaymeTransaction).where(PaymeTransaction.transaction_id == transaction_id)
            )
        )
        .scalars()
        .first()
    )

    if transaction is None:
        return error(request_id, TRANSACTION_NOT_FOUND, "id")

    return result(
        request_id,
        {
            "create_time": transaction.create_time,
            "perform_time": transaction.perform_time,
            "cancel_time": transaction.cancel_time,
            "transaction": str(transaction.id),
            "state": transaction.state,
            "reason": transaction.reason,
        },
    )


async def get_statement(
    session: AsyncSession, request_id: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Reconciliation: every transaction created in the window."""
    start = int(params.get("from") or 0)
    end = int(params.get("to") or 0)

    rows = (
        (
            await session.execute(
                select(PaymeTransaction)
                .where(
                    PaymeTransaction.create_time >= start,
                    PaymeTransaction.create_time <= end,
                )
                .order_by(PaymeTransaction.create_time)
            )
        )
        .scalars()
        .all()
    )

    return result(
        request_id,
        {
            "transactions": [
                {
                    "id": row.transaction_id,
                    "time": row.create_time,
                    "amount": row.amount_tiyin,
                    "account": {settings.payme_account_field: row.account},
                    "create_time": row.create_time,
                    "perform_time": row.perform_time,
                    "cancel_time": row.cancel_time,
                    "transaction": str(row.id),
                    "state": row.state,
                    "reason": row.reason,
                }
                for row in rows
            ]
        },
    )


HANDLERS = {
    "CheckPerformTransaction": check_perform_transaction,
    "CreateTransaction": create_transaction,
    "PerformTransaction": perform_transaction,
    "CancelTransaction": cancel_transaction,
    "CheckTransaction": check_transaction,
    "GetStatement": get_statement,
}


async def dispatch(
    session: AsyncSession, request_id: Any, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    if method == "ChangePassword":
        # Rotating the key over the API is refused deliberately: the key lives
        # in configuration, and letting a caller change it moves the source of
        # truth somewhere nobody is watching.
        return error(request_id, METHOD_NOT_ALLOWED, "method")

    handler = HANDLERS.get(method)
    if handler is None:
        return error(request_id, METHOD_NOT_FOUND, "method")
    return await handler(session, request_id, params)
