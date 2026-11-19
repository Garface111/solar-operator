"""Real-estate tracking: comps in a property's neighborhood drive its value.

The estimate is a weighted median of comp $/sqft (falling back to raw prices when
sqft is unknown), weighted by recency and proximity — recent nearby sales dominate.
Comps arrive three ways: the RentCast AVM API (RENTCAST_API_KEY, free tier),
the dashboard, or the copilot recording a sale it was told about. A refresh with
auto_update on applies the value to the property's account (snapshotted), so net
worth and its history track the market.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .ingest import _snapshot_balance
from .models import Comp, Property, Valuation

MAX_COMP_AGE_MONTHS = 18
RENTCAST_URL = "https://api.rentcast.io/v1/avm/value"


def _months_ago(d: date | None) -> float:
    if d is None:
        return MAX_COMP_AGE_MONTHS / 2  # unknown date: mid-weight, not dominant
    return max(0.0, (date.today() - d).days / 30.44)


def _weight(comp: Comp) -> float:
    recency = 1.0 / (1.0 + _months_ago(comp.sale_date) / 6.0)
    distance = 1.0 / (1.0 + (comp.distance_miles if comp.distance_miles is not None else 1.0))
    return recency * distance


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    """pairs = (value, weight); returns the value at 50% cumulative weight."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    cum = 0.0
    for value, w in pairs:
        cum += w
        if cum >= total / 2:
            return value
    return pairs[-1][0]


def usable_comps(prop: Property) -> list[Comp]:
    cutoff = date.today() - timedelta(days=int(MAX_COMP_AGE_MONTHS * 30.44))
    return [
        c for c in prop.comps
        if c.price and c.price > 0 and (c.sale_date is None or c.sale_date >= cutoff)
    ]


def estimate_from_comps(prop: Property) -> dict | None:
    """Weighted-median estimate; returns None when there are no usable comps."""
    comps = usable_comps(prop)
    if not comps:
        return None
    with_sqft = [c for c in comps if c.sqft and prop.sqft]
    if with_sqft:
        ppsf = _weighted_median([(c.price / c.sqft, _weight(c)) for c in with_sqft])
        return {
            "value": round(ppsf * prop.sqft, -3),
            "method": "comps_median",
            "basis": f"${ppsf:,.0f}/sqft weighted median of {len(with_sqft)} comps"
                     f" × {prop.sqft} sqft",
            "comp_count": len(with_sqft),
            "price_per_sqft": round(ppsf, 2),
        }
    value = _weighted_median([(c.price, _weight(c)) for c in comps])
    return {
        "value": round(value, -3),
        "method": "comps_median",
        "basis": f"weighted median price of {len(comps)} comps (no sqft data)",
        "comp_count": len(comps),
        "price_per_sqft": None,
    }


def upsert_comp(
    session: Session,
    prop: Property,
    *,
    source: str,
    address: str,
    price: float,
    external_id: str | None = None,
    status: str = "sold",
    sale_date: date | None = None,
    sqft: int | None = None,
    beds: float | None = None,
    baths: float | None = None,
    distance_miles: float | None = None,
) -> tuple[Comp, bool]:
    """Dedupe by external_id when present, else by (address, price)."""
    existing = None
    if external_id:
        existing = session.execute(
            select(Comp).where(Comp.property_id == prop.id, Comp.external_id == external_id)
        ).scalar_one_or_none()
    if existing is None:
        existing = session.execute(
            select(Comp).where(
                Comp.property_id == prop.id,
                Comp.address == address.strip(),
                Comp.price == price,
            )
        ).scalar_one_or_none()
    if existing:
        existing.status = status or existing.status
        existing.sale_date = sale_date or existing.sale_date
        existing.sqft = sqft or existing.sqft
        existing.distance_miles = (
            distance_miles if distance_miles is not None else existing.distance_miles
        )
        existing.fetched_at = datetime.utcnow()
        return existing, False
    comp = Comp(
        property_id=prop.id, source=source, external_id=external_id,
        address=address.strip(), status=status, price=price, sale_date=sale_date,
        sqft=sqft, beds=beds, baths=baths, distance_miles=distance_miles,
    )
    session.add(comp)
    session.flush()
    session.expire(prop, ["comps"])  # so an estimate right after sees the new comp
    return comp, True


def fetch_rentcast(prop: Property) -> dict | None:
    """AVM value + comparables from RentCast. Returns None when no key configured."""
    if not config.RENTCAST_API_KEY:
        return None
    address = f"{prop.street}, {prop.city}, {prop.state} {prop.zip_code}".strip()
    params: dict = {"address": address, "compCount": 20}
    if prop.sqft:
        params["squareFootage"] = prop.sqft
    resp = httpx.get(
        RENTCAST_URL, params=params,
        headers={"X-Api-Key": config.RENTCAST_API_KEY}, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def apply_value(session: Session, prop: Property, valuation: Valuation) -> None:
    account = prop.account
    account.balance = valuation.value
    account.balance_date = datetime.utcnow()
    session.flush()
    _snapshot_balance(session, account)
    valuation.applied = True


def refresh_property(session: Session, prop: Property, fetcher=fetch_rentcast) -> dict:
    """Pull fresh comps (when a fetcher/key is available), re-estimate, and — with
    auto_update on — apply the value to the account. Always records a Valuation."""
    comps_added = 0
    avm_value = None
    avm_range = None
    fetch_error = None
    try:
        data = fetcher(prop) if fetcher else None
    except Exception as exc:
        data = None
        fetch_error = str(exc)[:300]
    if data:
        avm_value = data.get("price")
        if data.get("priceRangeLow") and data.get("priceRangeHigh"):
            avm_range = [data["priceRangeLow"], data["priceRangeHigh"]]
        for c in data.get("comparables") or []:
            raw_date = c.get("removedDate") or c.get("listedDate") or c.get("lastSeenDate")
            sale_date = None
            if raw_date:
                try:
                    sale_date = date.fromisoformat(str(raw_date)[:10])
                except ValueError:
                    sale_date = None
            price = c.get("price")
            addr = c.get("formattedAddress") or c.get("addressLine1")
            if not price or not addr:
                continue
            _, created = upsert_comp(
                session, prop, source="rentcast", external_id=c.get("id"),
                address=addr, price=float(price),
                status=(c.get("status") or "sold").lower(),
                sale_date=sale_date,
                sqft=c.get("squareFootage"), beds=c.get("bedrooms"),
                baths=c.get("bathrooms"), distance_miles=c.get("distance"),
            )
            comps_added += int(created)
        session.flush()
        session.refresh(prop)
    estimate = estimate_from_comps(prop)
    value = avm_value or (estimate["value"] if estimate else None)
    result: dict = {
        "property_id": prop.id,
        "comps_added": comps_added,
        "comps_total": len(usable_comps(prop)),
        "estimate": estimate,
        "avm_value": avm_value,
        "value": value,
        "applied": False,
    }
    if fetch_error:
        result["fetch_error"] = fetch_error
    if value is None:
        return result
    detail = {
        "avm_value": avm_value, "avm_range": avm_range,
        "estimate": estimate, "comps_total": result["comps_total"],
    }
    valuation = Valuation(
        property_id=prop.id, value=float(value),
        method="avm" if avm_value else "comps_median",
        detail=json.dumps(detail, default=str),
    )
    session.add(valuation)
    session.flush()
    if prop.auto_update:
        apply_value(session, prop, valuation)
        result["applied"] = True
    return result
