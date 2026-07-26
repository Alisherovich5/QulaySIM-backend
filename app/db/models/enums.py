"""String enums mirroring Django's TextChoices.

Django enforces these at the ORM layer only — there are no CHECK constraints
in the database — so this service validates them on the way in as well.
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


class ReferralStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


class ModerationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Provider(StrEnum):
    MOCK = "mock"
    ESIMACCESS = "esimaccess"
