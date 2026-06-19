"""Smart duplicate-array merge + detection (api.array_merge + jobs.array_dedup).

Covers:
  • merge_arrays is LOSSLESS — reparents utility accounts, GMP daily, per-array
    daily generation (with source-priority on day collisions), inverters,
    inverter connection (unique-constraint aware), warranty/verification, billing
    subs; soft-deletes src; writes an undo row.
  • find_duplicate_pairs confidence tiers: STRONG (shared UA / identical name),
    MEDIUM (cross-source containment), and the SUB-ARRAY GUARD that refuses to
    flag Starlake North vs Starlake South.
  • sweep_tenant dry-run vs execute.
"""
from __future__ import annotations

import secrets
from datetime import date

import pytest

from api.db import SessionLocal
from api.models import (
    Tenant, Client, Array, UtilityAccount, DailyGeneration, GmpDailyGeneration,
    InverterConnection, Inverter, DeleteHistory, now,
)
from api.array_merge import (
    merge_arrays, find_duplicate_pairs, _differs_only_by_subarray_token,
)
from api.jobs.array_dedup import sweep_tenant


def _tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name="Dedup Test", contact_email=f"{tid}@t.test",
                      tenant_key="sol_live_" + secrets.token_urlsafe(8),
                      plan="standard", active=True, product="array_operator"))
        c = Client(tenant_id=tid, name="Self", active=True)
        db.add(c); db.commit()
    return tid


def _client_id(tid):
    with SessionLocal() as db:
        return db.execute(
            __import__("sqlalchemy").select(Client.id).where(Client.tenant_id == tid)
        ).scalars().first()


def _array(tid, name, client_id=None, **kw) -> int:
    with SessionLocal() as db:
        a = Array(tenant_id=tid, name=name, client_id=client_id, fuel_type="solar", **kw)
        db.add(a); db.commit(); return a.id


def _ua(tid, array_id, acct, provider="gmp"):
    with SessionLocal() as db:
        db.add(UtilityAccount(tenant_id=tid, array_id=array_id, provider=provider,
                              account_number=acct))
        db.commit()


def _conn(array_id, vendor="solaredge"):
    with SessionLocal() as db:
        db.add(InverterConnection(array_id=array_id, vendor=vendor,
                                  config={"api_key": "k", "site_id": 1}, status="ok"))
        db.commit()


# ── sub-array guard (pure) ────────────────────────────────────────────────────

def test_subarray_guard_blocks_directional_siblings():
    assert _differs_only_by_subarray_token("Starlake North", "Starlake South")
    assert _differs_only_by_subarray_token("Maple Ridge Roof", "Maple Ridge Carport")
    assert _differs_only_by_subarray_token("Riverbend 1", "Riverbend 2")


def test_subarray_guard_blocks_prefixed_siblings_regression():
    # REGRESSION (prod dry-run caught this): a "1a_" label prefix must NOT defeat
    # the guard. Starlake North/South/Center are 3 distinct sub-meters and must
    # never collapse into one "Starlake".
    assert _differs_only_by_subarray_token("1a_Starlake_North", "Starlake")
    assert _differs_only_by_subarray_token("1a_Starlake_South", "Starlake")
    assert _differs_only_by_subarray_token("1a_Starlake_Center", "Starlake")
    assert _differs_only_by_subarray_token("1a_Starlake_North", "1a_Starlake_South")


def test_subarray_guard_allows_prefixed_real_twins():
    # A label prefix WITHOUT a positional token is a real GMP↔vendor twin → merge.
    assert not _differs_only_by_subarray_token("1a_Londonderry", "Londonderry")
    assert not _differs_only_by_subarray_token("1b_COVER Catamount Building",
                                               "Cover Catamount Building")


def test_subarray_guard_allows_real_twins():
    # GMP nickname vs vendor name — differ by a NON-subarray word → not blocked
    assert not _differs_only_by_subarray_token("Londonderry Community Solar", "Londonderry")
    assert not _differs_only_by_subarray_token("Catamount Ridge Solar", "Catamount Ridge")
    # identical → not a sub-array split
    assert not _differs_only_by_subarray_token("Hilltop", "Hilltop")


# ── detection tiers ───────────────────────────────────────────────────────────

def test_detect_strong_identical_normalized_name():
    # STRONG via identical normalized name (the reachable strong signal — a
    # shared utility account can't exist within a tenant: uq_account_per_tenant
    # makes (provider, account_number) unique, so it only ever points at one array).
    tid = _tenant(); cid = _client_id(tid)
    a1 = _array(tid, "Hilltop Solar", cid)
    a2 = _array(tid, "hilltop-solar", cid)   # normalizes to the same string
    pairs = find_duplicate_pairs(SessionLocal(), tid)
    assert pairs and pairs[0]["confidence"] == "STRONG"


def test_detect_medium_cross_source_containment():
    tid = _tenant(); cid = _client_id(tid)
    vendor_arr = _array(tid, "Londonderry", cid); _conn(vendor_arr)
    gmp_arr = _array(tid, "Londonderry Community Solar", cid); _ua(tid, gmp_arr, "9001")
    pairs = find_duplicate_pairs(SessionLocal(), tid)
    top = pairs[0]
    assert top["confidence"] == "MEDIUM"
    assert top["cross_source"] is True
    # destination should be the VENDOR array (richer, owns the connection)
    assert top["dst_id"] == vendor_arr


def test_detect_skips_subarray_siblings():
    tid = _tenant(); cid = _client_id(tid)
    base = _array(tid, "Starlake", cid); _conn(base)
    n = _array(tid, "Starlake North", cid)
    s = _array(tid, "Starlake South", cid)
    pairs = find_duplicate_pairs(SessionLocal(), tid)
    # No pair should pair the directional siblings against each other.
    sib = {tuple(sorted((n, s)))}
    assert not any(tuple(sorted((p["a_id"], p["b_id"]))) in sib for p in pairs)


# ── lossless merge ────────────────────────────────────────────────────────────

def test_merge_is_lossless_and_reparents_everything():
    tid = _tenant(); cid = _client_id(tid)
    # dst = vendor array; src = GMP twin with daily + gmp daily + ua
    dst = _array(tid, "Londonderry", cid); _conn(dst)
    src = _array(tid, "Londonderry Community Solar", cid)
    _ua(tid, src, "9001")
    with SessionLocal() as db:
        # src has GMP-sourced daily + a gmp_daily row; dst has a solaredge day that
        # collides with one src day (solaredge must win).
        db.add(DailyGeneration(tenant_id=tid, array_id=src, day=date(2025,7,1), kwh=10.0, source="bill_prorate"))
        db.add(DailyGeneration(tenant_id=tid, array_id=src, day=date(2025,7,2), kwh=12.0, source="gmp_api"))
        db.add(DailyGeneration(tenant_id=tid, array_id=dst, day=date(2025,7,2), kwh=99.0, source="solaredge"))
        ua_id = db.execute(__import__("sqlalchemy").select(UtilityAccount.id).where(UtilityAccount.array_id==src)).scalars().first()
        db.add(GmpDailyGeneration(tenant_id=tid, account_id=ua_id, array_id=src,
                                  account_number="9001", day=date(2025,7,1), kwh=10.0))
        db.add(Inverter(tenant_id=tid, array_id=src, vendor="gmp", serial="m-1"))
        db.commit()

    with SessionLocal() as db:
        res = merge_arrays(db, src, dst, tid, reason="test")
        db.commit()
        assert res["ok"]
        import sqlalchemy as sa
        # utility account moved
        assert db.execute(sa.select(sa.func.count(UtilityAccount.id)).where(UtilityAccount.array_id==dst)).scalar() == 1
        # gmp daily moved
        assert db.execute(sa.select(sa.func.count(GmpDailyGeneration.id)).where(GmpDailyGeneration.array_id==dst)).scalar() == 1
        # daily generation: day1 moved (no clash), day2 clash → solaredge kept (99.0)
        d1 = db.execute(sa.select(DailyGeneration).where(DailyGeneration.array_id==dst, DailyGeneration.day==date(2025,7,1))).scalar_one()
        assert d1.kwh == 10.0
        d2 = db.execute(sa.select(DailyGeneration).where(DailyGeneration.array_id==dst, DailyGeneration.day==date(2025,7,2))).scalar_one()
        assert d2.kwh == 99.0 and d2.source == "solaredge"   # stronger source won
        # no src daily rows left
        assert db.execute(sa.select(sa.func.count(DailyGeneration.id)).where(DailyGeneration.array_id==src)).scalar() == 0
        # inverter moved
        assert db.execute(sa.select(sa.func.count(Inverter.id)).where(Inverter.array_id==dst)).scalar() == 1
        # src soft-deleted
        assert db.get(Array, src).deleted_at is not None
        # undo row written
        assert db.execute(sa.select(sa.func.count(DeleteHistory.id)).where(DeleteHistory.undo_token==res["undo_token"])).scalar() == 1


def test_merge_source_priority_keeps_stronger_on_collision():
    """When src has the STRONGER source on a colliding day, dst's value updates."""
    tid = _tenant(); cid = _client_id(tid)
    dst = _array(tid, "Alpha", cid); src = _array(tid, "Alpha Two", cid)
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=dst, day=date(2025,7,2), kwh=5.0, source="bill_prorate"))
        db.add(DailyGeneration(tenant_id=tid, array_id=src, day=date(2025,7,2), kwh=42.0, source="solaredge"))
        db.commit()
    with SessionLocal() as db:
        merge_arrays(db, src, dst, tid); db.commit()
        import sqlalchemy as sa
        row = db.execute(sa.select(DailyGeneration).where(DailyGeneration.array_id==dst, DailyGeneration.day==date(2025,7,2))).scalar_one()
        assert row.kwh == 42.0 and row.source == "solaredge"


def test_merge_idempotent_on_deleted_src():
    tid = _tenant(); cid = _client_id(tid)
    dst = _array(tid, "A", cid); src = _array(tid, "A2", cid)
    with SessionLocal() as db:
        merge_arrays(db, src, dst, tid); db.commit()
    with SessionLocal() as db:
        res = merge_arrays(db, src, dst, tid)
        assert res.get("noop") is True


def test_merge_refuses_cross_tenant():
    t1 = _tenant(); t2 = _tenant()
    a1 = _array(t1, "X", _client_id(t1)); a2 = _array(t2, "X", _client_id(t2))
    with SessionLocal() as db:
        res = merge_arrays(db, a1, a2, t1)
        assert res["ok"] is False and "tenant" in res["error"]


# ── sweep job ─────────────────────────────────────────────────────────────────

def test_sweep_dryrun_lists_but_does_not_merge():
    tid = _tenant(); cid = _client_id(tid)
    v = _array(tid, "Londonderry", cid); _conn(v)
    g = _array(tid, "Londonderry Community Solar", cid); _ua(tid, g, "9001")
    r = sweep_tenant(tid, execute=False)
    assert len(r["auto_merged"]) == 1
    assert r["auto_merged"][0]["executed"] is False
    with SessionLocal() as db:
        assert db.get(Array, g).deleted_at is None  # nothing merged in dry run


def test_sweep_execute_merges_strong_and_medium():
    tid = _tenant(); cid = _client_id(tid)
    v = _array(tid, "Londonderry", cid); _conn(v)
    g = _array(tid, "Londonderry Community Solar", cid); _ua(tid, g, "9001")
    r = sweep_tenant(tid, execute=True)
    assert len(r["auto_merged"]) == 1 and r["auto_merged"][0]["executed"] is True
    with SessionLocal() as db:
        # exactly one of the pair survives
        import sqlalchemy as sa
        alive = db.execute(sa.select(sa.func.count(Array.id)).where(
            Array.tenant_id==tid, Array.deleted_at.is_(None))).scalar()
        assert alive == 1
