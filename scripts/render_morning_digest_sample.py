"""Render a realistic morning-digest sample to HTML (no DB, no email)."""
import sys
from types import SimpleNamespace

sys.path.insert(0, "/root/solar-operator")
from api.jobs import morning_fleet_digest as digest

tenant = SimpleNamespace(
    id="ten_demo", name="Maple Ridge Energy",
    company_name="Maple Ridge Energy", operator_name="Ford Genereaux",
    contact_email="owner@example.test", product="array_operator",
)

tree = {
    "generated_at": "2026-06-17T12:00:00Z",
    "columns": [
        {
            "array_id": 1, "array_name": "South Pasture", "inverter_count": 4,
            "alert": {"level": "ok", "count": 0, "status": "ok", "headline": "All clear"},
            "inverters": [{"inverter_id": i, "name": f"SP-{i}", "status": "ok"} for i in range(1, 5)],
            "daily": [{"date": "2026-06-14", "kwh": 88.2}, {"date": "2026-06-15", "kwh": 91.7},
                      {"date": "2026-06-16", "kwh": 94.3}],
            "is_daylight": True,
        },
        {
            "array_id": 2, "array_name": "Barn Roof", "inverter_count": 2,
            "alert": {"level": "warn", "count": 1, "status": "underperforming",
                      "headline": "A money leak caught early"},
            "inverters": [
                {"inverter_id": 11, "name": "BR-1", "status": "ok"},
                {"inverter_id": 12, "name": "BR-2", "status": "underperforming", "peer_index": 0.42},
            ],
            "daily": [{"date": "2026-06-15", "kwh": 31.0}, {"date": "2026-06-16", "kwh": 28.4}],
            "is_daylight": True,
        },
        {
            "array_id": 3, "array_name": "Pump House", "inverter_count": 1,
            "alert": {"level": "critical", "count": 1, "status": "fault",
                      "headline": "Inverter fault — service drafted"},
            "inverters": [{"inverter_id": 21, "name": "PH-1", "status": "fault"}],
            "daily": [{"date": "2026-06-16", "kwh": 4.2}],
            "is_daylight": True,
        },
        {
            "array_id": 4, "array_name": "New Carport", "inverter_count": 0,
            "alert": {"level": "ok", "count": 0, "status": "ok", "headline": "All clear"},
            "inverters": [], "daily": [], "is_daylight": True,
        },
    ],
    "summary": {"arrays_total": 4, "inverters_total": 7, "attention": 2, "is_daylight": True},
}

html = digest.build_digest_html(tenant, tree)
out = "/root/vt-solar-intel/morning-digest-sample.html"
with open(out, "w") as f:
    f.write(html)
import os
print(f"Wrote {out} ({os.path.getsize(out)} bytes)")
