"""
capture-debt must flag a co-op as stale by its browser-fed GENERATION stream —
catching a real co-op customer that has fresh browser data but NO stored session
(the session-driven loop's blind spot, prod 2026-07-02: ten_bae078ae4c81bb24).

There is deliberately no server-side SmartHub pull (the NISC usage API is
cookie-bound), so the browser is the ONLY generation source; the capture-debt
drain is what keeps co-ops fresh without a human. These tests pin the DATA-stream
staleness signal that drives that drain.
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from api.db import SessionLocal
from api.capture_debt import _stale_coop_providers, UTILITY_STALE_DAYS
from api.models import Array, Client, DailyGeneration, Tenant, UtilityAccount


def _mk_tenant() -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name="Coop Debt Test", contact_email=f"{tid}@t.t",
            tenant_key="k_" + secrets.token_hex(8), plan="standard", active=True,
        ))
        db.commit()
    return tid


def _mk_array_with_coop(tid: str, provider: str, acct: str) -> int:
    with SessionLocal() as db:
        c = Client(tenant_id=tid, name="C " + acct, active=True)
        db.add(c); db.flush()
        arr = Array(tenant_id=tid, client_id=c.id, name="Arr " + acct)
        db.add(arr); db.flush()
        db.add(UtilityAccount(
            tenant_id=tid, array_id=arr.id, provider=provider,
            account_number=acct, enabled=True,
        ))
        db.commit()
        return arr.id


def _seed_gen(tid: str, array_id: int, day: date, source: str = "utility_meter") -> None:
    with SessionLocal() as db:
        db.add(DailyGeneration(tenant_id=tid, array_id=array_id, day=day,
                               kwh=12.5, source=source))
        db.commit()


def test_stale_coop_flagged_without_a_session():
    """The exact prod gap: fresh-ish-but-STALE browser data, NO UtilitySession."""
    tid = _mk_tenant()
    array_id = _mk_array_with_coop(tid, "vec", "6578300")
    stale_day = date.today() - timedelta(days=UTILITY_STALE_DAYS + 1)
    _seed_gen(tid, array_id, stale_day)

    with SessionLocal() as db:
        stale = _stale_coop_providers(db, tid)

    assert "vec" in stale
    assert stale["vec"]["reason"].startswith("gen_stale_")
    assert stale["vec"]["last_day"] == stale_day.isoformat()


def test_fresh_coop_not_flagged():
    tid = _mk_tenant()
    array_id = _mk_array_with_coop(tid, "vec", "111")
    _seed_gen(tid, array_id, date.today() - timedelta(days=1))
    with SessionLocal() as db:
        assert _stale_coop_providers(db, tid) == {}


def test_coop_with_no_generation_not_flagged():
    """Never-captured is onboarding's problem, not debt (no false nag)."""
    tid = _mk_tenant()
    _mk_array_with_coop(tid, "wec", "222")
    with SessionLocal() as db:
        assert _stale_coop_providers(db, tid) == {}


def test_non_smarthub_provider_ignored():
    """A stale GMP array must NOT surface as a co-op drain (GMP has its own path)."""
    tid = _mk_tenant()
    array_id = _mk_array_with_coop(tid, "gmp", "333")
    _seed_gen(tid, array_id, date.today() - timedelta(days=30))
    with SessionLocal() as db:
        assert _stale_coop_providers(db, tid) == {}


def test_discovered_sh_provider_flagged():
    """Discovered co-ops (sh_<subdomain>) are SmartHub too."""
    tid = _mk_tenant()
    array_id = _mk_array_with_coop(tid, "sh_missoulaelectric", "444")
    _seed_gen(tid, array_id, date.today() - timedelta(days=UTILITY_STALE_DAYS + 3))
    with SessionLocal() as db:
        assert "sh_missoulaelectric" in _stale_coop_providers(db, tid)
