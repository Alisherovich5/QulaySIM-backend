"""Turning a completed eSIMCard purchase into an eSIM the customer can install.

Separate from `esimcard.py` because this is the half that touches our database,
and separate from the eSIM Access sync because the two suppliers hand over
profiles in different shapes and at different times.

The reason this step exists at all: eSIMCard's purchase call answers with the
eSIM's id but not its activation code. The code shows up afterwards as
`universal_link` in `/my-esims`, and for a `sim_applied: false` purchase it may
be a couple of minutes late. So between "paid" and "installable" there is always
a window, and this closes it.

`universal_link` is an Apple provisioning URL whose `carddata` query parameter is
the LPA string — the same payload eSIM Access hands over as `ac`. Both are stored
as `qr_payload` and rendered to the same QR, so the account page and the e-mail
do not care which wholesaler served the order.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session, joinedload

from app.core.logging import get_logger
from app.db.models import ESIM, Order, OrderItem
from app.integrations.esimcard import EsimCardClient, RemoteEsim
from app.integrations.qr import render_qr_data_url
from app.services import supplier_ledger as ledger

logger = get_logger(__name__)

# eSIMCard's own words for a profile's life stage, mapped onto ours. Anything
# unrecognised stays "pending" rather than guessing: a wrong "active" starts a
# validity countdown on the customer's screen that has not really begun.
_STATUS_MAP = {
    "installed": "active",
    "released": "pending",
    "in use": "active",
    "active": "active",
    "expired": "expired",
    "deleted": "expired",
    "revoked": "expired",
}


def activation_payload(universal_link: str) -> str:
    """Pull the LPA string out of an Apple provisioning URL.

    Falls back to the whole link when it does not carry `carddata`: a link we
    cannot parse is still something the customer can tap, and shipping it beats
    shipping an empty QR.
    """
    if not universal_link:
        return ""
    query = parse_qs(urlparse(universal_link).query)
    payload = (query.get("carddata") or [""])[0].strip()
    return payload or universal_link


def _local_status(supplier_status: str) -> str:
    return _STATUS_MAP.get(supplier_status.strip().lower(), "pending")


def _plan_for(row, items_by_id: dict, plans_by_code: dict):
    """The plan this purchase was for, by item id, falling back to the code."""
    item_id, _, _ = row.line_key.partition(":")
    if item_id.isdigit():
        item = items_by_id.get(int(item_id))
        if item is not None and item.plan is not None:
            return item.plan
    return plans_by_code.get(row.package_code)


def sync_order_profiles(db: Session, order: Order, client: EsimCardClient | None = None) -> int:
    """Fetch the eSIMs this order bought and upsert them as customer profiles.

    Driven by the purchase ledger rather than by anything the supplier echoes
    back: the ledger is the only record of which eSIMs belong to which order,
    because eSIMCard has no concept of an order at all — only of eSIMs in our
    account. Reading `/my-esims` and guessing by timestamp would attach one
    customer's eSIM to another customer's order.

    Returns the number of profiles created or refreshed. Zero is a normal answer
    for a purchase that has not been cut yet, and the caller retries.
    """
    if order.provider != "esimcard":
        return 0

    rows = ledger.purchases_for(db, order.id, "esimcard")
    done = [row for row in rows if row.state == ledger.DONE and row.supplier_ref]
    if not done:
        unresolved = [row.line_key for row in rows if row.state == ledger.CLAIMED]
        if unresolved:
            # Money may have moved with nothing to show for it. Error level: this
            # is the one state that needs a human to look at the wallet.
            logger.error(
                "esimcard.unresolved_purchases",
                order_id=order.id,
                line_keys=unresolved,
            )
        return 0

    if not order.items:
        order = (
            db.query(Order)
            .options(joinedload(Order.items).joinedload(OrderItem.plan))
            .filter(Order.id == order.id)
            .one()
        )
    # `line_key` is "<order item id>:<n>", so the plan comes back exactly rather
    # than by matching package codes — two plans sharing one supplier code would
    # otherwise be a coin toss, and the wrong plan means the wrong allowance and
    # the wrong expiry on the customer's screen.
    items_by_id = {item.id: item for item in order.items}
    # Kept as a fallback for rows written before the key carried the item id.
    plans_by_code = {
        offer.package_code: item.plan
        for item in order.items
        if item.plan is not None
        for offer in item.plan.offers
        if offer.provider == "esimcard"
    }
    for item in order.items:
        if item.plan is not None and item.plan.provider_package_code:
            plans_by_code.setdefault(item.plan.provider_package_code, item.plan)

    remote = (client or EsimCardClient()).find_esims({row.supplier_ref for row in done})
    touched = 0

    for row in done:
        profile: RemoteEsim | None = remote.get(row.supplier_ref)
        plan = _plan_for(row, items_by_id, plans_by_code)
        if profile is None:
            # Bought, but not yet listed. Expected for a delayed purchase.
            logger.info(
                "esimcard.profile_not_ready", order_id=order.id, supplier_ref=row.supplier_ref
            )
            continue
        payload = activation_payload(profile.universal_link)
        if not payload or plan is None:
            # Never create a profile with no QR or the wrong plan attached: an
            # eSIM the customer cannot install, or can install against someone
            # else's allowance, is worse than one that is still coming.
            logger.warning(
                "esimcard.profile_incomplete",
                order_id=order.id,
                supplier_ref=row.supplier_ref,
                has_payload=bool(payload),
                package_code=row.package_code,
            )
            continue

        iccid = profile.iccid or row.iccid
        existing = (
            db.query(ESIM)
            .filter(ESIM.provider == "esimcard", ESIM.provider_esim_tran_no == row.supplier_ref)
            .first()
        )
        values = {
            "plan_id": plan.id,
            "iccid": iccid,
            "qr_payload": payload,
            "qr_image": render_qr_data_url(payload),
            "provider": "esimcard",
            "provider_esim_tran_no": row.supplier_ref,
            "provider_status": profile.status,
            "provider_qr_url": profile.universal_link,
            "status": _local_status(profile.status),
            "validity_days": plan.validity_days,
        }
        if existing:
            for field, value in values.items():
                setattr(existing, field, value)
        else:
            db.add(ESIM(order_id=order.id, customer_id=order.customer_id, **values))
        if iccid and iccid != row.iccid:
            row.iccid = iccid
        touched += 1

    if touched:
        order.provider_status = "GOT_RESOURCE"
    db.commit()
    return touched
