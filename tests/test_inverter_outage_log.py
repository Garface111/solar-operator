"""Inverter OUTAGE LOG — episode derivation + honest cause attribution.

The feature's whole value is that it does not lie about WHY a unit went dark, so
most of these tests are honesty tests: absent data must not be reported as an
outage, night must never be counted, a vendor's own fault code must beat our
inference, and an estimate must be None rather than fabricated when there is no
basis for it.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta

from api import inverter_outage_log as ool
from api.db import SessionLocal
from api.models import (
    AlertEvent, Array, Inverter, InverterDaily, Tenant,
)

TODAY = date(2026, 7, 18)        # pinned fleet-local "today"
WINDOW_END = TODAY - timedelta(days=1)   # 2026-07-17 — the last COMPLETE day


# ── fixtures ──────────────────────────────────────────────────────────────────

def _tenant() -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Outage Test", contact_email=f"{key}@t.test",
                      tenant_key=key, plan="standard", active=True,
                      product="array_operator"))
        db.commit()
    return tid, key


def _array(tid: str, name: str = "Londonderry") -> int:
    with SessionLocal() as db:
        a = Array(tenant_id=tid, name=name)
        db.add(a)
        db.flush()
        db.commit()
        return a.id


def _inverter(tid: str, aid: int, serial: str, nameplate: float | None = 10.0) -> int:
    with SessionLocal() as db:
        iv = Inverter(tenant_id=tid, array_id=aid, vendor="fronius", serial=serial,
                      name=f"Inverter {serial}", nameplate_kw=nameplate)
        db.add(iv)
        db.flush()
        db.commit()
        return iv.id


def _daily(tid: str, inv_id: int, days: list[date], kwh: float,
           error_code: str | None = None) -> None:
    with SessionLocal() as db:
        for d in days:
            db.add(InverterDaily(tenant_id=tid, inverter_id=inv_id, day=d,
                                 kwh=kwh, error_code=error_code))
        db.commit()


def _span(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _build(tid: str, inv_id: int, days: int = 60, today: date = TODAY):
    """Run the builder against a fresh session, as the endpoint does."""
    with SessionLocal() as db:
        tenant = db.get(Tenant, tid)
        return ool.build_outage_log(db, tenant, inv_id, days=days, today=today)


def _healthy_site(nameplate: float | None = 10.0, peers: int = 2,
                  start: date = date(2026, 6, 1)):
    """An array where everything produced 40 kWh/day from `start` through WINDOW_END.
    Returns (tenant_id, tenant_key, array_id, subject_inverter_id, [peer_ids])."""
    tid, key = _tenant()
    aid = _array(tid)
    sub = _inverter(tid, aid, "SUB-" + secrets.token_hex(3), nameplate)
    peer_ids = [_inverter(tid, aid, f"PEER{i}-" + secrets.token_hex(3), 10.0)
                for i in range(peers)]
    all_days = _span(start, WINDOW_END)
    for i in [sub, *peer_ids]:
        _daily(tid, i, all_days, 40.0)
    return tid, key, aid, sub, peer_ids


def _zero_out(tid: str, inv_id: int, days: list[date],
              error_code: str | None = None, delete: bool = False) -> None:
    """Turn already-healthy days into zeros (or, with delete=True, into MISSING rows —
    the crucial distinction between 'reported nothing' and 'told us zero')."""
    with SessionLocal() as db:
        for d in days:
            row = db.query(InverterDaily).filter_by(inverter_id=inv_id, day=d).one()
            if delete:
                db.delete(row)
            else:
                row.kwh = 0.0
                if error_code is not None:
                    row.error_code = error_code
        db.commit()


# ── episode derivation ────────────────────────────────────────────────────────

def test_contiguous_days_group_into_episodes_including_an_ongoing_one():
    tid, _key, _aid, sub, _peers = _healthy_site()
    old = _span(date(2026, 7, 1), date(2026, 7, 3))
    live = _span(date(2026, 7, 15), WINDOW_END)          # runs to the window edge
    _zero_out(tid, sub, old + live)

    out = _build(tid, sub)
    eps = out["episodes"]
    assert len(eps) == 2, eps

    # newest first — the live problem reads before the history
    now_ep, past_ep = eps
    assert now_ep["started_on"] == "2026-07-15"
    assert now_ep["ended_on"] is None and now_ep["ongoing"] is True
    assert now_ep["days"] == 3
    assert past_ep["started_on"] == "2026-07-01"
    assert past_ep["ended_on"] == "2026-07-03"
    assert past_ep["ongoing"] is False and past_ep["days"] == 3

    s = out["summary"]
    assert s["outage_days"] == 6 and s["episode_count"] == 2
    assert s["ongoing"] is True and s["ongoing_since"] == "2026-07-15"
    assert s["longest"]["days"] == 3
    assert s["state"] == "ongoing"


def test_a_single_gap_day_is_one_episode_and_a_clean_unit_reports_clean():
    tid, _key, _aid, sub, _peers = _healthy_site()
    out = _build(tid, sub)
    assert out["episodes"] == []
    assert out["summary"]["state"] == "clean"
    assert "run clean" in out["summary"]["headline"]

    _zero_out(tid, sub, [date(2026, 7, 5)])
    out = _build(tid, sub)
    assert len(out["episodes"]) == 1
    assert out["episodes"][0]["days"] == 1
    assert out["summary"]["state"] == "recovered"
    assert out["summary"]["last_ended_on"] == "2026-07-05"


# ── the night rule ────────────────────────────────────────────────────────────

def test_today_is_never_counted_as_an_outage():
    """An inverter is legitimately dark every night, and today is a partial day —
    so today's missing/zero row must never become an outage episode."""
    tid, _key, _aid, sub, _peers = _healthy_site()
    # No rows exist for TODAY at all (the healthy site stops at WINDOW_END).
    out = _build(tid, sub, today=TODAY)
    assert out["episodes"] == [], "today's absent row was wrongly counted"
    assert out["window"]["end"] == WINDOW_END.isoformat()

    # Roll the clock forward one day: what was "today" is now a COMPLETE day, and the
    # same missing row legitimately becomes an outage.
    out2 = _build(tid, sub, today=TODAY + timedelta(days=1))
    assert len(out2["episodes"]) == 1
    assert out2["episodes"][0]["started_on"] == TODAY.isoformat()
    assert out2["episodes"][0]["ongoing"] is True


def test_days_before_the_first_ever_reading_are_not_outages():
    tid, _key, _aid, sub, _peers = _healthy_site(start=date(2026, 7, 10))
    out = _build(tid, sub, days=180)
    assert out["episodes"] == []
    assert out["window"]["first_data_on"] == "2026-07-10"
    assert out["window"]["evaluated_from"] == "2026-07-10"


def test_an_inverter_with_no_history_says_so_instead_of_claiming_clean():
    tid, _key = _tenant()
    aid = _array(tid)
    sub = _inverter(tid, aid, "NEW-1")
    out = _build(tid, sub)
    assert out["summary"]["state"] == "no_history"
    assert "nothing to report" in out["summary"]["headline"]


# ── cause attribution ─────────────────────────────────────────────────────────

def test_vendor_error_code_wins_over_peer_inference():
    """Even when the peer picture would say 'site wide', the vendor's own code is
    fact and must be reported as the reason, verbatim."""
    tid, _key, _aid, sub, peers = _healthy_site()
    days = _span(date(2026, 7, 8), date(2026, 7, 9))
    _zero_out(tid, sub, days, error_code="State 306 - No Power")
    for p in peers:                    # whole site zero too => would infer site_wide
        _zero_out(tid, p, days)

    ep = _build(tid, sub)["episodes"][0]
    assert ep["cause_kind"] == "vendor_code"
    assert ep["vendor_codes"] == ["State 306 - No Power"]
    assert "State 306 - No Power" in ep["cause"]
    assert "not our inference" in ep["cause"]


def test_site_wide_zero_is_not_blamed_on_this_inverter():
    tid, _key, _aid, sub, peers = _healthy_site()
    days = _span(date(2026, 7, 8), date(2026, 7, 9))
    for i in [sub, *peers]:
        _zero_out(tid, i, days)

    ep = _build(tid, sub)["episodes"][0]
    assert ep["cause_kind"] == "site_wide"
    assert "whole site" in ep["cause"]
    assert ep["evidence"]["peers_producing_days"] == 0
    # No producing peer => no honest basis for a loss estimate.
    assert ep["lost_kwh_est"] is None


def test_unit_specific_zero_while_peers_produced():
    tid, _key, _aid, sub, _peers = _healthy_site()
    days = _span(date(2026, 7, 8), date(2026, 7, 9))
    _zero_out(tid, sub, days)

    ep = _build(tid, sub)["episodes"][0]
    assert ep["cause_kind"] == "unit"
    assert "alone" in ep["cause"]
    assert ep["evidence"]["peers_producing_days"] == 2


def test_two_units_down_together_are_not_each_described_as_alone():
    """Real prod case (Benson Site, inverters 15 + 16, both dark from 2026-07-09):
    two units down at once is still unit-specific, but calling each of them "alone"
    is a small avoidable lie — and "two units down" is a different service call."""
    tid, _key, _aid, sub, peers = _healthy_site(peers=3)
    days = _span(date(2026, 7, 8), date(2026, 7, 9))
    _zero_out(tid, sub, days)
    _zero_out(tid, peers[0], days)          # a sibling goes down over the same period

    ep = _build(tid, sub)["episodes"][0]
    assert ep["cause_kind"] == "unit"        # the rest of the site produced => still ours
    assert "alone" not in ep["cause"]
    assert "1 other unit" in ep["cause"]
    assert ep["evidence"]["peers_also_down"] == 1

    # ...while a genuinely solitary failure still says "alone".
    tid2, _k2, _a2, sub2, _p2 = _healthy_site(peers=3)
    _zero_out(tid2, sub2, days)
    ep2 = _build(tid2, sub2)["episodes"][0]
    assert "This inverter alone" in ep2["cause"]
    assert ep2["evidence"]["peers_also_down"] == 0


def test_absent_rows_are_no_data_not_an_outage_verdict():
    """Absent != zero. If the array sent nothing, production is UNKNOWN — we list
    the gap but must not claim the inverter was down."""
    tid, _key, _aid, sub, peers = _healthy_site()
    days = _span(date(2026, 7, 8), date(2026, 7, 9))
    for i in [sub, *peers]:
        _zero_out(tid, i, days, delete=True)

    ep = _build(tid, sub)["episodes"][0]
    assert ep["cause_kind"] == "no_data"
    assert "unknown, not zero" in ep["cause"]
    assert ep["evidence"]["array_rows_present"] is False
    assert ep["lost_kwh_est"] is None

    # ...and the same shape with rows PRESENT at zero classifies differently.
    tid2, _k2, _a2, sub2, peers2 = _healthy_site()
    for i in [sub2, *peers2]:
        _zero_out(tid2, i, days)
    assert _build(tid2, sub2)["episodes"][0]["cause_kind"] == "site_wide"


def test_solo_inverter_zero_is_honestly_unknown():
    tid, _key, _aid, sub, _peers = _healthy_site(peers=0)
    _zero_out(tid, sub, _span(date(2026, 7, 8), date(2026, 7, 9)))
    ep = _build(tid, sub)["episodes"][0]
    assert ep["cause_kind"] == "unknown"
    assert "only unit on this array" in ep["cause"]
    assert ep["lost_kwh_est"] is None


def test_an_overlapping_ticket_is_attached():
    tid, _key, aid, sub, _peers = _healthy_site()
    _zero_out(tid, sub, _span(date(2026, 7, 8), date(2026, 7, 9)))
    with SessionLocal() as db:
        iv = db.get(Inverter, sub)
        db.add(AlertEvent(tenant_id=tid, array_id=aid, array_name="Londonderry",
                          inverter_ref=iv.name, title="Inverter stopped producing",
                          severity="critical", status="open",
                          created_at=datetime(2026, 7, 8, 12, 0)))
        db.commit()

    ep = _build(tid, sub)["episodes"][0]
    assert ep["ticket"] is not None
    assert ep["ticket"]["title"] == "Inverter stopped producing"
    assert ep["ticket"]["severity"] == "critical"


# ── the lost-kWh estimate ─────────────────────────────────────────────────────

def test_lost_kwh_estimate_uses_peer_median_per_kw_times_nameplate():
    # peers: 40 kWh on a 10 kW unit = 4.0 kWh/kW/day; subject nameplate 10 kW
    # => 40 kWh/day * 3 days = 120.0
    tid, _key, _aid, sub, _peers = _healthy_site(nameplate=10.0)
    _zero_out(tid, sub, _span(date(2026, 7, 8), date(2026, 7, 10)))
    ep = _build(tid, sub)["episodes"][0]
    assert ep["lost_kwh_est"] == 120.0
    assert ep["lost_kwh_is_estimate"] is True
    assert "median output-per-kW" in ep["lost_kwh_basis"]

    # A half-size unit loses half as much.
    tid2, _k2, _a2, sub2, _p2 = _healthy_site(nameplate=5.0)
    _zero_out(tid2, sub2, _span(date(2026, 7, 8), date(2026, 7, 10)))
    assert _build(tid2, sub2)["episodes"][0]["lost_kwh_est"] == 60.0


def test_lost_kwh_is_null_without_a_nameplate_rather_than_guessed():
    tid, _key, _aid, sub, _peers = _healthy_site(nameplate=None)
    _zero_out(tid, sub, _span(date(2026, 7, 8), date(2026, 7, 10)))
    ep = _build(tid, sub)["episodes"][0]
    assert ep["lost_kwh_est"] is None
    assert "No nameplate" in ep["lost_kwh_basis"]


def test_lost_kwh_is_null_without_producing_peers():
    tid, _key, _aid, sub, _peers = _healthy_site(peers=0)
    _zero_out(tid, sub, _span(date(2026, 7, 8), date(2026, 7, 10)))
    ep = _build(tid, sub)["episodes"][0]
    assert ep["lost_kwh_est"] is None
    assert "no honest baseline" in ep["lost_kwh_basis"]


# ── the endpoint: auth + tenant isolation ─────────────────────────────────────

def test_endpoint_returns_the_log_for_the_owning_tenant(client):
    tid, key, _aid, sub, _peers = _healthy_site()
    _zero_out(tid, sub, _span(date(2026, 7, 8), date(2026, 7, 9)))
    r = client.get(f"/v1/array-owners/inverters/{sub}/outages?days=60",
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inverter"]["id"] == sub
    assert body["window"]["days"] == 60
    assert len(body["episodes"]) >= 1
    assert body["episodes"][0]["cause_kind"] in {
        "vendor_code", "site_wide", "no_data", "unit", "unknown"}


def test_another_tenants_inverter_404s(client):
    tid, _key, _aid, sub, _peers = _healthy_site()
    _other_tid, other_key = _tenant()
    r = client.get(f"/v1/array-owners/inverters/{sub}/outages",
                   headers={"Authorization": f"Bearer {other_key}"})
    assert r.status_code == 404, r.text


def test_unknown_inverter_404s(client):
    _tid, key = _tenant()
    r = client.get("/v1/array-owners/inverters/99999999/outages",
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 404
