"""Pull a supplier's catalogue and price it against the two we already use.

Why this exists: every supplier quotes prices in its own shape — USD×10000,
cents, strings, per-plan or per-day, bytes or "GB" in the title. Comparing by
eye is how we nearly published wrong numbers once. This normalises any
catalogue to one row shape and prints the only three figures that decide
whether a supplier is worth adding:

    cheapest true-unlimited $/day · cheapest daily-allowance $/day · $/GB

Usage:
    ./.venv/bin/python scripts/probe_supplier.py --baseline
    ./.venv/bin/python scripts/probe_supplier.py --spec specs/foo.json
    ./.venv/bin/python scripts/probe_supplier.py --baseline --spec specs/foo.json --country TR

A new supplier needs no Python — write a JSON spec (see SPEC_HELP) pointing at
its catalogue endpoint and naming the fields. Credentials are read from the
environment, never written into the spec, so a spec file is safe to commit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

# Allow `python scripts/...` without requiring callers to set PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integrations.esim_access import EsimAccessClient
from app.integrations.esimcard import EsimCardClient

SPEC_HELP = """\
{
  "name": "example",
  "url": "https://api.example.com/v1/packages",
  "method": "GET",
  "headers": {"Authorization": "Bearer ${EXAMPLE_TOKEN}"},
  "list_path": "data.packages",
  "fields": {
    "country": "location",
    "title": "name",
    "days": "validity_days",
    "bytes": "volume",
    "price": "price_usd"
  },
  "price_scale": 1,
  "unlimited_when": {"field": "volume", "equals": -1}
}

${VAR} in headers/url is substituted from the environment. `price_scale` divides
the raw price (use 10000 for USD×10000, 100 for cents). `list_path` is a
dot-path to the array; omit it when the response body is the array itself.
"""

BYTES_PER_GB = 1024 * 1024 * 1024
# Measured 2026-09-22 from live API probes; the bar a third supplier must clear.
BENCHMARK = {
    "unlimited": ("eSIMCard", Decimal("0.57")),
    "daily": ("eSIMAccess", Decimal("0.30")),
}


@dataclass(frozen=True)
class Offer:
    """One purchasable package, normalised across suppliers."""

    supplier: str
    country: str
    title: str
    days: int
    gb: Decimal | None  # None = no volume cap (true unlimited)
    usd: Decimal

    @property
    def usd_per_day(self) -> Decimal | None:
        if self.days <= 0:
            return None
        return (self.usd / self.days).quantize(Decimal("0.001"))

    @property
    def usd_per_gb(self) -> Decimal | None:
        if not self.gb:
            return None
        return (self.usd / self.gb).quantize(Decimal("0.001"))

    @property
    def kind(self) -> str:
        """`unlimited` only when nothing caps the volume.

        A daily-allowance plan is sold as "unlimited" by most suppliers but
        throttles after a per-day quota, so it is never priced against true
        unlimited here — that conflation is what makes catalogues look cheaper
        than they are.
        """
        if self.gb is None:
            return "daily" if _DAILY_HINT.search(self.title) else "unlimited"
        return "fixed"


_DAILY_HINT = re.compile(r"(\d+\s*(gb|mb)\s*/?\s*(per\s*)?day|daily|fup|kbps)", re.I)


def _dig(payload: Any, path: str) -> Any:
    for part in filter(None, path.split(".")):
        if isinstance(payload, dict):
            payload = payload.get(part)
        elif isinstance(payload, list) and part.isdigit():
            payload = payload[int(part)] if int(part) < len(payload) else None
        else:
            return None
    return payload


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _expand(text: str) -> str:
    """Substitute ${VAR} from the environment, refusing to leave one empty."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if not value:
            raise SystemExit(f"{name} muhit o'zgaruvchisi yo'q — kalitni export qiling")
        return value

    return re.sub(r"\$\{(\w+)\}", replace, text)


def from_spec(spec_path: Path) -> list[Offer]:
    spec = json.loads(spec_path.read_text())
    name = spec.get("name") or spec_path.stem
    headers = {k: _expand(v) for k, v in (spec.get("headers") or {}).items()}
    response = httpx.request(
        spec.get("method", "GET"),
        _expand(spec["url"]),
        headers=headers,
        json=spec.get("body"),
        timeout=60,
    )
    response.raise_for_status()
    rows = _dig(response.json(), spec.get("list_path", "")) or response.json()
    if not isinstance(rows, list):
        raise SystemExit(f"{name}: list_path massivga olib bormadi — javob {type(rows).__name__}")

    fields = spec["fields"]
    scale = Decimal(str(spec.get("price_scale", 1)))
    unlimited = spec.get("unlimited_when") or {}
    offers: list[Offer] = []
    for row in rows:
        price = _decimal(_dig(row, fields["price"]))
        days = _decimal(_dig(row, fields["days"]))
        if price is None or days is None:
            continue
        raw_bytes = _dig(row, fields.get("bytes", ""))
        is_unlimited = bool(unlimited) and _dig(row, unlimited["field"]) == unlimited["equals"]
        gb = None
        if not is_unlimited:
            volume = _decimal(raw_bytes)
            gb = (
                (volume / BYTES_PER_GB).quantize(Decimal("0.01")) if volume and volume > 0 else None
            )
        offers.append(
            Offer(
                supplier=name,
                country=str(_dig(row, fields.get("country", "")) or "—").upper()[:12],
                title=str(_dig(row, fields.get("title", "")) or "—"),
                days=int(days),
                gb=gb,
                usd=(price / scale).quantize(Decimal("0.0001")),
            )
        )
    return offers


def from_esimaccess() -> list[Offer]:
    client = EsimAccessClient()
    if not client.is_configured:
        print("eSIMAccess sozlanmagan — o'tkazib yuborildi", file=sys.stderr)
        return []
    packages = client.list_packages().get("obj", {}).get("packageList", []) or []
    offers = []
    for package in packages:
        price = _decimal(package.get("price"))
        days = _decimal(package.get("duration"))
        if price is None or days is None or days <= 0:
            continue
        volume = _decimal(package.get("volume"))
        daily = int(package.get("dataType") or 0) == 2
        gb = None if daily else ((volume / BYTES_PER_GB) if volume else None)
        offers.append(
            Offer(
                supplier="eSIMAccess",
                country=str(package.get("location") or "—").upper()[:12],
                title=str(package.get("name") or "—"),
                days=int(days),
                gb=gb.quantize(Decimal("0.01")) if gb else None,
                # eSIM Access quotes USD × 10,000.
                usd=(price / Decimal(10000)).quantize(Decimal("0.0001")),
            )
        )
    return offers


def from_esimcard() -> list[Offer]:
    client = EsimCardClient()
    if not client.is_configured:
        print("eSIMCard sozlanmagan — o'tkazib yuborildi", file=sys.stderr)
        return []
    response = client._request("GET", "/package/list")
    rows = response.get("data") or response.get("packages") or []
    offers = []
    for row in rows:
        price = _decimal(row.get("price") or row.get("wholesale_price"))
        days = _decimal(row.get("validity") or row.get("day"))
        if price is None or days is None or days <= 0:
            continue
        quantity = _decimal(row.get("data_quantity"))
        unlimited = quantity is not None and quantity < 0
        unit = str(row.get("data_unit") or "GB").upper()
        gb = None
        if not unlimited and quantity and quantity > 0:
            gb = quantity if unit.startswith("GB") else quantity / Decimal(1024)
        offers.append(
            Offer(
                supplier="eSIMCard",
                country=str(row.get("country") or row.get("location") or "—").upper()[:12],
                title=str(row.get("name") or row.get("title") or "—"),
                days=int(days),
                gb=gb.quantize(Decimal("0.01")) if gb else None,
                usd=price.quantize(Decimal("0.0001")),
            )
        )
    return offers


def report(offers: list[Offer], country: str | None) -> None:
    if country:
        offers = [o for o in offers if o.country == country.upper()]
    if not offers:
        print("Hech qanday taklif topilmadi.")
        return

    by_supplier: dict[str, list[Offer]] = defaultdict(list)
    for offer in offers:
        by_supplier[offer.supplier].append(offer)

    print(f"\n{"ta'minotchi":<16}{'tur':<11}{'eng arzon':>11}  {'davlat':<8}{'tarif'}")
    print("-" * 88)
    for supplier, rows in sorted(by_supplier.items()):
        for kind in ("unlimited", "daily", "fixed"):
            group = [o for o in rows if o.kind == kind]
            if not group:
                continue
            if kind == "fixed":
                priced = [o for o in group if o.usd_per_gb]
                if not priced:
                    continue
                best = min(priced, key=lambda o: o.usd_per_gb)
                figure = f"${best.usd_per_gb}/GB"
            else:
                priced = [o for o in group if o.usd_per_day]
                if not priced:
                    continue
                best = min(priced, key=lambda o: o.usd_per_day)
                figure = f"${best.usd_per_day}/kun"
            label = {"unlimited": "cheksiz", "daily": "kunlik-FUP", "fixed": "hajmli"}[kind]
            print(f"{supplier:<16}{label:<11}{figure:>11}  {best.country:<8}{best.title[:36]}")

    print("\nMezon (2026-09-22 o'lchovi):")
    for kind, (holder, price) in BENCHMARK.items():
        label = "cheksiz" if kind == "unlimited" else "kunlik-FUP"
        beaten = [
            supplier
            for supplier, rows in by_supplier.items()
            if supplier != holder
            and any(o.kind == kind and o.usd_per_day and o.usd_per_day < price for o in rows)
        ]
        verdict = f"yutdi: {', '.join(beaten)}" if beaten else "hech kim yutmadi"
        print(f"  {label:<11} ${price}/kun ({holder}) — {verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--spec",
        type=Path,
        action="append",
        default=[],
        help="yangi ta'minotchi JSON spetsifikatsiyasi",
    )
    parser.add_argument(
        "--baseline", action="store_true", help="eSIMAccess va eSIMCard'ni ham torting"
    )
    parser.add_argument("--country", help="faqat shu ISO2 davlat")
    parser.add_argument(
        "--spec-help", action="store_true", help="spetsifikatsiya namunasini chop eting"
    )
    args = parser.parse_args()

    if args.spec_help:
        print(SPEC_HELP)
        return 0
    if not args.spec and not args.baseline:
        parser.error("--baseline yoki --spec kerak")

    offers: list[Offer] = []
    if args.baseline:
        offers += from_esimaccess()
        offers += from_esimcard()
    for spec in args.spec:
        offers += from_spec(spec)

    report(offers, args.country)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
