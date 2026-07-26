"""Synchronise QulaySIM local plans from eSIM Access.

Usage:
    ./venv/bin/python scripts/sync_esimaccess_catalog.py --dry-run
    ./venv/bin/python scripts/sync_esimaccess_catalog.py --apply --replace-local

The command deliberately imports only local, fixed-volume plans for countries
already present in our catalogue. Regional/global and daily-pass products need
their own storefront presentation, so they are not silently mixed into a
country's local-plan page.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from math import log2
from pathlib import Path

from sqlalchemy.orm import joinedload

# Allow `python scripts/...` without requiring callers to set PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import Country, Plan
from app.integrations.esim_access import EsimAccessClient
from app.workers.session import SyncSessionFactory as SessionLocal

TARGETS = ((1, 7), (3, 15), (5, 30), (10, 30), (20, 30))
BYTES_PER_GB = 1024 * 1024 * 1024


def _local_fixed_packages(packages: list[dict], iso2: str) -> list[dict]:
    return [
        package
        for package in packages
        if str(package.get("location", "")).strip().upper() == iso2
        and int(package.get("dataType") or 0) == 1
        and package.get("slug")
    ]


def _choose_packages(packages: list[dict]) -> list[dict]:
    """Select five familiar data/validity tiers without duplicate supplier SKUs."""
    selected: list[dict] = []
    used: set[str] = set()
    for target_gb, target_days in TARGETS:
        candidates = [p for p in packages if str(p.get("slug")) not in used]
        if not candidates:
            break

        def score(package: dict) -> tuple[float, int]:
            volume_gb = max(int(package.get("volume") or 0) / BYTES_PER_GB, 0.01)
            duration = int(package.get("duration") or 0)
            return (abs(log2(volume_gb / target_gb)) * 20 + abs(duration - target_days), int(package.get("price") or 0))

        choice = min(candidates, key=score)
        selected.append(choice)
        used.add(str(choice["slug"]))
    return selected


def _usd(value: object) -> Decimal:
    """eSIM Access sends integer USD × 10,000."""
    return (Decimal(str(value or 0)) / Decimal(10_000)).quantize(Decimal("0.01"))


def _cost_usd(package: dict) -> Decimal:
    """What the supplier charges us.

    This used to write `retailPrice` straight into the selling price, because
    there was no markup engine and selling at supplier cost would have meant
    zero margin. Now the cost is recorded as cost and the markup rules in the
    admin decide the selling price — so `price` is the correct field to read.
    """
    return _usd(package.get("price") or package.get("retailPrice"))


def _suggested_retail_usd(package: dict) -> Decimal:
    """The supplier's own suggested retail, used only to seed brand-new plans
    until the pricing rules are applied."""
    return _usd(package.get("retailPrice") or package.get("price"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes to the database")
    parser.add_argument("--dry-run", action="store_true", help="preview changes (the default)")
    parser.add_argument(
        "--replace-local",
        action="store_true",
        help="remove existing local plans not supplied by eSIM Access (requires --apply)",
    )
    args = parser.parse_args()
    if args.replace_local and not args.apply:
        parser.error("--replace-local requires --apply")

    packages = (EsimAccessClient().list_packages().get("obj") or {}).get("packageList") or []
    with SessionLocal() as db:
        countries = db.query(Country).options(joinedload(Country.plans)).order_by(Country.id).all()
        changes = 0
        for country in countries:
            choices = _choose_packages(_local_fixed_packages(packages, country.iso2.upper()))
            existing = {plan.provider_package_code: plan for plan in country.plans if plan.scope == "local"}
            selected_slugs = set()
            for package in choices:
                slug = str(package["slug"])
                selected_slugs.add(slug)
                volume_mb = round(int(package.get("volume") or 0) / (1024 * 1024))
                duration = int(package.get("duration") or 0)
                plan = existing.get(slug)
                fields = {
                    "scope": "local",
                    "country_id": country.id,
                    "region_id": None,
                    "title": str(package.get("name") or slug),
                    "data_amount_mb": volume_mb,
                    "is_unlimited": False,
                    "validity_days": duration,
                    "cost_usd": _cost_usd(package),
                    "network_type": "5G" if "5G" in str(package.get("speed") or "") else "4G",
                    "supports_hotspot": True,
                    "is_popular": len(selected_slugs) == 3,
                    "is_active": True,
                    "provider": "esimaccess",
                    "provider_package_code": slug,
                }
                if plan is None:
                    # Seed the selling price from the supplier's suggested
                    # retail so a new plan is never briefly free; the markup
                    # rules replace it on the next recalculation.
                    plan = Plan(
                        **fields,
                        price_usd=_suggested_retail_usd(package),
                        sort_order=len(selected_slugs),
                    )
                    db.add(plan)
                else:
                    # Deliberately does NOT touch price_usd: overwriting it
                    # would wipe the markup configured in the admin on every
                    # sync. Django recalculates it from the new cost.
                    for field, value in fields.items():
                        setattr(plan, field, value)
                changes += 1

            if args.replace_local:
                for plan in country.plans:
                    if plan.scope == "local" and plan.provider_package_code not in selected_slugs:
                        db.delete(plan)
                        changes += 1
            print(f"{country.iso2}: {len(choices)} provider plans")

        if args.apply:
            db.commit()
            print(
                "\nCosts written. Apply the markup rules with:\n"
                "  cd ../QulaySIM-admin && python manage.py recalculate_prices"
            )
            print(f"Applied {changes} catalogue changes.")
        else:
            db.rollback()
            print(f"Dry run: {changes} catalogue changes would be applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
