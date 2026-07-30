"""All ORM models. Importing this package registers every mapper."""

from app.db.models.catalog import Country, Plan, Region, SupplierOffer
from app.db.models.content import (
    FAQ,
    Banner,
    Benefit,
    Device,
    PromoBanner,
    Testimonial,
)
from app.db.models.customers import Customer, SocialAccount, Referral
from app.db.models.enums import (
    DiscountType,
    ESIMStatus,
    ModerationStatus,
    OrderStatus,
    PaymentStatus,
    PlanScope,
    Provider,
    ReferralStatus,
)
from app.db.models.orders import (
    ESIM,
    Order,
    OrderItem,
    Payment,
    PaymeTransaction,
    PromoCode,
)

__all__ = [
    "ESIM",
    "FAQ",
    "Banner",
    "Benefit",
    "Country",
    "Customer",
    "SocialAccount",
    "Device",
    "DiscountType",
    "ESIMStatus",
    "ModerationStatus",
    "Order",
    "OrderItem",
    "OrderStatus",
    "PaymeTransaction",
    "Payment",
    "PaymentStatus",
    "Plan",
    "SupplierOffer",
    "PlanScope",
    "PromoBanner",
    "PromoCode",
    "Provider",
    "Referral",
    "ReferralStatus",
    "Region",
    "Testimonial",
]
