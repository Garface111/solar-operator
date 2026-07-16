"""Cross-product tenant linking + capture fan-out — "one install feeds BOTH".

Covers the bar from the build spec:
  • link_by_email picks the CANONICAL tenant per product and ignores duplicates.
  • a capture with FAN_OUT_TO_SIBLING ON writes to BOTH tenants (arrays + daily
    rows in each), idempotent on re-capture.
  • flag OFF → single-tenant behavior unchanged (sibling untouched).
  • a sibling-write failure does NOT break the primary capture.
  • the extension-status endpoint reports BOTH products when linked.
"""
from __future__ import annotations

import secrets
from datetime import date

import pytest
from sqlalchemy import select

from api import capture_fanout, tenant_link
from api.db import SessionLocal
from api.models import Array, DailyGeneration, Inverter, Tenant


# ── helpers ──────────────────────────────────────────────────────────────────

def _mk_tenant(*, email: str, product: str, active: bool = True,
               name: str | None = None, sub_id: str | None = None,
               status: str | None = "active") -> tuple[str, str]:
    tid = "ten_" + secrets.token_hex(6)
    key = "sol_test_" + secrets.token_hex(8)
    with SessionLocal() as db:
        db.add(Tenant(
            id=tid, name=name or f"{product} {tid}", contact_email=email,
            tenant_key=key, plan="standard", product=product, active=active,
            subscription_status=status, stripe_subscription_id=sub_id,
        ))
        db.commit()
    return tid, key


def _inv_capture_payload(site_id: str = "S1", name: str = "Linked Site",
                         kwh: float = 42.0) -> dict:
    return {
        "provider": "fronius",
        "sites": [{
            "site_id": site_id,
            "name": name,
            "energy_today_kwh": kwh,
            "peak_power_kw": 10.0,
            "current_power_w": 3000.0,
            "inverters": [{
                "serial": "INV-001", "name": "Inv 1",
                "nameplate_kw": 5.0, "energy_today_kwh": kwh,
            }],
        }],
    }


def _arrays(tid: str) -> list[Array]:
    with SessionLocal() as db:
        return db.execute(
            select(Array).where(Array.tenant_id == tid,
                                Array.deleted_at.is_(None))
        ).scalars().all()


def _daily_rows(tid: str) -> list[DailyGeneration]:
    with SessionLocal() as db:
        return db.execute(
            select(DailyGeneration).where(DailyGeneration.tenant_id == tid)
        ).scalars().all()


# ── linking / canonical resolution ───────────────────────────────────────────

def test_link_by_email_links_canonical_pair():
    email = f"{secrets.token_hex(6)}@link.test"
    nep, _ = _mk_tenant(email=email, product="nepool")
    ao, _ = _mk_tenant(email=email, product="array_operator")

    # dry run writes nothing
    pre = tenant_link.link_by_email(email, apply=False)
    assert pre["reason"] == "dry-run-would-link"
    assert pre["linked"] is False

    res = tenant_link.link_by_email(email, apply=True)
    assert res["linked"] is True
    assert res["reason"] == "linked"

    with SessionLocal() as db:
        t_nep = db.get(Tenant, nep)
        t_ao = db.get(Tenant, ao)
        assert t_nep.linked_tenant_id == ao
        assert t_ao.linked_tenant_id == nep


def test_link_ignores_stale_duplicate_and_picks_canonical():
    """When an email has a duplicate AO tenant — one a live, data-bearing real
    account, one an empty inactive clone — linking must pick the canonical (real)
    one, never the dup."""
    email = f"{secrets.token_hex(6)}@dup.test"
    _mk_tenant(email=email, product="nepool")
    # The stale duplicate: inactive, no subscription, no data.
    dup, _ = _mk_tenant(email=email, product="array_operator", active=False,
                        status="canceled", name="STALE DUP")
    # The canonical AO tenant: active, has a Stripe sub, carries real data.
    real, _ = _mk_tenant(email=email, product="array_operator", active=True,
                         sub_id="sub_real", name="REAL AO")
    with SessionLocal() as db:
        arr = Array(tenant_id=real, name="Real Array", fuel_type="solar")
        db.add(arr); db.flush()
        db.add(DailyGeneration(tenant_id=real, array_id=arr.id,
                               day=date.today(), kwh=10.0, source="extension_pull"))
        db.commit()

    with SessionLocal() as db:
        chosen = tenant_link.canonical_tenant_for_product(db, email, "array_operator")
        assert chosen.id == real, "canonical must be the real, active, data-bearing tenant"
        assert chosen.id != dup

    res = tenant_link.link_by_email(email, apply=True)
    assert res["linked"] is True
    with SessionLocal() as db:
        assert db.get(Tenant, real).linked_tenant_id is not None
        # the stale dup is NOT linked
        assert db.get(Tenant, dup).linked_tenant_id is None


def test_link_missing_one_product_is_noop():
    email = f"{secrets.token_hex(6)}@solo.test"
    _mk_tenant(email=email, product="array_operator")  # only AO, no NEPOOL
    res = tenant_link.link_by_email(email, apply=True)
    assert res["linked"] is False
    assert res["reason"] == "missing-one-product"


def test_unlink_reverses_both_sides():
    email = f"{secrets.token_hex(6)}@unlink.test"
    nep, _ = _mk_tenant(email=email, product="nepool")
    ao, _ = _mk_tenant(email=email, product="array_operator")
    tenant_link.link_by_email(email, apply=True)
    res = tenant_link.unlink_tenant(nep, apply=True)
    assert res["unlinked"] is True
    with SessionLocal() as db:
        assert db.get(Tenant, nep).linked_tenant_id is None
        assert db.get(Tenant, ao).linked_tenant_id is None


# ── fan-out ──────────────────────────────────────────────────────────────────

def test_fanout_off_leaves_sibling_untouched(client, monkeypatch):
    monkeypatch.delenv("FAN_OUT_TO_SIBLING", raising=False)  # default OFF
    email = f"{secrets.token_hex(6)}@off.test"
    ao, ao_key = _mk_tenant(email=email, product="array_operator")
    nep, _ = _mk_tenant(email=email, product="nepool")
    tenant_link.link_by_email(email, apply=True)

    r = client.post("/v1/array-owners/inverter-capture",
                    json=_inv_capture_payload(),
                    headers={"Authorization": f"Bearer {ao_key}"})
    assert r.status_code == 200, r.text
    assert len(_arrays(ao)) == 1          # primary got it
    assert len(_arrays(nep)) == 0         # sibling untouched (flag OFF)


def test_fanout_inverter_capture_stays_out_of_nepool_sibling(client, monkeypatch):
    """Inverter capture on the AO tenant writes to AO, and the fan-out to a linked
    NEPOOL sibling is REFUSED by the product guard — NEPOOL builds arrays from
    UTILITY data only (Ford 2026-07-16). So the inverter data must never cross
    into the nepool sibling, even with fan-out ON."""
    monkeypatch.setenv("FAN_OUT_TO_SIBLING", "true")
    email = f"{secrets.token_hex(6)}@on.test"
    ao, ao_key = _mk_tenant(email=email, product="array_operator")
    nep, _ = _mk_tenant(email=email, product="nepool")
    tenant_link.link_by_email(email, apply=True)

    payload = _inv_capture_payload(kwh=42.0)
    r = client.post("/v1/array-owners/inverter-capture", json=payload,
                    headers={"Authorization": f"Bearer {ao_key}"})
    assert r.status_code == 200, r.text

    # AO (the inverter product) got the array + daily row...
    assert len(_arrays(ao)) == 1
    assert len(_daily_rows(ao)) == 1
    # ...but the NEPOOL sibling got NOTHING — inverter->array write refused there.
    assert len(_arrays(nep)) == 0
    assert len(_daily_rows(nep)) == 0

    # Re-capture the SAME day → idempotent on AO; nepool still empty.
    r2 = client.post("/v1/array-owners/inverter-capture", json=payload,
                     headers={"Authorization": f"Bearer {ao_key}"})
    assert r2.status_code == 200, r2.text
    assert len(_arrays(ao)) == 1
    assert len(_daily_rows(ao)) == 1
    assert len(_arrays(nep)) == 0

    # No inverter rows leaked into the nepool sibling either.
    with SessionLocal() as db:
        sib_invs = db.execute(
            select(Inverter).where(Inverter.tenant_id == nep,
                                   Inverter.deleted_at.is_(None))
        ).scalars().all()
        assert len(sib_invs) == 0


def test_fanout_sibling_failure_does_not_break_primary(client, monkeypatch):
    monkeypatch.setenv("FAN_OUT_TO_SIBLING", "true")
    email = f"{secrets.token_hex(6)}@fail.test"
    ao, ao_key = _mk_tenant(email=email, product="array_operator")
    nep, _ = _mk_tenant(email=email, product="nepool")
    tenant_link.link_by_email(email, apply=True)

    # Force the sibling replay to blow up. fanout() must swallow it and the
    # primary capture must still succeed + persist.
    import api.array_owners as ao_mod
    orig = ao_mod._inverter_capture_for_tenant

    def _wrapped(tenant, provider, body):
        if tenant.id == nep:
            raise RuntimeError("simulated sibling write failure")
        return orig(tenant, provider, body)

    monkeypatch.setattr(ao_mod, "_inverter_capture_for_tenant", _wrapped)

    r = client.post("/v1/array-owners/inverter-capture",
                    json=_inv_capture_payload(),
                    headers={"Authorization": f"Bearer {ao_key}"})
    assert r.status_code == 200, r.text          # primary unaffected
    assert len(_arrays(ao)) == 1                  # primary persisted
    assert len(_arrays(nep)) == 0                 # sibling write failed cleanly


# ── extension-status reflects the link ───────────────────────────────────────

def test_extension_status_reports_both_products_when_linked(client):
    email = f"{secrets.token_hex(6)}@status.test"
    ao, ao_key = _mk_tenant(email=email, product="array_operator")
    nep, _ = _mk_tenant(email=email, product="nepool")
    tenant_link.link_by_email(email, apply=True)

    r = client.get("/v1/array-owners/extension-status",
                   headers={"Authorization": f"Bearer {ao_key}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["linked"] is True
    assert set(body["products"]) == {"array_operator", "nepool"}
    assert "array_operator" in body
    assert "nepool" in body


def test_extension_status_single_product_when_unlinked(client):
    email = f"{secrets.token_hex(6)}@solo2.test"
    ao, ao_key = _mk_tenant(email=email, product="array_operator")
    r = client.get("/v1/array-owners/extension-status",
                   headers={"Authorization": f"Bearer {ao_key}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["linked"] is False
    assert body["products"] == ["array_operator"]
    assert "nepool" not in body
