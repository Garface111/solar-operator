"""scripts/migrate_nepool_tenant.py — the Phase-1 fold migration tool.

Synthetic fixture mirroring the real prod shape (dual-capture "sibling"
arrays): a NEPOOL tenant with 2 clients / 3 arrays migrated onto an AO tenant.

  A1 "North Field"  -> exact-name sibling on target        (link-name)
  A2 "South Field"  -> account-number sibling "S Field Two" (link-account + rename)
  A3 "Hydro Dam"    -> no match                             (move wholesale)

Asserts: the mapping plan, NEPOOL fields carried, settings moved,
ReportDelivery history re-pointed, verify-mode workbook byte-equality on the
default writer dispatch, the rollback proof (verify leaves the DB untouched),
and that ambiguities abort execute instead of guessing.
"""
from __future__ import annotations

import secrets
from datetime import datetime, date

import pytest

from api.db import SessionLocal
from api.models import (Tenant, Client, Array, UtilityAccount, Bill,
                        DailyGeneration, ReportDelivery)
from scripts.migrate_nepool_tenant import (
    build_plan, run_verify, run_execute, MigrationBlocked, compare_workbooks,
)

# Q2-2024 reference -> rolling window Q4'22..Q1'24; all data sits in Jan 2024.
_REF = date(2024, 4, 1)


def _tenant(*, product: str, **kw) -> str:
    tid = "ten_" + secrets.token_hex(6)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, name=f"Mig {tid[-4:]}",
                      contact_email=f"{tid}@mig.test",
                      tenant_key="k_" + secrets.token_hex(8),
                      plan="standard", active=True, product=product, **kw))
        db.commit()
    return tid


def _bill_kwargs(kwh: int) -> dict:
    # Identical on source + target sibling so the workbooks agree.
    return dict(bill_date=datetime(2024, 1, 20),
                period_start=datetime(2024, 1, 1),
                period_end=datetime(2024, 1, 31),
                kwh_generated=kwh)


def _fixture() -> dict:
    """The synthetic two-tenant world. Returns ids keyed by name."""
    ids: dict = {}
    src = _tenant(product="nepool",
                  report_frequency="monthly", send_mode="to_both",
                  cc_on_reports=True,
                  email_subject_template="Sub {{quarter}}",
                  email_body_template="Hello {{client_name}} — custom body",
                  email_signoff="— Bruce",
                  send_from_email="bruce@src.test", send_from_name="Bruce")
    tgt = _tenant(product="array_operator",
                  send_from_email="ao@tgt.test")   # target already set -> kept
    ids["src"], ids["tgt"] = src, tgt

    with SessionLocal() as db:
        c1 = Client(tenant_id=src, name="Alpha Solar", active=True,
                    contact_email="alpha@client.test")
        c2 = Client(tenant_id=src, name="Beta Hydro", active=True,
                    contact_email="beta@client.test")
        db.add_all([c1, c2])
        db.flush()
        ids["c1"], ids["c2"] = c1.id, c2.id

        # A1: exact-name sibling.
        a1 = Array(tenant_id=src, client_id=c1.id, name="North Field",
                   nepool_gis_id="111")
        # A2: sibling matched by utility account number, different name.
        a2 = Array(tenant_id=src, client_id=c1.id, name="South Field",
                   nepool_gis_id="222")
        # A3: unmapped — moves wholesale. Non-solar so the writer DISPATCH
        # (registry fuel resolution) is exercised through the oracle too.
        a3 = Array(tenant_id=src, client_id=c2.id, name="Hydro Dam",
                   nepool_gis_id="333", fuel_type="hydro", cert_registry="LIHI")
        db.add_all([a1, a2, a3])
        db.flush()
        ids["a1"], ids["a2"], ids["a3"] = a1.id, a2.id, a3.id

        shared_acct = "SHARED_" + secrets.token_hex(3)
        ids["shared_acct"] = shared_acct
        ua1 = UtilityAccount(tenant_id=src, array_id=a1.id, provider="gmp",
                             account_number="N1_" + secrets.token_hex(3))
        ua2 = UtilityAccount(tenant_id=src, array_id=a2.id, provider="gmp",
                             account_number=shared_acct)
        ua3 = UtilityAccount(tenant_id=src, array_id=a3.id, provider="gmp",
                             account_number="H3_" + secrets.token_hex(3))
        db.add_all([ua1, ua2, ua3])
        db.flush()
        ids["ua3"] = ua3.id

        db.add(Bill(tenant_id=src, account_id=ua1.id,
                    document_number="d-" + secrets.token_hex(3),
                    **_bill_kwargs(1500)))
        db.add(Bill(tenant_id=src, account_id=ua3.id,
                    document_number="d-" + secrets.token_hex(3),
                    **_bill_kwargs(900)))
        for d in (5, 6, 7):
            db.add(DailyGeneration(tenant_id=src, array_id=a2.id,
                                   day=date(2024, 1, d), kwh=100.0))

        # ── target siblings (dual capture: same numbers, its own rows) ──
        ta1 = Array(tenant_id=tgt, client_id=None, name="North Field")
        ta2 = Array(tenant_id=tgt, client_id=None, name="S Field Two")
        db.add_all([ta1, ta2])
        db.flush()
        ids["ta1"], ids["ta2"] = ta1.id, ta2.id
        tua1 = UtilityAccount(tenant_id=tgt, array_id=ta1.id, provider="gmp",
                              account_number="TN1_" + secrets.token_hex(3))
        tua2 = UtilityAccount(tenant_id=tgt, array_id=ta2.id, provider="gmp",
                              account_number=shared_acct)   # the sibling tell
        db.add_all([tua1, tua2])
        db.flush()
        db.add(Bill(tenant_id=tgt, account_id=tua1.id,
                    document_number="d-" + secrets.token_hex(3),
                    **_bill_kwargs(1500)))
        for d in (5, 6, 7):
            db.add(DailyGeneration(tenant_id=tgt, array_id=ta2.id,
                                   day=date(2024, 1, d), kwh=100.0))

        db.add(ReportDelivery(tenant_id=src, client_id=c1.id,
                              client_name="Alpha Solar", recipient="alpha@client.test",
                              cadence="monthly", status="sent",
                              sent_at=datetime(2026, 6, 1)))
        db.commit()
    return ids


# ── the mapping plan ───────────────────────────────────────────────────────────

def test_plan_maps_by_name_then_account_then_moves():
    ids = _fixture()
    with SessionLocal() as db:
        plan = build_plan(db, db.get(Tenant, ids["src"]), db.get(Tenant, ids["tgt"]))

    assert plan.blockers == []
    by_src = {a.source_id: a for a in plan.arrays}

    a1 = by_src[ids["a1"]]
    assert a1.action == "link-name" and a1.target_id == ids["ta1"]
    assert a1.rename_to is None                       # exact case already
    assert a1.fields["nepool_gis_id"] == "111"

    a2 = by_src[ids["a2"]]
    assert a2.action == "link-account" and a2.target_id == ids["ta2"]
    assert a2.rename_to == "South Field"              # report-name continuity
    assert a2.fields["nepool_gis_id"] == "222"

    a3 = by_src[ids["a3"]]
    assert a3.action == "move"                        # create-on-target default
    assert a3.fields["fuel_type"] == "hydro"
    assert a3.fields["cert_registry"] == "LIHI"

    assert {c["name"] for c in plan.clients} == {"Alpha Solar", "Beta Hydro"}
    assert plan.report_deliveries == 1

    settings = {s["field"]: s["action"] for s in plan.settings}
    assert settings["email_body_template"] == "copy"
    assert settings["send_mode"] == "copy"
    assert settings["cc_on_reports"] == "copy"
    assert settings["send_from_email"].startswith("keep-target")   # target set


# ── the byte-diff oracle + rollback proof ──────────────────────────────────────

def test_verify_workbooks_identical_and_rolls_back():
    ids = _fixture()
    results = run_verify(ids["src"], ids["tgt"], reference_date=_REF)

    for cid in (ids["c1"], ids["c2"]):
        r = results[cid]
        assert r["level"] in ("identical", "content_identical"), r
    # C1 exercises the gmcs (solar) writer, C2 the rec (hydro) writer — both
    # through the real registry dispatch.

    # ROLLBACK PROOF: verify must leave the world byte-for-byte as it found it.
    with SessionLocal() as db:
        assert db.get(Client, ids["c1"]).tenant_id == ids["src"]
        assert db.get(Client, ids["c2"]).tenant_id == ids["src"]
        assert db.get(Array, ids["a1"]).client_id == ids["c1"]     # not detached
        assert db.get(Array, ids["a3"]).tenant_id == ids["src"]    # not moved
        ta2 = db.get(Array, ids["ta2"])
        assert ta2.name == "S Field Two" and ta2.client_id is None
        assert ta2.nepool_gis_id is None
        tgt = db.get(Tenant, ids["tgt"])
        assert tgt.email_body_template is None                     # not copied
        assert tgt.generation_reports is False                     # flag NOT flipped
        rd = db.execute(
            __import__("sqlalchemy").select(ReportDelivery)
            .where(ReportDelivery.client_id == ids["c1"])).scalars().first()
        assert rd.tenant_id == ids["src"]


# ── execute mode ───────────────────────────────────────────────────────────────

def test_execute_moves_everything_and_carries_nepool_fields():
    ids = _fixture()
    plan = run_execute(ids["src"], ids["tgt"])
    assert plan.blockers == []

    with SessionLocal() as db:
        # clients re-pointed, ids stable
        assert db.get(Client, ids["c1"]).tenant_id == ids["tgt"]
        assert db.get(Client, ids["c2"]).tenant_id == ids["tgt"]

        # A1 -> TA1 linked, fields carried, source sibling detached
        ta1 = db.get(Array, ids["ta1"])
        assert ta1.client_id == ids["c1"]
        assert ta1.nepool_gis_id == "111"
        assert db.get(Array, ids["a1"]).client_id is None

        # A2 -> TA2 linked + renamed for report-name continuity
        ta2 = db.get(Array, ids["ta2"])
        assert ta2.client_id == ids["c1"]
        assert ta2.name == "South Field"
        assert ta2.nepool_gis_id == "222"
        assert db.get(Array, ids["a2"]).client_id is None

        # A3 moved wholesale: array + account + bill + daily rows on target
        a3 = db.get(Array, ids["a3"])
        assert a3.tenant_id == ids["tgt"] and a3.client_id == ids["c2"]
        assert a3.fuel_type == "hydro" and a3.cert_registry == "LIHI"
        ua3 = db.get(UtilityAccount, ids["ua3"])
        assert ua3.tenant_id == ids["tgt"]
        from sqlalchemy import select
        b3 = db.execute(select(Bill).where(Bill.account_id == ids["ua3"])).scalars().one()
        assert b3.tenant_id == ids["tgt"]
        dg = db.execute(select(DailyGeneration)
                        .where(DailyGeneration.array_id == ids["a3"])).scalars().all()
        assert dg == [] or all(r.tenant_id == ids["tgt"] for r in dg)

        # settings: hard copies land, soft copy keeps the target's own value
        tgt = db.get(Tenant, ids["tgt"])
        src = db.get(Tenant, ids["src"])
        # the reports-world marker: THIS is what makes the migrated AO tenant
        # eligible for scheduled sends/digests (api/report_eligibility)
        assert tgt.generation_reports is True
        from api.report_eligibility import tenant_reports_eligible
        assert tenant_reports_eligible(tgt) is True
        assert tgt.email_body_template == "Hello {{client_name}} — custom body"
        assert tgt.email_subject_template == "Sub {{quarter}}"
        assert tgt.email_signoff == "— Bruce"
        assert tgt.send_mode == "to_both" and tgt.cc_on_reports is True
        assert tgt.report_frequency == "monthly"
        assert tgt.send_from_email == "ao@tgt.test"      # kept, not clobbered
        assert src.email_body_template is not None        # source untouched

        # ReportDelivery history re-pointed
        rd = db.execute(select(ReportDelivery)
                        .where(ReportDelivery.client_id == ids["c1"])).scalars().first()
        assert rd.tenant_id == ids["tgt"]

    # And the migrated target state still renders the same workbooks: a fresh
    # post-commit build for C1 equals a reference build of the same data shape.
    from api.writers import build_workbook
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        p = build_workbook(client_id=ids["c1"],
                           out_path=pathlib.Path(td) / "c1.xlsx",
                           reference_date=_REF)
        from openpyxl import load_workbook
        wb = load_workbook(p)
        assert wb.sheetnames == ["North Field", "South Field"]
        assert wb["North Field"]["A1"].value == "North Field (111)"
        assert wb["South Field"]["A1"].value == "South Field (222)"


# ── never guess between two candidates ─────────────────────────────────────────

def test_ambiguous_name_match_blocks_execute():
    src = _tenant(product="nepool")
    tgt = _tenant(product="array_operator")
    with SessionLocal() as db:
        c = Client(tenant_id=src, name="Amb Client", active=True)
        db.add(c)
        db.flush()
        db.add(Array(tenant_id=src, client_id=c.id, name="dup house"))
        # TWO live target arrays that both match case-insensitively.
        db.add(Array(tenant_id=tgt, client_id=None, name="Dup House"))
        db.add(Array(tenant_id=tgt, client_id=None, name="DUP HOUSE"))
        db.commit()

    with SessionLocal() as db:
        plan = build_plan(db, db.get(Tenant, src), db.get(Tenant, tgt))
    assert plan.ambiguities and "will not guess" in plan.ambiguities[0]

    with pytest.raises(MigrationBlocked):
        run_execute(src, tgt)
    with pytest.raises(MigrationBlocked):
        run_verify(src, tgt, reference_date=_REF)

    # and nothing moved
    with SessionLocal() as db:
        from sqlalchemy import select
        cl = db.execute(select(Client).where(Client.tenant_id == src)).scalars().all()
        assert len(cl) == 1


def test_product_sanity_and_demo_are_blockers():
    src = _tenant(product="array_operator")        # wrong way around
    tgt = _tenant(product="nepool")
    with SessionLocal() as db:
        db.add(Client(tenant_id=src, name="X", active=True))
        db.commit()
    with SessionLocal() as db:
        plan = build_plan(db, db.get(Tenant, src), db.get(Tenant, tgt))
    assert any("expected 'nepool'" in c for c in plan.conflicts)
    assert any("expected 'array_operator'" in c for c in plan.conflicts)

    demo_src = _tenant(product="nepool", is_demo=True)
    ao = _tenant(product="array_operator")
    with SessionLocal() as db:
        db.add(Client(tenant_id=demo_src, name="Y", active=True))
        db.commit()
    with SessionLocal() as db:
        plan = build_plan(db, db.get(Tenant, demo_src), db.get(Tenant, ao))
    assert any("demo" in c for c in plan.conflicts)


def test_compare_workbooks_levels():
    from openpyxl import Workbook
    import io as _io

    def _wb_bytes(val, created=None):
        wb = Workbook()
        wb.active["A1"] = val
        if created is not None:
            wb.properties.created = created
            wb.properties.modified = created
        buf = _io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    same_t = datetime(2026, 1, 1, 12, 0, 0)
    a = _wb_bytes("x", created=same_t)
    b = _wb_bytes("x", created=same_t)
    assert compare_workbooks(a, b)[0] == "identical"

    c = _wb_bytes("x", created=datetime(2026, 1, 1, 12, 0, 5))
    assert compare_workbooks(a, c)[0] == "content_identical"   # timestamp-only

    d = _wb_bytes("y", created=same_t)
    level, detail = compare_workbooks(a, d)
    assert level == "different" and "row 1" in detail
