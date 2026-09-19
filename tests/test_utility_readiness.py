import copy
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from datetime import date
import pytest

from api.utility_readiness import assess_utility, analyze_roster, IntakeError
from scripts.norwich_utility_readiness import scaffold, report_markdown, COLUMNS
from scripts.verify_utility_fixture import verify


def group(**kw):
    return {"group_id": "gmp-monthly", "utility_name": "GMP", "provider_code": "gmp",
            "state": "VT", "portal_url": "https://greenmountainpower.com",
            "source_role": "offtaker_credit", "billing_basis": "bill_credit",
            "cadence": "monthly", "offtaker_count": 10, **kw}


def test_collector_does_not_claim_billing_or_live_qualification():
    for code in ["gmp", "vec", "cmp", "eversource", "eversource_ma", "unknown"]:
        r = assess_utility(code, billing_basis="bill_credit")
        assert not r["invoice_ready"]
        if code in {"cmp", "eversource", "eversource_ma"}:
            assert r["integration_route"] == "add_billing_evidence_path"
            assert "bill_json" not in r["capture_outputs"]


def test_known_and_dynamic_smarthub_require_per_utility_qualification():
    for code,host in [("vec", "vermontelectric.smarthub.coop"), ("sh_newcoop", "newcoop.smarthub.coop")]:
        r = assess_utility(code, portal_url=host, billing_basis="production_kwh")
        assert r["family"] == "smarthub"
        assert r["integration_route"] == "qualify_existing_family"
        assert not r["invoice_ready"]
    assert assess_utility("vec", billing_basis="production_kwh", cadence="quarterly")["integration_route"] == "add_billing_evidence_path"


def test_catalog_identity_mismatch_is_not_silently_reassigned():
    r = assess_utility("vec", portal_url="washingtonelectric.smarthub.coop", billing_basis="production_kwh")
    assert r["integration_route"] == "verify_identity"
    assert "portal_identity_mismatch" in r["identity_issues"]
    assert assess_utility("gmp",state="ME",billing_basis="bill_credit")["integration_route"] == "verify_identity"
    assert assess_utility(utility_name="Pomfret Power-ish",billing_basis="bill_credit")["family"] == "unknown"


@pytest.mark.parametrize("url", ["https://user:secret@example.com", "https://example.com/?token=secret", "https://example.com/#secret", "http://example.com", "https://127.0.0.1", "https://example.com:444", "https://localhost"])
def test_secret_bearing_or_nonportal_addresses_rejected(url):
    with pytest.raises(IntakeError): assess_utility(portal_url=url)


def test_spoofed_smarthub_suffix_is_unknown():
    assert assess_utility(portal_url="https://x.smarthub.coop.evil.example")["family"] == "unknown"


def test_300_roster_groups_are_counted_prioritized_and_never_approved():
    rows = [group(group_id=f"source-{i}",offtaker_count=1) for i in range(300)]
    r = analyze_roster(rows)
    assert (r["group_count"],r["offtaker_count"],r["invoice_ready_count"]) == (300,300,0)
    assert all(g["qualification"] == "not_assessed_on_real_accounts" for g in r["groups"])
    r = analyze_roster([group(),group(group_id="other",offtaker_count=290,provider_code="unknown",portal_url="")])
    assert r["groups"][0]["group_id"] == "other" and r["offtaker_count"] == 300


def test_duplicate_groups_and_unknown_counts_fail_instead_of_underreporting():
    with pytest.raises(IntakeError): analyze_roster([group(),group()])
    for count in [0,-1,1.5,"unknown",True,"1,000",""]:
        with pytest.raises(IntakeError): analyze_roster([group(offtaker_count=count)])
    assert analyze_roster([group(source_role="unknown")])["groups"][0]["integration_route"] == "confirm_billing_source"


def test_markdown_escapes_untrusted_utility_labels():
    s = report_markdown(analyze_roster([group(utility_name="<img src=x>|\nInjected")]))
    assert "<img" not in s and "&lt;img" in s and "&#124;" in s


def test_scaffold_is_inert_and_does_not_overwrite(tmp_path):
    p = scaffold("new_utility",tmp_path)
    assert json.loads((p/"qualification.json").read_text())["status"] == "candidate"
    assert "NotImplementedError" in (p/"normalize.py").read_text()
    with pytest.raises(FileExistsError): scaffold("new_utility",tmp_path)
    with pytest.raises(IntakeError): scaffold("../../bad",tmp_path)


def test_cli_reports_unsupported_roster_without_network(tmp_path):
    source=tmp_path/"intake.csv"
    with source.open("w",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=COLUMNS);writer.writeheader();writer.writerow(group())
    result=subprocess.run([sys.executable,"-m","scripts.norwich_utility_readiness","--roster",str(source),"--output",str(tmp_path/"out")],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads((tmp_path/"out"/"utility-readiness.json").read_text())["invoice_ready_count"]==0


def test_readiness_endpoint_requires_auth_and_has_no_capture_side_effects(client,monkeypatch):
    from tests.test_offtaker_upload import _make_tenant
    from api import bill_adapter_autopilot as a
    def forbidden(*args,**kwargs): raise AssertionError("Planning must not start capture")
    monkeypatch.setattr(a,"on_credential_saved",forbidden)
    monkeypatch.setattr(a,"synthesize_bill_extractor",forbidden)
    path="/v1/bill-autopilot/readiness"
    assert client.post(path,json={"groups":[group()]}).status_code==401
    _,auth=_make_tenant()
    r=client.post(path,headers={"Authorization":auth},json={"groups":[group()]})
    assert r.status_code==200 and r.json()["invoice_ready_count"]==0
    assert client.post(path,headers={"Authorization":auth},json={"groups":[group(),group()]}).status_code==422


def document():
    source=b"Synthetic independently reviewed statement: account A June 2026 credit 123.45 USD"
    row={"provider_code":"acme", "account_reference":"A", "period_start":"2026-06-01", "period_end":"2026-06-30", "billing_basis":"bill_credit", "source_kind":"utility_statement", "currency":"USD", "estimated":False,"values":{"net_meter_credit_cents":12345}}
    return source,{"source_sha256":hashlib.sha256(source).hexdigest(),"records":[row]}


def test_fixture_verifier_matches_source_identity_and_cents_but_never_certifies_live():
    source,d=document()
    result=verify(d,copy.deepcopy(d),source,today=date(2026,7,1))
    assert result["ok"] and not result["invoice_ready"]
    with pytest.raises(ValueError):verify(d,d,b"changed source")


@pytest.mark.parametrize("change", ["wrong_account","missing_period","incomplete_cycle","duplicate","wrong_cents","nonfinite","estimated","wrong_semantics","bool_money","fractional_money","additional_account"])
def test_fixture_verifier_rejects_bad_evidence(change):
    source,d=document();actual=copy.deepcopy(d);r=actual["records"][0]
    if change=="wrong_account":r["account_reference"]="B"
    if change=="missing_period":r.pop("period_start")
    if change=="incomplete_cycle":r["period_end"]="2026-06-15"
    if change=="duplicate":actual["records"].append(copy.deepcopy(r))
    if change=="wrong_cents":r["values"]["net_meter_credit_cents"]=12346
    if change=="nonfinite":r["values"]["net_meter_credit_cents"]="NaN"
    if change=="estimated":r["estimated"]=True
    if change=="wrong_semantics":r["source_kind"]="inverter_telemetry"
    if change=="bool_money":r["values"]["net_meter_credit_cents"]=True
    if change=="fractional_money":r["values"]["net_meter_credit_cents"]=12345.0
    if change=="additional_account":
        extra=copy.deepcopy(r);extra["account_reference"]="B";actual["records"].append(extra)
    with pytest.raises(ValueError):verify(d,actual,source,today=date(2026,7,1))


def test_known_label_cannot_contradict_explicit_provider_and_cadence_is_honest():
    r=assess_utility("gmp",utility_name="Central Maine Power (CMP)",state="VT",billing_basis="bill_credit")
    assert "utility_name_identity_mismatch" in r["identity_issues"]
    assert r["integration_route"]=="verify_identity"
    assert assess_utility("gmp",billing_basis="bill_credit",cadence="annual")["integration_route"]=="add_billing_evidence_path"
    assert assess_utility("gmp",billing_basis="bill_credit",cadence="unknown")["integration_route"]=="confirm_cadence"


@pytest.mark.parametrize("shape", ["duplicate_header","extra_cell","missing_cell"])
def test_csv_cannot_hide_a_count_in_ambiguous_columns(tmp_path,shape):
    source=tmp_path/"bad.csv"
    headers=COLUMNS+["offtaker_count"] if shape=="duplicate_header" else COLUMNS
    row=[group()[k] for k in COLUMNS]
    if shape in {"duplicate_header","extra_cell"}:row.append("1")
    else:row.pop()
    with source.open("w",newline="") as f:
        writer=csv.writer(f);writer.writerow(headers);writer.writerow(row)
    result=subprocess.run([sys.executable,"-m","scripts.norwich_utility_readiness","--roster",str(source),"--output",str(tmp_path/"out")],capture_output=True,text=True)
    assert result.returncode==2
    assert not (tmp_path/"out"/"utility-readiness.json").exists()


def test_compact_iso_dates_cannot_hide_a_duplicate_cycle():
    source,d=document()
    alternate=copy.deepcopy(d["records"][0]);alternate["period_start"]="20260601";alternate["period_end"]="20260630"
    d["records"].append(alternate)
    with pytest.raises(ValueError,match="YYYY-MM-DD"):verify(d,d,source,today=date(2026,7,1))
