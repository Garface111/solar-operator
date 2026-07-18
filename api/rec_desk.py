"""REC Desk — ownership + readiness + expected certificate inventory (software only).

NEPOOL-GIS tracks and transfers certificates; it does NOT buy/sell or price them.
This module never initiates a GIS transfer, never holds certificates, and never
quotes a firm bid. It answers:

  • Do YOU still own the RECs on each array?
  • What is missing before mint/sale is even possible?
  • Roughly how many RECs should the trailing generation produce?

Indicative market bands live in Resources (`resources-data.json`); we may echo
them as display-only context, never as an AO bid.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

# Ownership machine — free strings, constrained at the API layer.
OWNERSHIP_VALUES = frozenset({
    "unknown",
    "owner_retained",
    "assigned_to_utility",
    "counterparty",
    "split",
})


def _as_date(d) -> Optional[date]:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return None


def _quarter(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def expected_recs_by_quarter(db: Session, array_id: int, *, quarters: int = 6) -> list[dict[str, Any]]:
    """Floor(MWh) per generation quarter from DailyGeneration — mirrors REC writer spirit.

    Uses the NEPOOL mint-lag default reference so the newest quarter is the one
    the Independent Verifier is likely filing now, not the incomplete in-progress
    quarter. This is EXPECTED inventory from our meters, not GIS-minted truth.
    """
    from .models import DailyGeneration
    from .writers.gmcs_writer import default_reporting_reference_date, _rolling_quarters

    today = date.today()
    ref = default_reporting_reference_date(today)
    window = _rolling_quarters(ref, count=quarters)  # list[(year, q)] oldest→newest

    # Bound the SQL window
    if not window:
        return []
    y0, q0 = window[0]
    start = date(y0, (q0 - 1) * 3 + 1, 1)
    y1, q1 = window[-1]
    end_month = q1 * 3
    # last day of end quarter month
    if end_month == 12:
        end = date(y1, 12, 31)
    else:
        end = date(y1, end_month + 1, 1)
        from datetime import timedelta
        end = end - timedelta(days=1)

    rows = db.execute(
        select(DailyGeneration.day, DailyGeneration.kwh).where(
            DailyGeneration.array_id == array_id,
            DailyGeneration.day >= start,
            DailyGeneration.day <= end,
            DailyGeneration.kwh.isnot(None),
        )
    ).all()

    by_q: dict[tuple[int, int], float] = defaultdict(float)
    days_by_q: dict[tuple[int, int], set] = defaultdict(set)
    for day, kwh in rows:
        d = _as_date(day)
        if not d:
            continue
        try:
            k = float(kwh or 0)
        except (TypeError, ValueError):
            continue
        if k <= 0:
            continue
        yq = _quarter(d)
        by_q[yq] += k
        days_by_q[yq].add(d)

    out = []
    for y, q in window:
        kwh = by_q.get((y, q), 0.0)
        mwh = kwh / 1000.0
        recs = int(mwh)  # floor — same safe rule as GMCS (MWh ≥ 0)
        out.append({
            "year": y,
            "quarter": q,
            "label": f"Q{q} {y}",
            "kwh": round(kwh, 1),
            "mwh": round(mwh, 3),
            "expected_recs": recs,
            "day_count": len(days_by_q.get((y, q), ())),
            "source": "daily_generation_floor",
            "note": "Expected from metered kWh (floor MWh). Not proof of GIS mint.",
        })
    return out


def array_readiness(db: Session, arr) -> dict[str, Any]:
    """Checklist + score for one array. Pure read of existing fields + generation."""
    ownership = (getattr(arr, "rec_ownership", None) or "unknown").strip().lower()
    if ownership not in OWNERSHIP_VALUES:
        ownership = "unknown"

    gis = (getattr(arr, "nepool_gis_id", None) or "").strip() or None
    fuel = (getattr(arr, "fuel_type", None) or "solar").strip() or "solar"
    registry = (getattr(arr, "cert_registry", None) or "").strip() or "NEPOOL-GIS"
    verifier = (getattr(arr, "rec_verifier_name", None) or "").strip() or None
    note = (getattr(arr, "rec_ownership_note", None) or "").strip() or None

    checks = []
    def add(key, ok, label, detail=None):
        checks.append({"key": key, "ok": bool(ok), "label": label, "detail": detail})

    add("ownership_known", ownership != "unknown",
        "REC ownership recorded",
        "Set whether you retain certificates or assigned them to the utility/program.")
    sellable = ownership == "owner_retained"
    add("owner_retained", sellable or ownership == "unknown",
        "Owner appears to retain RECs" if sellable else (
            "RECs assigned away — no open-market sell path" if ownership in (
                "assigned_to_utility", "counterparty") else "Ownership unclear"),
        note)
    add("gis_id", bool(gis), "NEPOOL-GIS (or registry) unit id",
        gis or "Add the GIS generator id so filings match the registry.")
    add("fuel", bool(fuel), "Fuel type", fuel)
    add("registry", bool(registry), "Certificate registry", registry)
    add("verifier", bool(verifier), "Independent Verifier named",
        verifier or "Who files generation to GIS? (e.g. your REC agent / Crown-class IV)")

    vintages = expected_recs_by_quarter(db, arr.id)
    gen_ok = any(v["expected_recs"] > 0 or v["day_count"] > 0 for v in vintages)
    add("generation", gen_ok, "Generation history for recent quarters",
        f"{sum(v['expected_recs'] for v in vintages)} expected RECs across window"
        if gen_ok else "No metered kWh in the REC reporting window yet.")

    ok_n = sum(1 for c in checks if c["ok"])
    # Sell path only when ownership is owner_retained AND core ids present
    can_sell_path = (
        ownership == "owner_retained"
        and bool(gis)
        and gen_ok
    )
    if ownership == "assigned_to_utility":
        path = "utility_assigned"
        path_label = "Attributes go to the utility/program — monetized via rate adjustor/incentive, not open-market REC sales."
    elif ownership == "counterparty":
        path = "counterparty"
        path_label = "A PPA/program counterparty holds attributes — not owner-sold."
    elif ownership == "owner_retained" and can_sell_path:
        path = "sell_ready"
        path_label = "Owner-retained with GIS id + generation — ready for mint pack / broker intro."
    elif ownership == "owner_retained":
        path = "sell_blocked"
        path_label = "Owner-retained but missing GIS id, verifier, or generation — finish readiness."
    else:
        path = "unknown"
        path_label = "Record ownership first. Many net-metering programs assign RECs to the utility."

    return {
        "array_id": arr.id,
        "array_name": arr.name,
        "ownership": ownership,
        "ownership_note": note,
        "nepool_gis_id": gis,
        "fuel_type": fuel,
        "cert_registry": registry,
        "verifier_name": verifier,
        "checks": checks,
        "readiness_score": ok_n,
        "readiness_max": len(checks),
        "path": path,
        "path_label": path_label,
        "can_list_intent": can_sell_path,  # future sell-intent gate
        "expected_vintages": vintages,
        "expected_recs_total": int(sum(v["expected_recs"] for v in vintages)),
        "disclaimer": (
            "Not a broker bid and not GIS inventory. Expected RECs = floor(MWh) from "
            "your captured generation. Certificate title moves only in NEPOOL-GIS (or "
            "another registry) under your or your verifier's account. AO does not hold "
            "certificates or sale proceeds."
        ),
    }


def tenant_rec_desk(db: Session, tenant_id: str) -> dict[str, Any]:
    from .models import Array

    arrays = db.execute(
        select(Array).where(Array.tenant_id == tenant_id)
        .order_by(Array.id)
    ).scalars().all()

    rows = []
    for a in arrays:
        if getattr(a, "excluded", False):
            continue
        if getattr(a, "deleted_at", None) is not None:
            continue
        rows.append(array_readiness(db, a))

    def _count(path):
        return sum(1 for r in rows if r["path"] == path)

    totals = {
        "array_count": len(rows),
        "unknown_ownership": sum(1 for r in rows if r["ownership"] == "unknown"),
        "owner_retained": sum(1 for r in rows if r["ownership"] == "owner_retained"),
        "assigned_to_utility": sum(1 for r in rows if r["ownership"] == "assigned_to_utility"),
        "sell_ready": _count("sell_ready"),
        "sell_blocked": _count("sell_blocked"),
        "expected_recs_total": int(sum(r["expected_recs_total"] for r in rows)),
    }
    # Sort: actionable first
    order = {"sell_ready": 0, "sell_blocked": 1, "unknown": 2, "utility_assigned": 3,
             "counterparty": 4}
    rows.sort(key=lambda r: (order.get(r["path"], 9), -(r["expected_recs_total"] or 0)))

    return {
        "ok": True,
        "arrays": rows,
        "totals": totals,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "market_note": (
            "Indicative New England Class I band is shown in Analysis → Resources "
            "(~$30–40/MWh, MA ACP $40 ceiling). That is reference context, not an AO bid."
        ),
        "legal_note": (
            "REC Desk is software: ownership, readiness, expected inventory, and "
            "checklists. GIS transfers and broker negotiations stay with you / your IV / "
            "a licensed marketer. No success fee and no custody in this version."
        ),
    }
