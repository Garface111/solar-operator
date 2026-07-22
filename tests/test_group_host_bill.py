"""Group-host bill anatomy + hard offtaker excess pool rules (Colleen / GMP)."""
from types import SimpleNamespace

from api.billing.group_host_bill import (
    bill_anatomy,
    group_excess_pool,
    help_copy_gmp_group_host,
)


def _bill(**kw):
    return SimpleNamespace(
        kwh_generated=kw.get("kwh_generated"),
        kwh_consumed=kw.get("kwh_consumed"),
        kwh_sent_to_grid=kw.get("kwh_sent_to_grid"),
        total_cost=kw.get("total_cost"),
        raw_json=kw.get("raw_json"),
        period_start=kw.get("period_start"),
        period_end=kw.get("period_end"),
    )


def test_may_style_group_excess_uses_sent_not_gross():
    """27,000 gen − 175 consumed = 26,825 shared — never bill offtakers on 27,180/27,000 alone."""
    b = _bill(
        kwh_generated=27000,
        kwh_consumed=175,
        kwh_sent_to_grid=26825,
        total_cost=31.81,
    )
    pool, src, warn = group_excess_pool(b)
    assert pool == 26825
    assert src == "kwh_sent_to_grid"
    assert warn is None
    anat = bill_anatomy(b)
    assert anat["group_excess_shared_kwh"] == 26825
    assert anat["generated_kwh"] == 27000
    assert anat["host_consumed_kwh"] == 175
    assert anat["fixed_charges_usd"] == 31.81
    assert anat["fallback_to_gross"] is False
    # gen − consumed matches shared → integrity ok
    assert anat["integrity_ok"] is True


def test_never_inflate_to_gross_when_shared_smaller():
    b = _bill(kwh_generated=27000, kwh_consumed=175, kwh_sent_to_grid=26825)
    pool, src, _ = group_excess_pool(b)
    assert pool < float(b.kwh_generated)
    assert src == "kwh_sent_to_grid"


def test_derive_gen_minus_consumed_when_sent_missing():
    b = _bill(kwh_generated=27000, kwh_consumed=175, kwh_sent_to_grid=None)
    pool, src, warn = group_excess_pool(b)
    assert pool == 26825
    assert src == "gen_minus_consumed"
    assert warn is None


def test_gross_fallback_warns():
    b = _bill(kwh_generated=28080, kwh_consumed=None, kwh_sent_to_grid=None)
    pool, src, warn = group_excess_pool(b)
    assert pool == 28080
    assert src == "gross_fallback"
    assert warn and "No Group Excess Shared" in warn
    anat = bill_anatomy(b)
    assert anat["fallback_to_gross"] is True
    assert anat["warnings"]


def test_integrity_warn_when_gen_minus_consumed_diverges():
    b = _bill(kwh_generated=27000, kwh_consumed=175, kwh_sent_to_grid=25000)
    anat = bill_anatomy(b)
    assert anat["integrity_ok"] is False
    assert any("differs by more than" in w for w in anat["warnings"])


def test_zero_host_load_shared_equals_gen():
    b = _bill(kwh_generated=28080, kwh_consumed=0, kwh_sent_to_grid=28080)
    pool, src, _ = group_excess_pool(b)
    assert pool == 28080
    assert src == "kwh_sent_to_grid"
    anat = bill_anatomy(b)
    assert anat["integrity_ok"] is True


def test_help_copy_present():
    h = help_copy_gmp_group_host()
    assert "Group Excess Shared" in h["summary"]
    assert h["example"]["group_excess_shared_kwh"] == 26825
