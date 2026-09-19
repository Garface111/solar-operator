from datetime import date
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from api.db import SessionLocal
from api.models import BillingReportSubscription, DailyGeneration, Bill
from api.billing.delivery import _complete_generation, _array_period_kwh_sourced
from api.billing.routes import _validate_budget, _validate_rate
from tests.test_offtaker_upload import _make_tenant, _make_array_with_bill

BASE = "/v1/array-operator/billing"

@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("validator", [_validate_budget, _validate_rate])
def test_nonfinite_money_rejected(value, validator):
    with pytest.raises(HTTPException):
        validator(value)


def test_closed_month_historical_and_missing_day():
    rows = [(date(2026, 5, d), 100, "daily_csv") for d in range(1, 32)]
    rows += [(date(2026, 6, d), 200, "daily_csv") for d in range(1, 31)]
    assert _complete_generation(rows, "2026-05")[0] == 3100
    assert _complete_generation(rows, "2026-06")[0] == 6000
    assert _complete_generation(rows[:-1], "2026-06")[0] is None
    assert _complete_generation([(date.today(), 10, "daily_csv")])[0] is None


def test_gmp_reader_does_not_trust_partial_month(monkeypatch):
    from api.reports import gmp_daily_read as gdr
    monkeypatch.setattr(gdr, "get_daily_series", lambda *a, **kw:
        [{"day": date(2026, 5, d), "kwh": 100} for d in range(1, 11)])
    with SessionLocal() as db:
        assert _array_period_kwh_sourced(db, -987654, "2026-05")[0] is None


def test_concurrent_imports_cannot_overallocate(client):
    tid, auth = _make_tenant()
    aid, ua = _make_array_with_bill(tid, "Concurrency", "CONC", with_bill=True)
    gate = Barrier(2)
    def create(name):
        gate.wait(timeout=5)
        return client.post(BASE + "/subscriptions/bulk-commit", headers={"Authorization": auth},
            json={"rows": [{"offtaker_name": name, "array_id": aid,
                "utility_account_id": ua, "allocation_pct": .6}]}).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(create, ["A", "B"]))
    assert sorted([a["created"], b["created"]]) == [0, 1]
    with SessionLocal() as db:
        rows = db.execute(select(BillingReportSubscription).where(
            BillingReportSubscription.tenant_id == tid)).scalars().all()
        assert len(rows) == 1 and rows[0].allocation_pct == .6


def test_revised_bill_retains_evidence_and_ignores_old_replay():
    from api.bill_revisions import apply_bill_revision
    bill = Bill(kwh_generated=1000, solar_credit_usd=200)
    apply_bill_revision(bill, {"solar_credit_usd": 200}, source="pdf", evidence="original")
    apply_bill_revision(bill, {"solar_credit_usd": 150}, source="pdf", evidence="corrected")
    assert bill.solar_credit_usd == 150
    assert bill.raw_json["_ao_evidence_revisions"][-1]["before"]["solar_credit_usd"] == 200
    assert not apply_bill_revision(bill, {"solar_credit_usd": 200}, source="pdf", evidence="original")
    assert bill.solar_credit_usd == 150


def test_explicit_column_mapping_honors_header_row(client, monkeypatch):
    from api.billing import routes
    tid, auth = _make_tenant()
    _make_array_with_bill(tid, "Maple", "MAPLE", with_bill=True)
    monkeypatch.setattr(routes, "_roster_rows", lambda *a, **kw: [
        ["Annual roster", "", ""], ["Array", "Offtaker", "Share %"],
        ["Maple", "Alice", "25"]])
    result = client.post(BASE + "/subscriptions/bulk-import",
        headers={"Authorization": auth}, files={"file": ("roster.csv", b"placeholder", "text/csv")},
        data={"column_map": json.dumps({"array": 0, "name": 1, "percent": 2}), "header_row": "1"})
    assert result.status_code == 200, result.text
    assert len(result.json()["rows"]) == 1
    assert result.json()["detection"]["header_row"] == 1


def test_array_group_cannot_overallocate_across_subaccounts(client):
    from api.models import UtilityAccount
    tid, auth = _make_tenant()
    aid, host = _make_array_with_bill(tid, "Group", "HOST", with_bill=True)
    with SessionLocal() as db:
        subacct = UtilityAccount(tenant_id=tid, array_id=aid, provider="gmp", account_number="SUB")
        db.add(subacct); db.commit(); sub_id = subacct.id
    response = client.post(BASE + "/subscriptions/bulk-commit", headers={"Authorization": auth},
        json={"rows": [{"offtaker_name": "Host share", "array_id": aid, "utility_account_id": host, "allocation_pct": .6},
                       {"offtaker_name": "Sub share", "array_id": aid, "utility_account_id": sub_id, "allocation_pct": .6}]})
    assert response.json()["created"] == 0
    assert "array group" in response.json()["failed"][0]["error"]


def test_cash_rate_outside_guard_band_is_not_billable():
    from api.rate_schedule import resolve_offtaker_excess_credit
    tid, auth = _make_tenant()
    aid, ua = _make_array_with_bill(tid, "Bad cash", "CASH", with_bill=True)
    with SessionLocal() as db:
        bill = db.execute(select(Bill).where(Bill.account_id == ua)).scalars().first()
        bill.kwh_sent_to_grid = 1000
        bill.solar_credit_usd = 10000
        db.commit()
        assert resolve_offtaker_excess_credit(db, ua) is None
