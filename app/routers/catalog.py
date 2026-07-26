from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.models import Country, Plan, Region
from app.schemas import CountryDetailOut, CountryOut, PlanOut, RegionOut

router = APIRouter(prefix="/api", tags=["catalog"])


def _country_out(country: Country) -> CountryOut:
    data = CountryOut.model_validate(country)
    prices = [p.price_usd for p in country.plans if p.is_active]
    data.starting_price = float(min(prices)) if prices else None
    return data


@router.get("/regions", response_model=list[RegionOut])
def list_regions(db: Session = Depends(get_db)):
    return db.query(Region).order_by(Region.sort_order, Region.name).all()


@router.get("/countries", response_model=list[CountryOut])
def list_countries(
    db: Session = Depends(get_db),
    search: str | None = Query(default=None),
    popular: bool | None = Query(default=None),
    region: str | None = Query(default=None, description="region slug"),
):
    query = (
        db.query(Country)
        .options(joinedload(Country.plans), joinedload(Country.region))
        .filter(Country.is_active == True)  # noqa: E712
    )
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(or_(Country.name.ilike(like), Country.iso2.ilike(like)))
    if popular is not None:
        query = query.filter(Country.is_popular == popular)
    if region:
        query = query.join(Region).filter(Region.slug == region)
    countries = query.order_by(Country.sort_order, Country.name).all()
    return [_country_out(c) for c in countries]


@router.get("/countries/{slug}", response_model=CountryDetailOut)
def country_detail(slug: str, db: Session = Depends(get_db)):
    country = (
        db.query(Country)
        .options(joinedload(Country.plans), joinedload(Country.region))
        .filter(Country.slug == slug)
        .first()
    )
    if not country:
        raise HTTPException(status_code=404, detail="Country not found")
    detail = CountryDetailOut.model_validate(country)
    active_plans = [p for p in country.plans if p.is_active]
    detail.plans = [PlanOut.model_validate(p) for p in active_plans]
    prices = [p.price_usd for p in active_plans]
    detail.starting_price = float(min(prices)) if prices else None
    return detail


@router.get("/plans/{plan_id}", response_model=PlanOut)
def plan_detail(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(Plan, plan_id)
    if not plan or not plan.is_active:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan
