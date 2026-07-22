"""Group-host utility bill anatomy + hard offtaker-base rules.

GMP group net-metering (Group Rate 06) host bills show:

  Total Generated
  Host Consumed
  Group Excess Shared   ← the offtaker credit pool
  Net Billed (often 0)
  Fixed / non-bypassable charges

Colleen / HCT (May 2026 Norwich Union Village): page-1 snapshot (27,180 gen /
180 used) does not match page-2 detail (27,000 gen / 175 consumed / **26,825
group excess shared**). Offtaker credits must use **Group Excess Shared**
(`Bill.kwh_sent_to_grid`), never the snapshot and never gross generation when
shared < gross.

This module is the single source of truth for that pool + the operator-facing
anatomy strip / integrity warnings.
"""
from __future__ import annotations

from typing import Any, Optional


# Integrity: gen − consumed should equal group excess within this many kWh.
ANATOMY_TOL_KWH = 1.0


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _net_billed_from_raw(raw_json: Optional[dict]) -> Optional[float]:
    """Largest |NET| kWh line on a GMP bill JSON, if present."""
    if not isinstance(raw_json, dict):
        return None
    best = None
    for seg in raw_json.get("billSegments") or []:
        for li in seg.get("segmentLineItems") or []:
            if li.get("unitOfMeasure") != "KWH":
                continue
            if (li.get("unitCode") or "").upper() != "NET":
                continue
            v = _f(li.get("unitCount"))
            if v is None:
                continue
            if best is None or abs(v) > abs(best):
                best = v
    return best


def group_excess_pool(bill) -> tuple[Optional[float], str, Optional[str]]:
    """Resolve the offtaker credit pool for a host bill with the hard rule.

    Preference (never inflates to gross when shared is smaller):
      1. kwh_sent_to_grid when > 0  (Group Excess Shared / EXCESS codes)
      2. max(0, kwh_generated − kwh_consumed) when both known
      3. kwh_generated as last resort, with a fallback warning

    Returns (kwh, source, warning_or_none).
    source ∈ kwh_sent_to_grid | gen_minus_consumed | gross_fallback | none
    """
    if bill is None:
        return None, "none", None
    sent = _f(getattr(bill, "kwh_sent_to_grid", None))
    gen = _f(getattr(bill, "kwh_generated", None))
    consumed = _f(getattr(bill, "kwh_consumed", None))

    if sent is not None and sent > 0:
        # HARD RULE: shared wins — never replace with gross even if gross is larger.
        warn = None
        if gen is not None and gen > 0 and sent > gen + ANATOMY_TOL_KWH:
            warn = (
                f"Group excess shared ({sent:,.0f} kWh) is larger than gross "
                f"generation ({gen:,.0f} kWh) — check the bill parse."
            )
        return round(sent, 1), "kwh_sent_to_grid", warn

    if gen is not None and gen > 0 and consumed is not None and consumed >= 0:
        residual = gen - consumed
        if residual > 0:
            return (
                round(residual, 1),
                "gen_minus_consumed",
                None,
            )

    if gen is not None and gen > 0:
        return (
            round(gen, 1),
            "gross_fallback",
            "No Group Excess Shared captured on this bill — using gross "
            "generation as the offtaker pool. Confirm host load is zero (or "
            "re-capture the bill) before approving offtaker invoices.",
        )
    return None, "none", None


def bill_anatomy(bill) -> Optional[dict[str, Any]]:
    """Operator-facing anatomy of a group-host (or generation) utility bill.

    Always returns a dict when `bill` is not None so the UI can show dashes for
    missing fields. Includes integrity + fallback warnings.
    """
    if bill is None:
        return None
    gen = _f(getattr(bill, "kwh_generated", None))
    consumed = _f(getattr(bill, "kwh_consumed", None))
    sent_raw = _f(getattr(bill, "kwh_sent_to_grid", None))
    total_cost = _f(getattr(bill, "total_cost", None))
    pool, pool_src, pool_warn = group_excess_pool(bill)
    net_billed = _net_billed_from_raw(getattr(bill, "raw_json", None))

    # Fixed charges: host often has positive non-bypassable total with 0 net kWh.
    fixed_charges = None
    if total_cost is not None and total_cost > 0:
        fixed_charges = round(total_cost, 2)
    elif total_cost is not None and total_cost < 0:
        # Net credit month — no "fixed charges due" framing.
        fixed_charges = None

    integrity_warn = None
    if (
        gen is not None
        and consumed is not None
        and pool is not None
        and pool_src == "kwh_sent_to_grid"
    ):
        expected = gen - consumed
        if abs(expected - pool) > ANATOMY_TOL_KWH:
            integrity_warn = (
                f"Generated ({gen:,.0f}) − host consumed ({consumed:,.0f}) "
                f"= {expected:,.0f} kWh, but Group Excess Shared is "
                f"{pool:,.0f} kWh — differs by more than {ANATOMY_TOL_KWH:g} kWh."
            )

    warnings: list[str] = []
    if pool_warn:
        warnings.append(pool_warn)
    if integrity_warn:
        warnings.append(integrity_warn)

    pe = getattr(bill, "period_end", None)
    ps = getattr(bill, "period_start", None)
    try:
        pe_s = pe.date().isoformat() if hasattr(pe, "date") else (
            pe.isoformat() if pe else None)
    except Exception:
        pe_s = None
    try:
        ps_s = ps.date().isoformat() if hasattr(ps, "date") else (
            ps.isoformat() if ps else None)
    except Exception:
        ps_s = None

    return {
        "generated_kwh": round(gen, 1) if gen is not None else None,
        "host_consumed_kwh": round(consumed, 1) if consumed is not None else None,
        "group_excess_shared_kwh": pool,
        "group_excess_source": pool_src,
        "net_billed_kwh": round(net_billed, 1) if net_billed is not None else None,
        "fixed_charges_usd": fixed_charges,
        "kwh_sent_to_grid_raw": round(sent_raw, 1) if sent_raw is not None else None,
        "period_start": ps_s,
        "period_end": pe_s,
        "is_group_host_signal": bool(
            pool is not None
            and gen is not None
            and pool + ANATOMY_TOL_KWH < gen
        ) or (pool_src == "kwh_sent_to_grid"),
        "warnings": warnings,
        "fallback_to_gross": pool_src == "gross_fallback",
        "integrity_ok": integrity_warn is None,
    }


def help_copy_gmp_group_host() -> dict[str, Any]:
    """Static operator help: how to read a GMP group host bill (May 2026 example)."""
    return {
        "title": "How to read a GMP group host bill",
        "summary": (
            "On a group net-metering host bill, GMP first nets the host site’s "
            "own use against generation, then labels the leftover as Group Excess "
            "Shared — that is the kWh pool assigned to group members for credits. "
            "It is not a second unexplained loss of energy."
        ),
        "rules": [
            "Page 2 Bill Details win over page 1 “My Energy Use Snap Shot”.",
            "Offtaker credits use Group Excess Shared, never the snapshot totals.",
            "Never use gross generation as the offtaker pool when Group Excess "
            "Shared is smaller (host load was netted first).",
            "When host consumption is 0, generated and group excess are the same number.",
        ],
        "example": {
            "label": "Example (group host, host load nonzero)",
            "period": "04/17/26 – 05/19/26",
            "snapshot_generation_kwh": 27180,
            "snapshot_energy_used_kwh": 180,
            "detail_generated_kwh": 27000,
            "detail_host_consumed_kwh": 175,
            "group_excess_shared_kwh": 26825,
            "identity": "27,000 generated − 175 host consumed = 26,825 group excess shared",
            "note": (
                "Snapshot (27,180 − 180 = 27,000) often lands near detail generation "
                "but is not settlement. Use 26,825 for offtaker credit math."
            ),
        },
    }
