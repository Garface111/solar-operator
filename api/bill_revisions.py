"""Retain corrected utility evidence; a replay cannot reinstate an older revision."""
import hashlib
import json
import math
from datetime import datetime, timezone

FIELDS = ("kwh_generated", "kwh_sent_to_grid", "solar_credit_usd")


def apply_bill_revision(bill, values, *, source, evidence=None):
    clean = {k: float(v) for k, v in values.items() if k in FIELDS and v is not None}
    if any(not math.isfinite(v) or v < 0 for v in clean.values()):
        raise ValueError("Utility billing quantities must be finite and non-negative")
    fingerprint = hashlib.sha256(json.dumps({"source": source, "values": clean,
        "evidence": evidence}, sort_keys=True, default=str).encode()).hexdigest()
    raw = dict(bill.raw_json or {})
    history = list(raw.get("_ao_evidence_revisions", []))
    if any(r["fingerprint"] == fingerprint for r in history):
        return False
    before = {k: getattr(bill, k) for k in FIELDS}
    history.append({"fingerprint": fingerprint, "source": source,
        "received_at": datetime.now(timezone.utc).isoformat(), "before": before,
        "after": dict(before, **clean), "evidence": evidence})
    raw["_ao_evidence_revisions"] = history
    bill.raw_json = raw
    for key, value in clean.items():
        setattr(bill, key, round(value) if key == "kwh_generated" else value)
    return True
