import json
from datetime import date, timedelta

from sqlalchemy import select

from bankai import realestate
from bankai.agent.tools import execute_tool
from bankai.ingest import upsert_account
from bankai.models import BalanceSnapshot, Property, Valuation


def make_property(session, *, sqft=1800, balance=1_460_000.0, auto_update=True):
    account = upsert_account(
        session, source="manual", name="Home — 36001 Cabrillo Dr",
        kind="property", balance=balance,
    )
    prop = Property(
        account_id=account.id, street="36001 Cabrillo Dr", city="Fremont",
        state="CA", zip_code="94536", sqft=sqft, auto_update=auto_update,
    )
    session.add(prop)
    session.flush()
    return prop


def add_comp(session, prop, price, *, sqft=None, months_ago=1, miles=0.3, **kw):
    return realestate.upsert_comp(
        session, prop, source="manual", price=price, sqft=sqft,
        sale_date=date.today() - timedelta(days=int(months_ago * 30.44)),
        distance_miles=miles,
        address=kw.pop("address", f"comp @ {price}"), **kw,
    )


# --- estimation math ---

def test_estimate_uses_ppsf_when_sqft_known(session):
    prop = make_property(session, sqft=2000)
    add_comp(session, prop, 1_500_000, sqft=2000)   # $750/sqft
    add_comp(session, prop, 1_600_000, sqft=2000)   # $800/sqft
    add_comp(session, prop, 1_400_000, sqft=2000)   # $700/sqft
    est = realestate.estimate_from_comps(prop)
    assert est["method"] == "comps_median"
    assert est["value"] == 1_500_000  # $750 median × 2000
    assert est["comp_count"] == 3


def test_recent_nearby_comps_dominate(session):
    prop = make_property(session, sqft=2000)
    add_comp(session, prop, 1_000_000, sqft=2000, months_ago=17, miles=3.0)
    add_comp(session, prop, 1_000_000, sqft=2000, months_ago=16, miles=3.0,
             address="another far old one")
    add_comp(session, prop, 1_600_000, sqft=2000, months_ago=0.5, miles=0.1)
    est = realestate.estimate_from_comps(prop)
    assert est["value"] == 1_600_000  # the fresh nearby sale outweighs two stale far ones


def test_estimate_falls_back_to_price_without_sqft(session):
    prop = make_property(session, sqft=None)
    add_comp(session, prop, 1_500_000)
    add_comp(session, prop, 1_450_000)
    est = realestate.estimate_from_comps(prop)
    assert est["price_per_sqft"] is None
    assert 1_450_000 <= est["value"] <= 1_500_000


def test_stale_comps_are_excluded(session):
    prop = make_property(session)
    add_comp(session, prop, 900_000, months_ago=30)
    assert realestate.estimate_from_comps(prop) is None


def test_comp_dedupe(session):
    prop = make_property(session)
    _, c1 = add_comp(session, prop, 1_500_000, address="123 Oak St")
    _, c2 = add_comp(session, prop, 1_500_000, address="123 Oak St")
    assert c1 and not c2
    _, c3 = realestate.upsert_comp(
        session, prop, source="rentcast", external_id="rc-1",
        address="9 Elm", price=1_200_000,
    )
    _, c4 = realestate.upsert_comp(
        session, prop, source="rentcast", external_id="rc-1",
        address="9 Elm", price=1_200_000,
    )
    assert c3 and not c4


# --- refresh ---

RENTCAST_FIXTURE = {
    "price": 1_520_000,
    "priceRangeLow": 1_440_000,
    "priceRangeHigh": 1_600_000,
    "comparables": [
        {"id": "rc-a", "formattedAddress": "36012 Cabrillo Dr", "price": 1_495_000,
         "squareFootage": 1750, "bedrooms": 4, "bathrooms": 2, "distance": 0.12,
         "removedDate": "2026-06-20T00:00:00Z", "status": "sold"},
        {"id": "rc-b", "formattedAddress": "4550 Deep Creek Rd", "price": 1_565_000,
         "squareFootage": 1900, "distance": 0.4, "listedDate": "2026-07-01", "status": "active"},
        {"id": "rc-junk", "formattedAddress": "no price street"},
    ],
}


def test_refresh_with_avm_applies_value(session):
    prop = make_property(session, auto_update=True)
    result = realestate.refresh_property(session, prop, fetcher=lambda p: RENTCAST_FIXTURE)
    assert result["comps_added"] == 2  # junk row without a price skipped
    assert result["avm_value"] == 1_520_000
    assert result["applied"] is True
    assert prop.account.balance == 1_520_000
    snaps = session.execute(
        select(BalanceSnapshot).where(BalanceSnapshot.account_id == prop.account_id)
    ).scalars().all()
    assert any(s.balance == 1_520_000 for s in snaps)
    v = session.execute(select(Valuation)).scalar_one()
    assert v.method == "avm" and v.applied
    assert json.loads(v.detail)["avm_range"] == [1_440_000, 1_600_000]


def test_refresh_without_auto_update_records_but_does_not_apply(session):
    prop = make_property(session, auto_update=False)
    realestate.refresh_property(session, prop, fetcher=lambda p: RENTCAST_FIXTURE)
    assert prop.account.balance == 1_460_000  # untouched
    v = session.execute(select(Valuation)).scalar_one()
    assert v.value == 1_520_000 and not v.applied


def test_refresh_comps_only_when_no_fetcher(session):
    prop = make_property(session, sqft=2000)
    add_comp(session, prop, 1_500_000, sqft=2000)
    result = realestate.refresh_property(session, prop, fetcher=None)
    assert result["avm_value"] is None
    assert result["value"] == 1_500_000
    assert result["applied"] is True
    v = session.execute(select(Valuation)).scalar_one()
    assert v.method == "comps_median"


def test_refresh_survives_fetch_error(session):
    prop = make_property(session, sqft=2000)
    add_comp(session, prop, 1_500_000, sqft=2000)

    def boom(p):
        raise RuntimeError("rate limited")

    result = realestate.refresh_property(session, prop, fetcher=boom)
    assert result["fetch_error"].startswith("rate limited")
    assert result["value"] == 1_500_000  # comps estimate still lands


# --- agent tools ---

def test_get_property_valuation_tool(session):
    prop = make_property(session, sqft=2000)
    add_comp(session, prop, 1_500_000, sqft=2000)
    out = json.loads(execute_tool(session, "get_property_valuation", {}))
    assert out[0]["property_id"] == prop.id
    assert out[0]["current_value"] == 1_460_000
    assert out[0]["fresh_comps_estimate"]["value"] == 1_500_000
    assert out[0]["comps"][0]["source"] == "manual"


def test_add_property_comp_tool(session):
    prop = make_property(session, sqft=2000)
    out = json.loads(execute_tool(session, "add_property_comp", {
        "property_id": prop.id, "address": "36020 Cabrillo Dr", "price": 1_530_000,
        "sale_date": date.today().isoformat(), "sqft": 2000, "distance_miles": 0.1,
    }))
    assert out["recorded"] and out["created"]
    assert out["fresh_estimate"]["value"] == 1_530_000


def test_set_property_value_tool(session):
    prop = make_property(session)
    out = json.loads(execute_tool(session, "set_property_value", {
        "property_id": prop.id, "value": 1_505_000,
        "reasoning": "three sold comps on the street averaging $840/sqft",
    }))
    assert out["updated"]
    assert out["old_value"] == 1_460_000 and out["new_value"] == 1_505_000
    assert prop.account.balance == 1_505_000
    v = session.execute(select(Valuation)).scalar_one()
    assert v.method == "agent" and v.applied and "840" in v.detail
    missing = json.loads(execute_tool(session, "set_property_value", {
        "property_id": "prop_nope", "value": 1, "reasoning": "x"}))
    assert "error" in missing
