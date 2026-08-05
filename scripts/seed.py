"""Seed demo catalog data for FastSIM.

Run:  python -m scripts.seed
Idempotent: existing rows (matched by slug/code/email) are left in place.
"""

from decimal import Decimal

from app.workers.session import SyncSessionFactory as SessionLocal
from app.core.security import hash_password
from app.db.models import (
    FAQ,
    Banner,
    Country,
    Customer,
    Plan,
    PromoCode,
    Region,
)

REGIONS = [
    ("Europe", "europe"),
    ("Asia", "asia"),
    ("Middle East", "middle-east"),
    ("North America", "north-america"),
    ("Africa", "africa"),
    ("Oceania", "oceania"),
    ("Latin America", "latin-america"),
]

# name, iso2, region_slug, popular
COUNTRIES = [
    ("Uzbekistan", "UZ", "asia", True),
    ("Turkey", "TR", "europe", True),
    ("United Arab Emirates", "AE", "middle-east", True),
    ("United States", "US", "north-america", True),
    ("Thailand", "TH", "asia", True),
    ("Japan", "JP", "asia", True),
    ("France", "FR", "europe", True),
    ("Spain", "ES", "europe", True),
    ("Germany", "DE", "europe", False),
    ("United Kingdom", "GB", "europe", True),
    ("Italy", "IT", "europe", False),
    ("Kazakhstan", "KZ", "asia", False),
    ("China", "CN", "asia", False),
    ("Saudi Arabia", "SA", "middle-east", False),
    ("Egypt", "EG", "africa", False),
    ("Singapore", "SG", "asia", False),
    ("South Korea", "KR", "asia", False),
    ("Australia", "AU", "oceania", False),
    ("Brazil", "BR", "latin-america", False),
    ("Mexico", "MX", "latin-america", False),
]

# (title suffix, data_mb, validity_days, base_price, network, unlimited, popular)
PLAN_TEMPLATES = [
    ("1 GB · 7 days", 1024, 7, "4.50", "4G", False, False),
    ("3 GB · 15 days", 3072, 15, "9.90", "5G", False, True),
    ("5 GB · 30 days", 5120, 30, "14.90", "5G", False, False),
    ("10 GB · 30 days", 10240, 30, "24.90", "5G", False, False),
    ("Unlimited · 7 days", 0, 7, "29.90", "5G", True, False),
]


def seed():
    db = SessionLocal()
    try:
        # Regions
        region_map = {}
        for i, (name, slug) in enumerate(REGIONS):
            region = db.query(Region).filter(Region.slug == slug).first()
            if not region:
                region = Region(name=name, slug=slug, sort_order=i)
                db.add(region)
                db.flush()
            region_map[slug] = region

        # Countries + plans
        for i, (name, iso2, region_slug, popular) in enumerate(COUNTRIES):
            slug = name.lower().replace(" ", "-")
            country = db.query(Country).filter(Country.slug == slug).first()
            if not country:
                country = Country(
                    name=name,
                    slug=slug,
                    iso2=iso2,
                    region_id=region_map[region_slug].id,
                    is_popular=popular,
                    is_active=True,
                    sort_order=i,
                )
                db.add(country)
                db.flush()

            if not db.query(Plan).filter(Plan.country_id == country.id).first():
                # Slightly vary price per country index for realism.
                bump = Decimal("1") + Decimal(i % 5) * Decimal("0.05")
                for j, (suffix, data_mb, days, price, net, unlimited, pop) in enumerate(
                    PLAN_TEMPLATES
                ):
                    # A seeded plan names a wholesaler and a package code.
                    #
                    # Not decoration: checkout refuses a plan no connected
                    # supplier can supply, and it refuses it because this seed
                    # once ran against production and left twenty plans on sale
                    # at $29.90-$35.88 with no supplier and no code — a card
                    # would have been charged for an eSIM that could never be
                    # issued. Development data that cannot be sold in
                    # development would hide that gate instead of exercising it.
                    #
                    # Nothing is actually bought: ESIM_PROVIDER=mock is what
                    # stops supplier calls, and the SEED- prefix makes it obvious
                    # in any log that this code is not a real package.
                    db.add(
                        Plan(
                            scope="local",
                            country_id=country.id,
                            title=f"{name} {suffix}",
                            data_amount_mb=data_mb,
                            is_unlimited=unlimited,
                            validity_days=days,
                            price_usd=(Decimal(price) * bump).quantize(Decimal("0.01")),
                            network_type=net,
                            supports_hotspot=True,
                            is_popular=pop,
                            is_active=True,
                            sort_order=j,
                            provider="esimaccess",
                            provider_package_code=(
                                f"SEED-{country.iso2}-{data_mb}-{days}"
                            ),
                        )
                    )

        # Promo codes
        for code, dtype, value in [
            ("WELCOME10", "percent", "10"),
            ("FASTSIM5", "fixed", "5"),
        ]:
            if not db.query(PromoCode).filter(PromoCode.code == code).first():
                db.add(
                    PromoCode(
                        code=code,
                        discount_type=dtype,
                        discount_value=Decimal(value),
                        max_uses=0,
                        is_active=True,
                    )
                )

        # FAQs
        faqs = [
            ("What is an eSIM?", "An eSIM is a digital SIM that lets you activate a mobile data plan without a physical card. You scan a QR code and you are connected.", "general"),
            ("How do I install my FastSIM eSIM?", "After purchase, open My eSIMs, scan the QR code with your phone camera or add it in Settings > Cellular > Add eSIM. Activate it when you arrive at your destination.", "setup"),
            ("Is my phone compatible?", "Most phones released after 2018 support eSIM (iPhone XS+, Pixel 3+, recent Samsung Galaxy S/Note/Z). Your device must also be carrier-unlocked.", "device"),
            ("Which payment methods do you accept?", "This demo build uses a mock payment gateway. Production will support international cards and local providers.", "billing"),
            ("Can I top up my plan?", "Yes — when your data runs low you can purchase an add-on without changing your eSIM.", "general"),
        ]
        for k, (q, a, cat) in enumerate(faqs):
            if not db.query(FAQ).filter(FAQ.question == q).first():
                db.add(FAQ(question=q, answer=a, category=cat, sort_order=k, is_active=True))

        # Banner
        if not db.query(Banner).filter(Banner.title == "Football 2026 data deals").first():
            db.add(
                Banner(
                    title="Football 2026 data deals",
                    subtitle="Stay connected across every host city. Plans from $4.50.",
                    cta_text="Explore plans",
                    cta_link="/destinations",
                    is_active=True,
                    sort_order=0,
                )
            )

        # Demo customer
        if not db.query(Customer).filter(Customer.email == "demo@fastsim.dev").first():
            db.add(
                Customer(
                    email="demo@fastsim.dev",
                    full_name="Demo Traveller",
                    hashed_password=hash_password("demo12345"),
                    is_active=True,
                )
            )

        db.commit()
        counts = {
            "regions": db.query(Region).count(),
            "countries": db.query(Country).count(),
            "plans": db.query(Plan).count(),
            "promo_codes": db.query(PromoCode).count(),
            "faqs": db.query(FAQ).count(),
        }
        print("Seed complete:", counts)
    finally:
        db.close()


if __name__ == "__main__":
    seed()
