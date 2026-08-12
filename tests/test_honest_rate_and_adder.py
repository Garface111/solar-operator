"""HONEST RATE + INCENTIVE ADDER (Ford, 2026-08-12).

Born from a real mis-bill. HCT Sun Enterprises' offtakers (Norwich Fire District,
Town of Fairlee) are contracted at 0.18398 tariff + 0.04 incentive = 0.22398/kWh.
Every rate override was blank, so pricing fell through to the bound GMP bill's own
credit rate — 0.23298 — and every invoice ran +4.018% high for months. Nothing
warned, because an INFERRED rate was indistinguishable from a CONFIRMED one, and
there was nowhere to enter "tariff + adder" in the first place.

Two guarantees are locked down here:

  1. tariff + adder is expressible, and the adder NEVER stacks on a rate we
     inferred from a utility bill (that credit rate is already all-in — adding to
     it would double-count). Adders also expire, because HCT's runs 2019-2029.

  2. A rate no human entered is marked unconfirmed and warns with the exact
     figure and its origin. It still drafts and can still be sent by hand — it
     just can't go out UNATTENDED (scheduler holds it).
"""
import os
os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_honest_rate_test")

import secrets as _secrets
from datetime import date, datetime

from api.db import SessionLocal
from api.models import Tenant, Array, UtilityAccount, Bill, BillingReportSubscription
from api.billing import delivery
from api.billing.delivery import resolve_discount_pricing
from api.billing.matcher import compute_invoice


# Colleen's June 2026 ground truth, straight off her spreadsheets.
CONTRACT_TARIFF = 0.18398
CONTRACT_ADDER = 0.04
BILL_CREDIT_RATE = 0.23298          # what GMP's bill actually credits
PRICE_FACTOR = 0.9                  # 10% discount
NFD_KWH = 4608.0                    # Norwich FD's June share
NFD_CORRECT_TOTAL = 928.889856      # 4608 × 0.22398 × 0.9
NFD_WRONG_TOTAL = 966.21            # what the inferred rate produced


def _seed(*, bill_credit_rate=BILL_CREDIT_RATE, excess=28800.0):
    tid = "ten_honest_" + _secrets.token_hex(3)
    with SessionLocal() as db:
        db.add(Tenant(id=tid, tenant_key=_secrets.token_hex(8), name="HCT-like Op",
                      contact_email=f"{tid}@e.com", active=True,
                      product="array_operator"))
        db.flush()
        arr = Array(tenant_id=tid, name="Norwich Union Village", region="VT")
        db.add(arr); db.flush()
        acct = UtilityAccount(tenant_id=tid, array_id=arr.id, provider="gmp",
                              account_number="GMP-" + _secrets.token_hex(2),
                              nickname="Norwich Union Village")
        db.add(acct); db.flush()
        db.add(Bill(tenant_id=tid, account_id=acct.id,
                    period_start=datetime(2026, 6, 19),
                    period_end=datetime(2026, 7, 20),
                    kwh_generated=int(excess),
                    kwh_sent_to_grid=excess,
                    solar_credit_usd=round(excess * bill_credit_rate, 2)))
        db.commit()
        return tid, arr.id, acct.id


def _sub(tid, aid, acct_id, **kw):
    kw.setdefault("allocation_pct", 0.16)
    return BillingReportSubscription(
        tenant_id=tid, customer_name="Norwich Fire District",
        utility_account_id=acct_id, array_id=aid,
        billing_model="percent_of_array", discount_pct=0.1, **kw)


# ── 1. the math itself ────────────────────────────────────────────────────────

def test_compute_invoice_reproduces_colleens_numbers():
    """The engine's tariff+adder math IS Colleen's sheet. Proven against her own
    June figures — this is the number the customer should have been billed."""
    inv = compute_invoice(NFD_KWH, CONTRACT_TARIFF, CONTRACT_ADDER,
                          PRICE_FACTOR, "percent_of_array", None)
    assert abs(inv["net_value"] - 847.78) < 0.01          # 4608 × 0.18398
    assert abs(inv["incentive_value"] - 184.32) < 0.01    # 4608 × 0.04
    assert abs(inv["solar_value"] - 1032.10) < 0.01       # 4608 × 0.22398
    assert abs(inv["billed_value"] - NFD_CORRECT_TOTAL) < 0.01
    # And it is NOT what the inferred bill rate produced.
    assert abs(inv["billed_value"] - NFD_WRONG_TOTAL) > 30


# ── 2. adder resolution + expiry ──────────────────────────────────────────────

class _Bare:
    """Minimal sub-like object for pure pricing resolution."""
    net_rate_per_kwh = None
    net_rate_adder_per_kwh = None
    net_rate_adder_until = None
    discount_pct = None
    rate_per_kwh = None
    array_id = None


def test_adder_resolves_from_offtaker_then_tenant():
    tid, _, _ = _seed()
    s = _Bare(); s.tenant_id = tid
    s.net_rate_per_kwh = CONTRACT_TARIFF
    s.net_rate_adder_per_kwh = CONTRACT_ADDER
    p = resolve_discount_pricing(s, period_end=date(2026, 7, 20))
    assert p["net_source"] == "customer"
    assert abs(p["adder"] - CONTRACT_ADDER) < 1e-9
    assert p["adder_source"] == "customer"

    # Blank on the offtaker → the fleet default carries it.
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        t.default_net_rate_per_kwh = CONTRACT_TARIFF
        t.default_net_rate_adder_per_kwh = CONTRACT_ADDER
        db.commit()
    s2 = _Bare(); s2.tenant_id = tid
    p2 = resolve_discount_pricing(s2, period_end=date(2026, 7, 20))
    assert p2["net_source"] == "global"
    assert abs(p2["adder"] - CONTRACT_ADDER) < 1e-9
    assert p2["adder_source"] == "global"


def test_expired_adder_stops_applying():
    """HCT's adder is '10 years only - 2019-2029'. After it lapses the adder must
    drop to zero rather than quietly keep billing 4 cents."""
    tid, _, _ = _seed()
    s = _Bare(); s.tenant_id = tid
    s.net_rate_per_kwh = CONTRACT_TARIFF
    s.net_rate_adder_per_kwh = CONTRACT_ADDER
    s.net_rate_adder_until = date(2029, 12, 31)

    inside = resolve_discount_pricing(s, period_end=date(2029, 6, 30))
    assert abs(inside["adder"] - CONTRACT_ADDER) < 1e-9

    after = resolve_discount_pricing(s, period_end=date(2030, 1, 31))
    assert after["adder"] == 0.0
    assert after["adder_source"] == "expired"
    assert "ended" in (after["adder_note"] or "")


# ── 3. the honest-rate guard ──────────────────────────────────────────────────

def test_blank_rate_is_flagged_unconfirmed_and_warns_with_the_number():
    """The HCT case exactly: nothing entered anywhere → we still price off the
    bill (a reasonable default) but mark it unconfirmed and say so out loud."""
    tid, aid, acct_id = _seed()
    m = delivery.build_manual_match(_sub(tid, aid, acct_id))
    ci = m.computed_invoice
    assert ci["net_rate_source"] == "gmp_bill_credit"
    assert ci["rate_is_operator_entered"] is False
    # The warning must carry the actual figure — a vague nudge is what let this
    # run for months.
    warned = " ".join(m.warnings)
    assert "Unconfirmed rate" in warned
    assert "0.23298" in warned
    # An inferred rate never picks up an adder, even if one is configured.
    assert ci["adder_per_kwh"] == 0.0


def test_adder_never_stacks_on_an_inferred_bill_rate():
    """Guard against the double-count: bill credit rate is already all-in."""
    tid, aid, acct_id = _seed()
    sub = _sub(tid, aid, acct_id, net_rate_adder_per_kwh=CONTRACT_ADDER)
    ci = delivery.build_manual_match(sub).computed_invoice
    assert ci["rate_is_operator_entered"] is False
    assert ci["adder_per_kwh"] == 0.0
    # Priced purely at the bill rate — the adder contributed nothing.
    assert abs(ci["net_rate_per_kwh"] - BILL_CREDIT_RATE) < 1e-6


def test_entering_the_rate_confirms_it_and_bills_the_contract():
    """The fix, end to end: enter tariff + adder and the invoice becomes Colleen's
    number, the unconfirmed warning disappears, and the rate reads as operator-set."""
    tid, aid, acct_id = _seed()
    sub = _sub(tid, aid, acct_id,
               net_rate_per_kwh=CONTRACT_TARIFF,
               net_rate_adder_per_kwh=CONTRACT_ADDER)
    m = delivery.build_manual_match(sub)
    ci = m.computed_invoice
    assert ci["rate_is_operator_entered"] is True
    assert ci["net_rate_source"] == "customer"
    assert abs(ci["net_rate_per_kwh"] - CONTRACT_TARIFF) < 1e-6
    assert abs(ci["adder_per_kwh"] - CONTRACT_ADDER) < 1e-6
    # All-in effective rate = (0.18398 + 0.04) × 0.9
    assert abs(ci["effective_rate_per_kwh"] - (0.22398 * 0.9)) < 1e-6
    assert "Unconfirmed rate" not in " ".join(m.warnings)
    # The money: Norwich FD's June invoice is Colleen's 928.89, not 966.21.
    assert abs(ci["amount_owed"] - NFD_CORRECT_TOTAL) < 0.02
    assert abs(ci["amount_owed"] - NFD_WRONG_TOTAL) > 30


def test_master_rate_alone_also_counts_as_confirmed():
    """An operator who sets one fleet rate has stated their price — no warning."""
    tid, aid, acct_id = _seed()
    with SessionLocal() as db:
        t = db.get(Tenant, tid)
        t.default_net_rate_per_kwh = CONTRACT_TARIFF
        t.default_net_rate_adder_per_kwh = CONTRACT_ADDER
        db.commit()
    m = delivery.build_manual_match(_sub(tid, aid, acct_id))
    ci = m.computed_invoice
    assert ci["net_rate_source"] == "global"
    assert ci["rate_is_operator_entered"] is True
    assert abs(ci["adder_per_kwh"] - CONTRACT_ADDER) < 1e-6
    assert "Unconfirmed rate" not in " ".join(m.warnings)


# ── 4. the unattended-send hold ───────────────────────────────────────────────

def test_scheduler_holds_auto_send_on_an_unconfirmed_rate():
    from api.scheduler import _unconfirmed_rate_should_hold
    tid, aid, acct_id = _seed()
    # This is the only test here that PERSISTS a subscription, and it must remove it
    # again: test_billing_delivery.test_match_preview_saves_nothing asserts that NO
    # BillingReportSubscription exists anywhere, so a row left behind fails an
    # unrelated test whenever the two files run in one session.
    sub_id = None
    try:
        with SessionLocal() as db:
            blank = _sub(tid, aid, acct_id)
            db.add(blank); db.commit(); db.refresh(blank)
            sub_id = blank.id
            assert _unconfirmed_rate_should_hold(db, blank) is True

            # Entering the rate releases the hold — no separate acknowledgement.
            blank.net_rate_per_kwh = CONTRACT_TARIFF
            blank.net_rate_adder_per_kwh = CONTRACT_ADDER
            db.commit()
            assert _unconfirmed_rate_should_hold(db, blank) is False
    finally:
        if sub_id is not None:
            with SessionLocal() as db:
                row = db.get(BillingReportSubscription, sub_id)
                if row is not None:
                    db.delete(row); db.commit()
