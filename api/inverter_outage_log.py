"""Per-inverter OUTAGE LOG — when a unit stopped, and the honest best account of why.

The operator question this answers is Ford's, verbatim: *"when you click on the details
of the inverter, you can see the outage log, and there's specifics about why it went
offline or our best guess as to why it went offline, when it went offline."*

Two words in that sentence carry the whole design: **specifics** and **best guess**.
They are different things and this module never lets them blur together:

  * a **specific** is the vendor's own fault code (``InverterDaily.error_code``) or its
    own live status string (``InverterReading.status``). That is FACT reported by the
    hardware — we quote it verbatim and never paraphrase it into a diagnosis.
  * a **best guess** is inference from the peer cohort — the unit was zero while its
    neighbours produced, or the whole site was dark. We label it as inference, and when
    the evidence does not support any of it we say ``unknown`` and say so in plain
    English rather than inventing a plausible-sounding cause.

Episodes are derived from ``InverterDaily`` (one row per inverter per local day), which
is the only per-inverter series that exists for every vendor — API-pulled (SolarEdge)
and extension-captured (Fronius/SMA/Chint) alike. ``AlertEvent`` is deliberately NOT the
episode source: it is deduped ``UNIQUE(tenant, array, inverter_ref, title)``, so it holds
at most one row per distinct title and is structurally incapable of being a time series.
We only use it to ENRICH an episode with the operator-facing ticket, if one exists.

THE HONESTY RULES (these are product doctrine, not style):

  1. **Night is never an outage.** Solar inverters report nothing after sunset, so a
     partial day is indistinguishable from a dead unit. The window therefore ends at the
     last COMPLETE fleet-local day (yesterday in ``models.FLEET_TZ``) — today is never
     judged. Same reasoning as ``peer_analysis(complete_days_only=True)``.
  2. **Absent is not zero.** No row at all means the feed did not deliver; production is
     UNKNOWN. That is ``no_data``, never "the inverter was down".
  3. **Pre-history is not an outage.** Days before this unit's first-ever daily row are
     not counted — we were not watching yet, so we cannot claim it was off.
  4. **An estimate is never presented as a measurement.** Lost kWh is peer-derived and
     always carries ``lost_kwh_is_estimate: True`` plus the basis sentence; it is ``None``
     (never a fabricated number) when there is no nameplate or no producing peer.
  5. **We never invent a cause.** ``unknown`` is a legitimate, shippable answer.

Pure reads — this module never writes.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime, timedelta
from statistics import median

from sqlalchemy import select

from .models import (
    AlertEvent,
    Array,
    Inverter,
    InverterDaily,
    InverterReading,
    local_today,
)

log = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 180
MAX_WINDOW_DAYS = 730

# Vendor status strings that mean "working fine". A live status is only treated as a
# CAUSE when it is not one of these — otherwise a healthy "Running" would masquerade as
# an explanation for an outage. Anything unrecognised is treated as a real signal and
# reported verbatim (fail toward showing the operator the raw vendor string).
_NORMAL_STATUS_RE = re.compile(
    r"^\s*(ok|okay|normal|running|producing|production|active|online|on[-_ ]?grid"
    r"|grid[-_ ]?connected|generating|mppt|standby|waiting|idle|sleep\w*|night|1)\s*$",
    re.I,
)


def _is_fault_status(value: str | None) -> bool:
    """True when a vendor live-status string looks like a real abnormal condition."""
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    return not _NORMAL_STATUS_RE.match(text)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _day_span(start: date, end: date) -> list[date]:
    """Every calendar day from start..end inclusive (empty when end < start)."""
    if end < start:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _fmt_codes(codes: list[str]) -> str:
    if len(codes) == 1:
        return f'"{codes[0]}"'
    return ", ".join(f'"{c}"' for c in codes[:-1]) + f' and "{codes[-1]}"'


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def build_outage_log(db, tenant, inverter_id: int, days: int = DEFAULT_WINDOW_DAYS,
                     today: date | None = None) -> dict | None:
    """Build the outage log for one inverter. Returns ``None`` when the inverter does
    not belong to ``tenant`` (the caller turns that into a 404 — an inverter id from
    another tenant must be indistinguishable from one that does not exist).

    ``today`` is injectable so tests can pin the fleet-local clock.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = DEFAULT_WINDOW_DAYS
    days = max(1, min(MAX_WINDOW_DAYS, days))

    iv = db.execute(
        select(Inverter).where(
            Inverter.id == inverter_id,
            Inverter.tenant_id == tenant.id,
            Inverter.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if iv is None:
        return None

    array = db.execute(
        select(Array).where(Array.id == iv.array_id, Array.tenant_id == tenant.id)
    ).scalar_one_or_none()
    array_name = array.name if array is not None else None

    today = today or local_today()
    # RULE 1 — judge only COMPLETE local days. Today is still being lived; an inverter
    # that has produced nothing "yet" at 6am is not an outage, it is sunrise.
    window_end = today - timedelta(days=1)
    window_start = window_end - timedelta(days=days - 1)

    # ── cohort ────────────────────────────────────────────────────────────────────
    cohort = db.execute(
        select(Inverter).where(
            Inverter.tenant_id == tenant.id,
            Inverter.array_id == iv.array_id,
            Inverter.deleted_at.is_(None),
        )
    ).scalars().all()
    peers = [p for p in cohort if p.id != iv.id]
    cohort_ids = [c.id for c in cohort]
    peer_np = {p.id: (p.nameplate_kw or 0.0) for p in peers}

    # RULE 3 — never blame the unit for days before it ever reported.
    first_day = db.execute(
        select(InverterDaily.day).where(InverterDaily.inverter_id == iv.id)
        .order_by(InverterDaily.day.asc()).limit(1)
    ).scalar_one_or_none()

    base = {
        "inverter": {
            "id": iv.id,
            "name": iv.name or iv.serial,
            "serial": iv.serial,
            "vendor": iv.vendor,
            "nameplate_kw": iv.nameplate_kw,
            "array_id": iv.array_id,
            "array_name": array_name,
        },
        "window": {
            "days": days,
            "start": _iso(window_start),
            "end": _iso(window_end),
            "note": "Only complete days are judged — today is still in progress, and "
                    "an inverter is legitimately dark every night.",
        },
        "episodes": [],
        "peer_count": len(peers),
    }

    if first_day is None:
        # Nothing has ever been captured for this unit. Saying "no outages" would be a
        # lie of omission — we simply have not been watching.
        base["summary"] = {
            "outage_days": 0, "episode_count": 0,
            "lost_kwh_est": None, "lost_kwh_partial": False,
            "longest": None, "ongoing": False, "ongoing_since": None,
            "last_ended_on": None, "days_since_last_outage": None,
            "state": "no_history",
            "headline": "No daily history has been captured for this inverter yet, so "
                        "there is nothing to report either way.",
        }
        return base

    effective_start = max(window_start, first_day)
    base["window"]["first_data_on"] = _iso(first_day)
    base["window"]["evaluated_from"] = _iso(effective_start)

    # ── the daily grid ────────────────────────────────────────────────────────────
    rows = db.execute(
        select(InverterDaily).where(
            InverterDaily.inverter_id.in_(cohort_ids or [-1]),
            InverterDaily.day >= effective_start,
            InverterDaily.day <= window_end,
        )
    ).scalars().all()

    self_rows: dict[date, InverterDaily] = {}
    peer_rows: dict[date, dict[int, float]] = {}
    for r in rows:
        if r.inverter_id == iv.id:
            self_rows[r.day] = r
        else:
            peer_rows.setdefault(r.day, {})[r.inverter_id] = float(r.kwh or 0.0)

    all_days = _day_span(effective_start, window_end)
    outage_days = [
        d for d in all_days
        if not (d in self_rows and float(self_rows[d].kwh or 0.0) > 0.0)
    ]

    # ── contiguous grouping ───────────────────────────────────────────────────────
    # Days group into an episode when they are consecutive AND rest on the same kind of
    # evidence. That second condition is not fussiness — it is rule 2 applied at the
    # episode level. Real prod case (Chester, ten_ford_demo_100): the array reported a
    # genuine all-zero day on 2026-07-11 and then sent NOTHING from 07-12 onward. Grouped
    # as one 7-day run it would claim "every inverter reported zero" for six days on which
    # no inverter reported anything at all. Split, it tells the truth twice: the site read
    # zero for a day, and we have had no data since — which correctly points a Fronius
    # owner at their stalled capture rather than at a week-long site outage.
    day_has_rows = {d: (d in self_rows or bool(peer_rows.get(d))) for d in all_days}
    runs: list[list[date]] = []
    for d in outage_days:
        prev = runs[-1][-1] if runs else None
        same_run = (prev is not None and (d - prev).days == 1
                    and day_has_rows[d] == day_has_rows[prev])
        if same_run:
            runs[-1].append(d)
        else:
            runs.append([d])

    episodes = [
        _describe_episode(db, tenant, iv, array_name, run, self_rows, peer_rows,
                          peers, peer_np, window_end)
        for run in runs
    ]
    episodes.reverse()   # newest first — the live problem reads before the history
    base["episodes"] = episodes
    base["summary"] = _summarize(episodes, window_end, today, days)
    return base


def _describe_episode(db, tenant, iv, array_name, run: list[date],
                      self_rows: dict, peer_rows: dict, peers: list,
                      peer_np: dict, window_end: date) -> dict:
    started_on, last_day = run[0], run[-1]
    ongoing = last_day == window_end

    # ── 1. VENDOR FACT: the hardware's own account of itself ──────────────────────
    codes: list[str] = []
    for d in run:
        r = self_rows.get(d)
        code = (r.error_code or "").strip() if r is not None else ""
        if code and code not in codes:
            codes.append(code)

    statuses: list[str] = []
    try:
        raw_statuses = db.execute(
            select(InverterReading.status).where(
                InverterReading.inverter_id == iv.id,
                InverterReading.ts >= datetime.combine(started_on, dtime.min),
                InverterReading.ts < datetime.combine(last_day + timedelta(days=1), dtime.min),
            ).distinct()
        ).scalars().all()
    except Exception:                                    # pragma: no cover - defensive
        log.exception("outage-log: reading-status lookup failed for inverter %s", iv.id)
        raw_statuses = []
    for s in raw_statuses:
        if _is_fault_status(s):
            s = str(s).strip()
            if s not in statuses:
                statuses.append(s)

    # ── evidence for the inferred causes ──────────────────────────────────────────
    self_rows_present = any(d in self_rows for d in run)
    peer_rows_present = any(peer_rows.get(d) for d in run)
    array_rows_present = self_rows_present or peer_rows_present
    peer_producing_days = [d for d in run if any(v > 0.0 for v in peer_rows.get(d, {}).values())]
    # Peers that ALSO never produced across the whole episode. Real fleets take two
    # units down at once (prod: Benson Site, inverters 15 and 16, both dark from
    # 2026-07-09) — calling each of them "alone" would be a small, avoidable lie, and
    # "two units down together" is a materially different service call than one.
    co_down = [p.id for p in peers
               if not any(peer_rows.get(d, {}).get(p.id, 0.0) > 0.0 for d in run)]

    vendor = (iv.vendor or "the vendor").title() if iv.vendor else "the vendor"
    where = f"“{array_name}”" if array_name else "this array"

    if codes or statuses:
        cause_kind = "vendor_code"
        parts = []
        if codes:
            parts.append(f"fault code {_fmt_codes(codes)}")
        if statuses:
            parts.append(f"status {_fmt_codes(statuses)}")
        cause = (f"The inverter itself reported {' and '.join(parts)} to {vendor} "
                 f"during this outage. That is the vendor's own explanation, "
                 f"reported verbatim — not our inference.")
    elif not array_rows_present:
        cause_kind = "no_data"
        cause = (f"No data reached us from {where} on {'this day' if len(run) == 1 else 'these days'} "
                 f"at all — production is unknown, not zero. This is a gap in the feed "
                 f"(vendor outage or a missed capture), and it is not evidence that the "
                 f"inverter was down.")
    elif peers and peer_rows_present and not peer_producing_days:
        cause_kind = "site_wide"
        cause = (f"The whole site went dark — every inverter at {where} reported zero, "
                 f"not just this one. That points at a utility outage, a monitoring or "
                 f"comms failure, or a vendor feed gap, rather than at this unit.")
    elif peers and peer_producing_days:
        cause_kind = "unit"
        alone = not co_down
        who = ("This inverter alone" if alone
               else f"This inverter — and {_plural(len(co_down), 'other unit')} at the same site —")
        if not self_rows_present:
            cause = (f"{who} went silent while the rest of {where} kept reporting "
                     f"production — no data from {'this unit' if alone else 'these units'}, "
                     f"real output from the others. That points at the "
                     f"{'unit' if alone else 'units'} or {'its' if alone else 'their'} "
                     f"comms, not a site-wide problem.")
        else:
            cause = (f"{who} stopped while the rest of {where} kept producing — a tripped "
                     f"string, a failed inverter, or a deliberate shutdown. "
                     + ("It is specific to this unit." if alone else
                        "The site as a whole was fine, so this is not a utility outage."))
    elif not peers:
        cause_kind = "unknown"
        cause = ("This inverter reported zero, but it is the only unit on this array, "
                 "so there are no neighbours to compare against. We cannot tell from the "
                 "data whether the whole site was dark or this unit failed.")
    else:
        cause_kind = "unknown"
        cause = ("We cannot attribute this one honestly. The vendor sent no fault code "
                 "and the peer picture is inconclusive, so the cause is unknown.")

    # ── lost-kWh ESTIMATE (rule 4) ────────────────────────────────────────────────
    lost_kwh, basis = _estimate_lost_kwh(iv, run, peer_rows, peer_np)

    ep = {
        "started_on": _iso(started_on),
        "ended_on": None if ongoing else _iso(last_day),
        "last_zero_on": _iso(last_day),
        "days": len(run),
        "ongoing": ongoing,
        "cause_kind": cause_kind,
        "cause": cause,
        "vendor_codes": codes,
        "vendor_statuses": statuses,
        "lost_kwh_est": lost_kwh,
        "lost_kwh_is_estimate": True,
        "lost_kwh_basis": basis,
        "evidence": {
            "self_rows_present": self_rows_present,
            "array_rows_present": array_rows_present,
            "peers_producing_days": len(peer_producing_days),
            "peers_also_down": len(co_down),
            "peer_count": len(peers),
        },
        "ticket": _find_ticket(db, tenant, iv, started_on, last_day, ongoing),
    }
    return ep


def _estimate_lost_kwh(iv, run: list[date], peer_rows: dict, peer_np: dict):
    """Peer-derived estimate of the energy this unit would have made.

    For each outage day: take every peer that PRODUCED that day and has a nameplate,
    compute kWh-per-kW, take the median (robust to one peer also being sick), multiply
    by this unit's nameplate. Sum across the episode.

    Returns ``(None, reason)`` — never a fabricated number — when this unit has no
    nameplate or no peer produced on any day of the episode.
    """
    np_kw = iv.nameplate_kw or 0.0
    if np_kw <= 0:
        return None, ("No nameplate rating is recorded for this inverter, so we cannot "
                      "estimate the lost energy without guessing.")

    total = 0.0
    priced_days = 0
    for d in run:
        per_kw = [
            kwh / peer_np[pid]
            for pid, kwh in peer_rows.get(d, {}).items()
            if kwh > 0.0 and peer_np.get(pid, 0.0) > 0.0
        ]
        if not per_kw:
            continue
        total += median(per_kw) * np_kw
        priced_days += 1

    if priced_days == 0:
        return None, ("No neighbouring inverter produced on these days, so there is no "
                      "honest baseline to estimate the loss from.")

    partial = "" if priced_days == len(run) else (
        f" Covers {priced_days} of {len(run)} days — the rest had no producing peer.")
    return round(total, 1), (
        f"Estimated from the median output-per-kW of {_plural(len(peer_np), 'neighbouring inverter')} "
        f"on the same days, scaled to this unit's {np_kw:g} kW nameplate.{partial}")


def _find_ticket(db, tenant, iv, started_on: date, last_day: date, ongoing: bool):
    """Attach the operator-facing AlertEvent that overlaps this episode, if any.

    AlertEvent is deduped one-row-per-title, so it is NOT a time series and can never
    define an episode — it only enriches one. We match on the inverter reference the
    alert pipeline writes (name or serial) and require the ticket to have been raised
    inside the episode, or to still be open while the episode is ongoing.
    """
    refs = [r for r in (iv.name, iv.serial) if r]
    if not refs:
        return None
    try:
        events = db.execute(
            select(AlertEvent).where(
                AlertEvent.tenant_id == tenant.id,
                AlertEvent.array_id == iv.array_id,
                AlertEvent.inverter_ref.in_(refs),
            ).order_by(AlertEvent.created_at.desc())
        ).scalars().all()
    except Exception:                                    # pragma: no cover - defensive
        log.exception("outage-log: ticket lookup failed for inverter %s", iv.id)
        return None

    for e in events:
        raised = e.created_at.date() if e.created_at else None
        in_span = raised is not None and started_on <= raised <= last_day + timedelta(days=1)
        still_open = ongoing and e.status == "open"
        if in_span or still_open:
            return {
                "id": e.id,
                "title": e.title,
                "severity": e.severity,
                "status": e.status,
                "note": e.note,
                "created_at": _iso(e.created_at),
            }
    return None


def _summarize(episodes: list[dict], window_end: date, today: date, days: int) -> dict:
    if not episodes:
        return {
            "outage_days": 0, "episode_count": 0,
            "lost_kwh_est": None, "lost_kwh_partial": False,
            "longest": None, "ongoing": False, "ongoing_since": None,
            "last_ended_on": None, "days_since_last_outage": None,
            "state": "clean",
            "headline": f"No outages in the last {days} days — this inverter has run clean.",
        }

    total_days = sum(e["days"] for e in episodes)
    priced = [e["lost_kwh_est"] for e in episodes if e["lost_kwh_est"] is not None]
    lost = round(sum(priced), 1) if priced else None
    longest = max(episodes, key=lambda e: e["days"])
    live = next((e for e in episodes if e["ongoing"]), None)

    if live:
        state = "ongoing"
        headline = (f"Offline now — down since {live['started_on']} "
                    f"({_plural(live['days'], 'day')} and counting).")
        last_ended_on, since = None, None
    else:
        state = "recovered"
        newest = episodes[0]                     # episodes are newest-first
        last_ended_on = newest["ended_on"]
        since = (window_end - date.fromisoformat(newest["ended_on"])).days + (today - window_end).days
        headline = (f"Running now — last outage ended {last_ended_on} "
                    f"({_plural(since, 'day')} ago).")

    return {
        "outage_days": total_days,
        "episode_count": len(episodes),
        "lost_kwh_est": lost,
        "lost_kwh_partial": len(priced) != len(episodes),
        "longest": {"days": longest["days"], "started_on": longest["started_on"],
                    "ended_on": longest["ended_on"]},
        "ongoing": bool(live),
        "ongoing_since": live["started_on"] if live else None,
        "last_ended_on": last_ended_on,
        "days_since_last_outage": since,
        "state": state,
        "headline": headline,
    }
