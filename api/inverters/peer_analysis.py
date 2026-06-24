"""Peer-relative ground-truth analysis (ported from sun-mirror/capture/inverters.py).

The core idea: a generation unit's health is judged not in isolation but against
its *cohort* — the other units that sat under the same sky over the same window.
Weather cancels out, so the cohort is its own baseline. A passing cloud dims the
whole cohort equally; a dead string dims only one.

This module is UNIT-AGNOSTIC. A "unit" is anything that produces daily kWh and
has a nameplate: in sun-mirror it was an *inverter* within one site; in the
NEPOOL Operator product it is an *array* within one Client/account cohort. The
same math applies because peer_index normalizes by nameplate share, not by any
unit-specific assumption.

  peer_index = (unit's share of cohort ENERGY) / (unit's share of cohort NAMEPLATE)
             = 1.00  -> pulling its weight
             < 0.85  -> underperforming (shading / soiling / failed string)

Status precedence (highest first):
  fault          -> vendor-reported error code/mode
  dead           -> zero output >= DEAD_DAYS while cohort peers produced
  comm_gap       -> no telemetry within COMM_GAP_HOURS
  underperforming -> peer_index < UNDERPERFORM_THRESHOLD
  ok             -> pulling its weight

DEGENERATE COHORTS (the loud caveat): peer comparison needs >= 2 units to mean
anything. With a single-unit cohort there is no peer signal, so peer_index is
reported as None and status collapses to the *self-evident* states only
(fault / dead / comm_gap / ok) — never "underperforming", because there is
nothing to underperform against. Callers should surface a "needs >= 2 units for
peer analysis" hint for solo owners.

Pure functions only — no I/O, no DB, no network. Feed it dicts, get dicts back.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Optional

# Thresholds (kept identical to sun-mirror so behavior is byte-comparable).
UNDERPERFORM_THRESHOLD = 0.85   # peer_index below this => underperforming
DEAD_DAYS = 2                   # zero output this many days (peers alive) => dead
COMM_GAP_HOURS = 24             # no telemetry within => comm_gap
WINDOW_DAYS = 14                # analysis window (informational; caller windows the data)


def _parse_ts(value: str | None) -> Optional[datetime]:
    """Parse an ISO timestamp into an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _infer_nameplate(unit: dict) -> Optional[float]:
    """Fall back to observed peak daily kWh / 4 when nameplate is absent.

    A roughly 4 kWh/day-per-kW peak is a reasonable temperate-climate ceiling;
    this only sets the normalization denominator, and every unit in the cohort
    is treated the same way, so a consistent bias cancels in peer_index.
    """
    if unit.get("nameplate_kw"):
        return float(unit["nameplate_kw"])
    peak = max((d["kwh"] for d in unit.get("daily", [])), default=0.0)
    return round(peak / 4.0, 1) if peak else None


def analyze_cohort(
    units: list[dict],
    *,
    now: Optional[datetime] = None,
    window_days: int = WINDOW_DAYS,
) -> dict:
    """Run peer-relative analysis over a cohort of generation units.

    Each input unit is a dict with at least:
      id            -- stable identifier (any hashable; echoed back)
      nameplate_kw  -- float | None (None => inferred from observed peak)
      daily         -- list of {"date": "YYYY-MM-DD", "kwh": float}, ascending
      error_code    -- vendor fault code/string | None
      last_report   -- ISO timestamp of last telemetry | None

    Returns {"units": [...enriched...], "summary": {...}, "cohort_size": N,
    "degenerate": bool, "thresholds": {...}, "generated_at": iso}. Each enriched
    unit gains: peer_index, window_kwh, stale_hours, status, diagnosis.

    The input list is not mutated; enriched copies are returned.
    """
    now = now or datetime.now(timezone.utc)
    cohort_size = len(units)
    degenerate = cohort_size < 2

    # Work on shallow copies so callers' dicts are untouched.
    out_units: list[dict] = [dict(u) for u in units]

    for u in out_units:
        u["nameplate_kw"] = _infer_nameplate(u)

    total_nameplate = sum(u["nameplate_kw"] or 0.0 for u in out_units) or 1.0
    window_energy = {
        id(u): sum(d["kwh"] for d in u.get("daily", [])) for u in out_units
    }
    cohort_energy = sum(window_energy.values()) or 1.0

    # How many peers produced on each day (for the dead-vs-cloudy distinction).
    peers_alive_by_day: dict[str, int] = {}
    for u in out_units:
        for d in u.get("daily", []):
            if d["kwh"] > 0:
                peers_alive_by_day[d["date"]] = peers_alive_by_day.get(d["date"], 0) + 1

    attention = 0
    for u in out_units:
        e = window_energy[id(u)]
        share_e = e / cohort_energy
        share_np = (u["nameplate_kw"] or 0.0) / total_nameplate
        # Peer index is only meaningful with >= 2 units in the cohort.
        u["peer_index"] = (
            round(share_e / share_np, 2) if (share_np and not degenerate) else None
        )
        u["window_kwh"] = round(e, 1)

        # Trailing zero-day streak while at least one peer produced.
        zero_streak = 0
        for d in reversed(u.get("daily", [])):
            if d["kwh"] == 0 and peers_alive_by_day.get(d["date"], 0) > 0:
                zero_streak += 1
            else:
                break

        ts = _parse_ts(u.get("last_report"))
        stale_h = (now - ts).total_seconds() / 3600 if ts else None
        u["stale_hours"] = round(stale_h, 1) if stale_h is not None else None

        last_day = u["daily"][-1]["date"] if u.get("daily") else None
        peers_that_day = peers_alive_by_day.get(last_day, 0) if last_day else 0

        if u.get("error_code"):
            u["status"] = "fault"
            u["diagnosis"] = (
                f"Vendor fault {u['error_code']} — reports mode "
                f"{u.get('mode') or '?'}. Dispatch-worthy."
            )
        elif zero_streak >= DEAD_DAYS:
            u["status"] = "dead"
            u["diagnosis"] = (
                f"Zero output for {zero_streak} days while {peers_that_day} "
                "peers produced. Hard failure or open AC disconnect."
            )
        elif stale_h is not None and stale_h > COMM_GAP_HOURS:
            u["status"] = "comm_gap"
            u["diagnosis"] = (
                f"No telemetry for {stale_h:.0f}h. Production unknown — likely "
                "gateway/Wi-Fi dropout, not necessarily a power fault."
            )
        elif (u["peer_index"] is not None and u["peer_index"] < UNDERPERFORM_THRESHOLD
              and u.get("daily")):
            # Real underperformance requires real history to compare against. An
            # inverter with NO captured daily series has peer_index ~0 purely because
            # its window energy is 0 — that's MISSING DATA, not underproduction (it
            # may be producing fine right now, as live power shows). The `and
            # u.get("daily")` guard drops the no-history case to the branch below so
            # we never falsely flag it "underperforming / 100% below peers".
            deficit = round((1 - u["peer_index"]) * 100)
            u["status"] = "underperforming"
            u["diagnosis"] = (
                f"Producing {deficit}% below its nameplate share vs peers over "
                f"{window_days}d under identical weather. Pattern suggests "
                "shading, soiling, or a failed string/module."
            )
        else:
            u["status"] = "ok"
            if not u.get("daily"):
                # Healthy by default until we have data to say otherwise — its daily
                # series simply hasn't synced yet (common right after a connect, or a
                # per-device history capture gap). Live power, if any, still shows.
                u["diagnosis"] = (
                    "Reporting — not enough captured history yet to peer-compare "
                    "(its daily series hasn't synced)."
                )
            elif degenerate:
                u["diagnosis"] = "Reporting normally (solo unit — no peers to compare against)."
            else:
                u["diagnosis"] = "Pulling its weight relative to cohort nameplate."
        if u["status"] != "ok":
            attention += 1

    # Estimated kWh the cohort lost to its troubled units over the window:
    # what each bad unit "should" have made (its nameplate share of cohort
    # energy, grossed up by how far below par it ran) minus what it did make.
    est_loss = round(
        sum(
            max(
                0.0,
                (u["nameplate_kw"] or 0.0) / total_nameplate * cohort_energy
                / max(u["peer_index"] or 1.0, 0.01)
                - window_energy[id(u)],
            )
            for u in out_units
            if u["status"] in ("dead", "underperforming", "fault")
        ),
        1,
    )

    today_kwh = round(
        sum(u["daily"][-1]["kwh"] for u in out_units if u.get("daily")), 1
    )

    summary = {
        "today_kwh": today_kwh,
        "window_days": window_days,
        "window_kwh": round(cohort_energy, 1),
        "units_total": cohort_size,
        "units_ok": cohort_size - attention,
        "units_attention": attention,
        "estimated_loss_kwh_window": est_loss,
        "peer_analysis_available": not degenerate,
    }

    return {
        "units": out_units,
        "summary": summary,
        "cohort_size": cohort_size,
        "degenerate": degenerate,
        "thresholds": {
            "underperform_peer_index": UNDERPERFORM_THRESHOLD,
            "dead_days": DEAD_DAYS,
            "comm_gap_hours": COMM_GAP_HOURS,
        },
        "generated_at": now.isoformat(),
    }
