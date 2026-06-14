#!/usr/bin/env python3
"""SmartHub generation-channel verification harness.

THE QUESTION (the one unverified assumption gating ~471 SmartHub utilities):
  Which net-metering channel is the TRUE solar generation kWh?
  The adapter assumes  RETURN = generation,  FORWARD = consumption,  NET = combined
  (api/adapters/smarthub.fetch_daily_generation). This confirms or corrects that
  on ONE real account — and because every SmartHub deployment shares the same
  NISC API, confirming it once generalizes to all of them.

USAGE — after an operator has signed into the co-op's SmartHub via the extension
(so a UtilitySession + UtilityAccount are captured for that login):

  python -m scripts.verify_smarthub_generation --account <acctNbr> [--days 120]
  python -m scripts.verify_smarthub_generation --tenant  <ten_id>   # every smarthub acct

It selects the token bound to THIS account's login (api/sessions.session_for_account
— the per-login fix), pulls the raw utility-usage poll, and prints every meter's
flowDirection + total kWh side by side with the adapter's parsed
generation/consumption/net and the billing-overview totalUsage (consumption). You
can then SEE which channel is generation and whether RETURN is correct.

Read-only: it only GETs / usage-polls SmartHub and never writes to SmartHub. The
only DB write is caching a discovered serviceLocationNumber on the account, the
same thing the live pull job already does.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select

from api.db import SessionLocal
from api.models import UtilityAccount
from api.sessions import session_for_account
from api.jobs.smarthub_pull import _build_session_dict, _get_smarthub_host
from api.adapters.smarthub import (
    fetch_account_list,
    fetch_daily_generation,
    _base_url,
    _auth_headers,
)


def _smarthub_accounts(db, *, account: str | None, tenant: str | None) -> list[UtilityAccount]:
    """Captured SmartHub-family accounts matching the filter. SmartHub providers
    are anything with a known host (excludes gmp / non-smarthub)."""
    q = select(UtilityAccount).where(UtilityAccount.deleted_at.is_(None))
    if account:
        q = q.where(UtilityAccount.account_number == account)
    if tenant:
        q = q.where(UtilityAccount.tenant_id == tenant)
    rows = db.execute(q).scalars().all()
    return [a for a in rows if _get_smarthub_host(a.provider)]


def _resolve_service_location(db, acct: UtilityAccount, host: str, session: dict) -> str | None:
    """serviceLocationNumber from account.extra, else discover + cache it."""
    sl = (acct.extra or {}).get("service_location_number")
    if sl:
        return sl
    try:
        locations = fetch_account_list(host, session)
    except Exception as exc:  # noqa: BLE001 — diagnostic, report and move on
        print(f"    ! fetch_account_list failed: {type(exc).__name__}: {exc}")
        return None
    for loc in locations:
        if loc.get("account_number") == acct.account_number:
            sl = loc.get("service_location_number")
            break
    if not sl and locations:
        sl = locations[0].get("service_location_number")
    if sl:
        acct.extra = {**(acct.extra or {}), "service_location_number": sl}
        db.flush()
    return sl


def _raw_channel_breakdown(host, session, service_location, account_number, start, end) -> dict:
    """Replay the same utility-usage poll the adapter uses, but return the RAW
    per-flowDirection totals (no max()/NET-derivation massaging) so we can see
    exactly what each channel reports."""
    url = f"{_base_url(host)}/services/secured/utility-usage/poll"
    start_ms = int(datetime(start.year, start.month, start.day).timestamp() * 1000)
    end_ms = int(datetime(end.year, end.month, end.day, 23, 59, 59).timestamp() * 1000)
    body = {
        "timeFrame": "DAILY", "userId": session["email"], "screen": "USAGE_EXPLORER",
        "includeDemand": False, "serviceLocationNumber": service_location,
        "accountNumber": account_number, "industries": ["ELECTRIC"],
        "startDateTime": str(start_ms), "endDateTime": str(end_ms),
    }
    headers = _auth_headers(session["email"], session["auth_token"])
    data: dict = {}
    for attempt in range(3):
        resp = httpx.post(url, json=body, headers=headers, follow_redirects=True, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "COMPLETE":
            break
    electric = (data.get("data") or {}).get("ELECTRIC") or []
    usage = next((e for e in electric if e.get("type") == "USAGE"), None)
    if usage is None:
        return {"status": data.get("status"), "meters": [], "flow_totals": {}}

    series_map = {s.get("name"): (s.get("data") or []) for s in (usage.get("series") or []) if s.get("name")}
    flow_totals: dict[str, float] = defaultdict(float)
    flow_days: dict[str, int] = defaultdict(int)
    meters_out = []
    for meter in (usage.get("meters") or []):
        flow = (meter.get("flowDirection") or "?").upper()
        pts = series_map.get(meter.get("seriesId") or "") or []
        total = sum(float(p["y"]) for p in pts if p.get("y") is not None)
        nonzero_days = sum(1 for p in pts if p.get("y") not in (None, 0, 0.0))
        flow_totals[flow] += total
        flow_days[flow] += nonzero_days
        meters_out.append({
            "meter": meter.get("meterNumber") or meter.get("seriesId"),
            "flow": flow, "total_kwh": round(total, 2), "days_with_data": nonzero_days,
        })
    return {
        "status": data.get("status"),
        "meters": meters_out,
        "flow_totals": {k: round(v, 2) for k, v in flow_totals.items()},
        "flow_days": dict(flow_days),
    }


def _billing_total_usage(host, session, account_number) -> float | None:
    """billing-overview totalUsage (the consumption number the extension scrapes)
    — a cross-check that RETURN ≠ consumption."""
    url = f"{_base_url(host)}/services/secured/billing/history/overview"
    headers = _auth_headers(session["email"], session["auth_token"])
    try:
        r = httpx.get(url, params={"acctNbr": account_number}, headers=headers,
                      follow_redirects=True, timeout=60.0)
        r.raise_for_status()
        body = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"    ! billing/overview failed: {type(exc).__name__}: {exc}")
        return None
    bills = body if isinstance(body, list) else (body.get("bills") or body.get("data") or [])
    totals = [b.get("totalUsage") for b in bills if isinstance(b, dict) and isinstance(b.get("totalUsage"), (int, float))]
    return round(sum(totals) / len(totals), 1) if totals else None


def _verdict(raw: dict) -> str:
    ft = raw.get("flow_totals", {})
    has_return = ft.get("RETURN", 0) > 0
    has_forward = ft.get("FORWARD", 0) > 0
    has_net = "NET" in ft
    if has_return and has_forward:
        return ("✅ LIKELY CONFIRMED — both RETURN (generation) and FORWARD "
                "(consumption) channels present and non-zero. Spot-check that the "
                "RETURN total matches the bill's net-metering generation credit.")
    if has_net and not has_return:
        return ("⚠️  NET-only — this account exposes a combined NET channel; "
                "generation is the magnitude of NET on export days. Verify the sign "
                "convention (negative = export) against the bill.")
    if has_forward and not has_return:
        return ("❌ NO GENERATION CHANNEL — only FORWARD (consumption) is present. "
                "Either this is not a net-metering/solar account, or this co-op "
                "labels generation differently. Try a known solar account here.")
    return "❓ UNCLEAR — no FORWARD/RETURN/NET channels found; inspect the raw meters above."


def verify_account(db, acct: UtilityAccount, days: int) -> None:
    print(f"\n=== {acct.provider} · account {acct.account_number} "
          f"(tenant {acct.tenant_id}, customer {acct.customer_number}) ===")
    host = _get_smarthub_host(acct.provider)
    sess_row = session_for_account(db, acct)
    if sess_row is None:
        print("  SKIP: no stored session — sign into this co-op's SmartHub via the extension first.")
        return
    session = _build_session_dict(sess_row)
    if session is None:
        print("  SKIP: stored session has no usable auth token.")
        return
    print(f"  host={host}  session.customer_number={sess_row.customer_number}  "
          f"login={session.get('email')}")

    sl = _resolve_service_location(db, acct, host, session)
    if not sl:
        print("  SKIP: could not resolve serviceLocationNumber.")
        return
    print(f"  serviceLocationNumber={sl}")

    end = date.today()
    start = end - timedelta(days=days)

    # 1) RAW channel breakdown — the thing we actually need to see.
    raw = _raw_channel_breakdown(host, session, sl, acct.account_number, start, end)
    print(f"\n  RAW utility-usage poll  (status={raw.get('status')}, last {days}d):")
    for m in raw["meters"]:
        print(f"    meter {m['meter']:<14} flow={m['flow']:<8} "
              f"total={m['total_kwh']:>12,.2f} kWh   days_with_data={m['days_with_data']}")
    print(f"    flow totals: {raw['flow_totals']}")

    # 2) Adapter's parsed view (what the live job persists).
    try:
        parsed = fetch_daily_generation(host, session, sl, acct.account_number, start, end)
        gen = round(sum(d["kwh_generated"] for d in parsed), 2)
        con = round(sum(d["kwh_consumed"] for d in parsed), 2)
        net = round(sum(d["kwh_net_export"] for d in parsed), 2)
        print(f"\n  ADAPTER parsed (fetch_daily_generation): "
              f"generation(RETURN)={gen:,.2f}  consumption(FORWARD)={con:,.2f}  "
              f"net_export={net:,.2f}  over {len(parsed)} days")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ADAPTER parse failed: {type(exc).__name__}: {exc}")

    # 3) Billing-overview cross-check (consumption).
    tu = _billing_total_usage(host, session, acct.account_number)
    if tu is not None:
        print(f"  billing-overview totalUsage (≈ consumption): {tu:,.1f} kWh/bill avg")

    print(f"\n  VERDICT: {_verdict(raw)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the SmartHub generation channel on a real account.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--account", help="utility account number to verify")
    g.add_argument("--tenant", help="verify every SmartHub account for this tenant id")
    ap.add_argument("--days", type=int, default=120, help="look-back window (default 120)")
    args = ap.parse_args()

    with SessionLocal() as db:
        accts = _smarthub_accounts(db, account=args.account, tenant=args.tenant)
        if not accts:
            print("No captured SmartHub accounts match. Has the operator signed into "
                  "the co-op's SmartHub via the extension yet?")
            return 1
        for acct in accts:
            verify_account(db, acct, args.days)
        db.commit()  # persist any discovered serviceLocationNumber caching
    return 0


if __name__ == "__main__":
    sys.exit(main())
