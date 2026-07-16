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
MIN_PEER_DAYS = 3               # stable mode: need >= this many reporting days to peer-judge


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
    complete_days_only: bool = False,
    tz_name: Optional[str] = None,
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

    STABLE MODE (complete_days_only=True) — for the e-mail alert path:
    The live dashboard judges health on data INCLUDING today, which is correct
    for "what's happening right now". But the daily digest / alert sweep run in
    the early morning, when *today* is a partial, just-waking day: captures land
    unevenly, the whole array is at a few % of rated under dawn fog, and a 24h
    wall-clock "no telemetry" trips for the whole fleet simply because solar
    inverters don't report overnight. All three produce false "underperforming /
    dead / gone quiet" alarms (Bruce: "the sun's variability, fog, etc early in
    the am will give erroneous results"). Stable mode is the fix he asked for —
    "pick up noon of the prior day":
      * drop TODAY's partial local day so verdicts run on settled, complete days
        (a full day integrates out the morning weather — better than one noon
        snapshot a passing cloud could ruin);
      * judge "gone quiet" COHORT-RELATIVE — an inverter is quiet only when it's
        far staler than its freshest peer, so an overnight lull (everyone stale
        together) never trips it; a single dead gateway still does.
    `tz_name` (e.g. "America/New_York") sets which local day counts as "today".
    """
    now = now or datetime.now(timezone.utc)
    cohort_size = len(units)
    degenerate = cohort_size < 2

    # Work on shallow copies so callers' dicts are untouched.
    out_units: list[dict] = [dict(u) for u in units]

    # STABLE MODE: drop today's partial local day so the verdict is judged only on
    # complete, settled days (kills the dawn false positives).
    if complete_days_only:
        ref = now
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                ref = now.astimezone(ZoneInfo(tz_name))
            except Exception:
                ref = now  # bad tz name -> fall back to UTC "today"
        today_local = ref.date().isoformat()
        for u in out_units:
            u["daily"] = [
                d for d in (u.get("daily") or [])
                if d.get("date") and str(d["date"]) < today_local
            ]

    for u in out_units:
        u["nameplate_kw"] = _infer_nameplate(u)

    # Cohort-relative comm-gap baseline: the freshest last_report across the cohort.
    # In stable mode a unit is "gone quiet" only when it's COMM_GAP_HOURS staler
    # than this freshest peer — so a uniform overnight/weekend lull (no captures
    # for anyone) never flags, while one genuinely-dropped device still does.
    _cohort_stales = []
    for u in out_units:
        _ts = _parse_ts(u.get("last_report"))
        if _ts is not None:
            _cohort_stales.append((now - _ts).total_seconds() / 3600)
    cohort_min_stale = min(_cohort_stales) if _cohort_stales else None

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

    # STABLE MODE: a per-day cohort baseline (energy + nameplate of the units that
    # actually REPORTED that day) for a peer_index robust to unequal captured-day
    # counts. Capture gaps store missing days as 0/absent, so an inverter that
    # simply captured FEWER days has a smaller window sum and looks
    # "underperforming" even when its output on the days it DID report matches its
    # peers exactly (Bruce's "Primo (7) flagged but all working"). Comparing mean
    # per-active-day output-per-kW — not raw window totals — fixes that: a missing
    # day counts neither for nor against, while a unit that reports every day at a
    # genuine deficit is still caught.
    day_energy: dict[str, float] = {}
    day_nameplate: dict[str, float] = {}
    if complete_days_only:
        for u in out_units:
            npk = u["nameplate_kw"] or 0.0
            for d in u.get("daily", []):
                if d.get("kwh") and d["kwh"] > 0 and npk > 0:
                    day_energy[d["date"]] = day_energy.get(d["date"], 0.0) + d["kwh"]
                    day_nameplate[d["date"]] = day_nameplate.get(d["date"], 0.0) + npk

    attention = 0
    for u in out_units:
        e = window_energy[id(u)]
        share_e = e / cohort_energy
        share_np = (u["nameplate_kw"] or 0.0) / total_nameplate
        # Peer index is only meaningful with >= 2 units in the cohort.
        if complete_days_only:
            # Mean of this unit's per-active-day (output-per-kW vs the cohort's
            # output-per-kW that same day). Missing/zero days are skipped, so a
            # sparse-but-healthy capture reads ~1.0; a real every-day deficit < 0.85.
            npk = u["nameplate_kw"] or 0.0
            ratios = []
            for d in u.get("daily", []):
                if d.get("kwh") and d["kwh"] > 0 and npk > 0:
                    be, bnp = day_energy.get(d["date"], 0.0), day_nameplate.get(d["date"], 0.0)
                    if be > 0 and bnp > 0:
                        cohort_per_kw = be / bnp
                        if cohort_per_kw > 0:
                            ratios.append((d["kwh"] / npk) / cohort_per_kw)
            u["peer_index"] = (
                round(sum(ratios) / len(ratios), 2)
                if (len(ratios) >= MIN_PEER_DAYS and not degenerate) else None
            )
        else:
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

        # Calendar-gap dead: last daily row is older than DEAD_DAYS while peers
        # kept producing later (missing days, not stored zeros — Danville #54).
        gap_dead_days = 0
        last_day = u["daily"][-1]["date"] if u.get("daily") else None
        if last_day:
            try:
                from datetime import date as _date
                _ld = _date.fromisoformat(str(last_day)[:10])
                _newer = [
                    _date.fromisoformat(str(dd)[:10])
                    for dd in peers_alive_by_day.keys()
                    if peers_alive_by_day.get(dd, 0) > 0
                ]
                if _newer:
                    _gap = (max(_newer) - _ld).days
                    if _gap >= DEAD_DAYS:
                        gap_dead_days = _gap
            except Exception:
                gap_dead_days = 0

        ts = _parse_ts(u.get("last_report"))
        stale_h = (now - ts).total_seconds() / 3600 if ts else None
        u["stale_hours"] = round(stale_h, 1) if stale_h is not None else None

        peers_that_day = peers_alive_by_day.get(last_day, 0) if last_day else 0

        if u.get("error_code"):
            u["status"] = "fault"
            u["diagnosis"] = (
                f"Vendor fault {u['error_code']} — reports mode "
                f"{u.get('mode') or '?'}. Dispatch-worthy."
            )
        elif zero_streak >= DEAD_DAYS or gap_dead_days >= DEAD_DAYS:
            _days = max(zero_streak, gap_dead_days)
            u["status"] = "dead"
            u["diagnosis"] = (
                f"No production for {_days} days while peers kept producing. "
                "Hard failure, open AC disconnect, or vendor dropped this unit."
            )
        elif stale_h is not None and (
            # Stable mode: quiet only RELATIVE to the freshest peer (an overnight
            # lull is uniform, so it never trips). Live mode: absolute 24h clock.
            (cohort_min_stale is not None and (stale_h - cohort_min_stale) > COMM_GAP_HOURS)
            if complete_days_only else stale_h > COMM_GAP_HOURS
        ):
            if complete_days_only and cohort_min_stale is not None:
                lag = stale_h - cohort_min_stale
                u["diagnosis"] = (
                    f"No telemetry for {stale_h:.0f}h — {lag:.0f}h longer than its "
                    "freshest peer, so this is the device, not an overnight lull. "
                    "Likely gateway/Wi-Fi dropout, not necessarily a power fault."
                )
            else:
                u["diagnosis"] = (
                    f"No telemetry for {stale_h:.0f}h. Production unknown — likely "
                    "gateway/Wi-Fi dropout, not necessarily a power fault."
                )
            u["status"] = "comm_gap"
        elif (u.get("expected_low") and u.get("expected_low_baseline")
              and u["peer_index"] is not None and u.get("daily")):
            # OWNER-CONFIRMED EXPECTED-LOW (structural shading / obstruction). We do
            # NOT flag it against the cohort floor — the owner told us this unit runs
            # permanently below its peers for a fixed physical reason. Instead we judge
            # it against the baseline peer_index recorded when it was marked: it's fine
            # while it HOLDS that level, and only flags if it drops meaningfully BELOW
            # it — a genuine NEW problem stacked on top of the known shading. This is
            # the whole point: silence the known bias without going blind to a fault.
            base = u["expected_low_baseline"]
            rel = (u["peer_index"] / base) if base > 0 else 1.0
            if rel < UNDERPERFORM_THRESHOLD:
                drop = round((1 - rel) * 100)
                u["status"] = "underperforming"
                u["expected_low_breach"] = True
                u["diagnosis"] = (
                    f"Running {drop}% below its OWN expected level. This unit is marked "
                    f"expected-low ({u.get('expected_low_reason') or 'known shading/obstruction'}), "
                    f"so we hold it to ~{round(base * 100)}% of peers — dropping beneath that "
                    "baseline points to a NEW issue on top of the shading. Worth a look."
                )
            else:
                u["status"] = "ok"
                u["diagnosis"] = (
                    f"Holding at its expected reduced level (~{round(base * 100)}% of peers — "
                    f"{u.get('expected_low_reason') or 'known shading/obstruction'}). "
                    "Marked expected-low, so this is normal for this unit, not a fault."
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
            # An expected-low unit's shortfall is STRUCTURAL (shading) — not money we
            # can recover by a repair — so it never inflates the recoverable-$ figure,
            # even when it briefly breaches its baseline.
            and not u.get("expected_low")
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
