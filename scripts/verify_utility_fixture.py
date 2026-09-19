"""Compare a candidate's output with independently transcribed source evidence.

This is offline fixture verification only; it cannot approve a production utility.
"""
import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

FIELDS = {
    "bill_credit": {"net_meter_credit_cents"},
    "production_kwh": {"generation_kwh"},
    "allocation_statement": {"allocated_kwh", "credit_applied_cents"},
}
SOURCES = {
    "bill_credit": {"utility_statement"},
    "production_kwh": {"utility_meter", "utility_statement"},
    "allocation_statement": {"allocation_statement"},
}


def _records(document, source_hash, label, today):
    if document.get("source_sha256") != source_hash:
        raise ValueError(f"{label}: original source hash does not match")
    records = document.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= 5000:
        raise ValueError(f"{label}: require 1 to 5000 records")
    out = {}
    for index, row in enumerate(records, 1):
        prefix = f"{label} record {index}"
        if not isinstance(row, dict):
            raise ValueError(f"{prefix}: expected an object")
        for field in ["provider_code", "account_reference", "period_start", "period_end"]:
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"{prefix}: missing {field}")
        start, end = date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"])
        if start.isoformat() != row["period_start"] or end.isoformat() != row["period_end"]:
            raise ValueError(f"{prefix}: dates must use YYYY-MM-DD")
        if start > end or end >= today:
            raise ValueError(f"{prefix}: require a closed, correctly ordered period")
        basis = row.get("billing_basis")
        if basis not in FIELDS or row.get("source_kind") not in SOURCES[basis]:
            raise ValueError(f"{prefix}: source does not support this billing basis")
        if row.get("currency") != "USD" or row.get("estimated") is not False:
            raise ValueError(f"{prefix}: require USD and explicitly non-estimated evidence")
        values = row.get("values")
        if not isinstance(values, dict) or set(values) != FIELDS[basis]:
            raise ValueError(f"{prefix}: expected explicit invoice input fields")
        normalized = {}
        for field, raw in values.items():
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                raise ValueError(f"{prefix}: invalid numeric value")
            try:
                number = Decimal(str(raw))
            except InvalidOperation as exc:
                raise ValueError(f"{prefix}: invalid numeric value") from exc
            if not number.is_finite():
                raise ValueError(f"{prefix}: nonfinite value")
            if field.endswith("_cents") and (not isinstance(raw, int) or number != number.to_integral_value()):
                raise ValueError(f"{prefix}: money must be integer cents")
            if field.endswith("_kwh") and number < 0:
                raise ValueError(f"{prefix}: negative kWh")
            normalized[field] = number
        key = (row["provider_code"], row["account_reference"], row["period_start"], row["period_end"], basis)
        if key in out:
            raise ValueError(f"{prefix}: duplicate account/period/basis")
        out[key] = (row["source_kind"], normalized)
    return out


def verify(expected, actual, source_bytes, *, today=None):
    today = today or date.today()
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        raise ValueError("Expected and actual documents must be objects")
    digest = hashlib.sha256(source_bytes).hexdigest()
    want = _records(expected, digest, "expected", today)
    got = _records(actual, digest, "actual", today)
    if set(want) != set(got):
        raise ValueError("Missing, additional or mismatched accounts/periods/billing bases")
    if want != got:
        raise ValueError("Captured amounts, units or source semantics differ from independent expectations")
    return {"ok": True, "records_matched": len(want), "source_sha256": digest,
            "invoice_ready": False, "qualification": "offline_fixture_only"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--expected", type=Path, required=True)
    p.add_argument("--actual", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    a = p.parse_args()
    try:
        result = verify(json.loads(a.expected.read_text()), json.loads(a.actual.read_text()), a.source.read_bytes())
    except (ValueError, OSError) as exc:
        p.exit(2, str(exc) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
