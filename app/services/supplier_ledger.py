"""The claim that stops a retry from buying a second eSIM.

eSIM Access deduplicates a repeated order by the transaction id we send it, so
retrying there is free. eSIMCard's purchase endpoint accepts a package id and
nothing else: two calls, two eSIMs, two charges, no way to reverse either. A
Celery retry is an ordinary event — a worker restart, a network blip, a task
redelivered after a lost ack — so without a guard, "ordinary" costs money.

The guard is a database row, not a lock or a cache. `INSERT` under a unique key
over (order, provider, line_key) either succeeds — this process owns the
purchase — or raises `IntegrityError`, meaning some earlier attempt already owns
it. That decision survives a process crash, a Redis flush, and two workers
racing on the same task, which is more than any in-memory scheme can promise.

The states carry the honest amount of uncertainty:

  claimed  we were about to spend money, or did, and do not know which.
  done     bought; `supplier_ref` names the eSIM.
  failed   refused before any charge; safe to route elsewhere.

A claimed row is deliberately NOT auto-retried. Retrying risks paying twice;
skipping risks a customer with no eSIM. Neither is acceptable as a silent
default, so it is reconciled against the supplier's own eSIM list — which is
authoritative about what we actually bought — and only released when that list
proves nothing was allocated.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import SupplierPurchase

logger = get_logger(__name__)

CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"


@dataclass(frozen=True)
class Claim:
    """Permission to buy one unit, or the record of why we must not."""

    row_id: int
    line_key: str
    package_code: str
    # False when a previous attempt already holds this unit. `existing_state`
    # says what became of it.
    ours: bool
    existing_state: str = ""
    existing_ref: str = ""


def line_keys(package_code: str, quantity: int, plan_id: int) -> list[str]:
    """Stable names for the individual units of one order line.

    Includes the plan id so two lines of the same package in one order — which
    the cart permits — do not collide, and stays identical across retries, which
    is the whole point.
    """
    return [f"{plan_id}:{package_code}:{index}" for index in range(1, quantity + 1)]


def claim(db: Session, *, order_id: int, provider: str, line_key: str, package_code: str) -> Claim:
    """Take ownership of one unit before spending anything.

    Uses a nested transaction so the `IntegrityError` from a collision does not
    poison the caller's session — the outer transaction still has real work to
    do afterwards, like recording the units this attempt *did* win.
    """
    row = SupplierPurchase(
        order_id=order_id,
        provider=provider,
        line_key=line_key,
        package_code=package_code,
        state=CLAIMED,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = (
            db.query(SupplierPurchase)
            .filter(
                SupplierPurchase.order_id == order_id,
                SupplierPurchase.provider == provider,
                SupplierPurchase.line_key == line_key,
            )
            .one()
        )
        if existing.state == FAILED and _retake(db, existing.id):
            # A refusal happened before any charge, so this unit is genuinely
            # unbought and buying it now is safe. Retaken with a conditional
            # UPDATE rather than a plain write: if two workers both find the
            # failed row, exactly one wins the state change and the other is
            # told to wait, which a read-then-write would not guarantee.
            logger.info(
                "supplier_ledger.retaken_after_failure",
                order_id=order_id,
                provider=provider,
                line_key=line_key,
            )
            return Claim(
                row_id=existing.id, line_key=line_key, package_code=package_code, ours=True
            )
        logger.info(
            "supplier_ledger.already_claimed",
            order_id=order_id,
            provider=provider,
            line_key=line_key,
            state=existing.state,
        )
        return Claim(
            row_id=existing.id,
            line_key=line_key,
            package_code=package_code,
            ours=False,
            existing_state=existing.state,
            existing_ref=existing.supplier_ref,
        )

    return Claim(row_id=row.id, line_key=line_key, package_code=package_code, ours=True)


def _retake(db: Session, row_id: int) -> bool:
    """Move one failed claim back to claimed, atomically. True if we won it."""
    from sqlalchemy import update

    result = db.execute(
        update(SupplierPurchase)
        .where(SupplierPurchase.id == row_id, SupplierPurchase.state == FAILED)
        .values(state=CLAIMED)
    )
    return bool(result.rowcount)


def settle(
    db: Session,
    claim_id: int,
    *,
    state: str,
    supplier_ref: str = "",
    iccid: str = "",
    note: str = "",
) -> None:
    """Record what became of a claim. Committed by the caller with the order."""
    row = db.get(SupplierPurchase, claim_id)
    if row is None:  # pragma: no cover - the claim was just created
        return
    row.state = state
    if supplier_ref:
        row.supplier_ref = supplier_ref
    if iccid:
        row.iccid = iccid
    if note:
        row.note = note[:200]


def purchases_for(db: Session, order_id: int, provider: str) -> list[SupplierPurchase]:
    return (
        db.query(SupplierPurchase)
        .filter(SupplierPurchase.order_id == order_id, SupplierPurchase.provider == provider)
        .order_by(SupplierPurchase.id)
        .all()
    )
