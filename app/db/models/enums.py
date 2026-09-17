"""String enums mirroring Django's TextChoices.

Django enforces these at the ORM layer only — there are no CHECK constraints
in the database — so the ones this service compares against are spelled out
here rather than typed as bare strings at each call site.

Every member must exist in the Django model it mirrors. Two of these drifted
once and the reason is worth keeping: `PlanScope` and `Provider` were the two
nothing imported, so nothing failed when Django grew `topup` and `esimcard` and
these did not. Production then held rows neither enum could name — which costs
nothing while they are unused and rejects real orders the day somebody wires one
into a schema. `tests/unit/test_enum_mirror.py` pins them to the values the
database actually contains.
"""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class ESIMStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    EXPIRED = "expired"


class PaymentStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    REFUNDED = "refunded"


class DiscountType(StrEnum):
    PERCENT = "percent"
    FIXED = "fixed"


class PlanScope(StrEnum):
    LOCAL = "local"
    REGIONAL = "regional"
    GLOBAL = "global"
    #: Extra data for an eSIM somebody already owns. Never browsable: a top-up
    #: only means anything beside one specific profile, so the plan row exists
    #: to carry the invoice line and the package code, nothing else.
    TOPUP = "topup"


class ReferralStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


class ModerationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Provider(StrEnum):
    #: Not a wholesaler: rows still carry it, and checkout refuses them —
    #: nothing can be ordered against it. See `services.checkout.is_fulfillable`.
    MOCK = "mock"
    ESIMACCESS = "esimaccess"
    ESIMCARD = "esimcard"
