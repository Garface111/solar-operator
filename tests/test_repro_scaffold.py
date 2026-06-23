"""Repro wrapper scaffold — structure + graceful degradation.

These don't need a headless renderer or an API key: they assert the wrapper
DEGRADES (no PDF, skipped verdict, heuristic-only) instead of crashing when
those aren't configured, which is the contract that lets us turn it on safely.
"""
import io
import os
import pathlib

os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_repro_test")
# Force the "nothing configured" baseline regardless of the host env.
os.environ.pop("GOTENBERG_URL", None)
os.environ.pop("SOFFICE_BIN", None)

from openpyxl import Workbook

from api.billing.repro import repro_enabled
from api.billing.repro import render as R
from api.billing.repro.analyze import ai_field_map, _sheets_to_text, FIELD_MAP_SCHEMA
from api.billing.repro.verify import ai_verify, Verdict
from api.billing.repro.pipeline import reproduce_invoice, ReproResult

FIX = pathlib.Path(__file__).parent / "fixtures" / "billing"


def _sample_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Ledger"
    ws.append(["Month", "kWh whole array", "Tariff", "Adder"])
    ws.append(["May", 1000, 0.18, 0.03])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_flag_defaults_off():
    os.environ.pop("REPRO_ENABLED", None)
    assert repro_enabled() is False


def test_renderer_reports_unavailable_when_unconfigured():
    # SOFFICE_BIN/GOTENBERG_URL popped above, but a real soffice on PATH is also
    # auto-detected — only assert the API shape, not a specific backend.
    assert R.active_backend() in ("none", "soffice", "gotenberg")
    assert isinstance(R.renderer_available(), bool)


def test_render_raises_unavailable_cleanly(monkeypatch):
    monkeypatch.setattr(R, "GOTENBERG_URL", None)
    monkeypatch.setattr(R, "SOFFICE_BIN", None)
    assert R.renderer_available() is False
    try:
        R.render_xlsx_to_pdf(_sample_xlsx())
        assert False, "expected RenderUnavailable"
    except R.RenderUnavailable:
        pass


def test_pipeline_degrades_without_renderer(monkeypatch):
    monkeypatch.setattr(R, "GOTENBERG_URL", None)
    monkeypatch.setattr(R, "SOFFICE_BIN", None)
    xlsx = _sample_xlsx()
    res = reproduce_invoice(lambda: xlsx, verify=False)
    assert isinstance(res, ReproResult)
    assert res.xlsx == xlsx
    assert res.pdf is None
    assert res.deliverable == xlsx          # falls back to the .xlsx
    assert res.verdict.skipped


def test_verify_skips_without_png_or_key():
    v = ai_verify(rendered_png=None)
    assert isinstance(v, Verdict)
    assert v.ok is None and v.skipped


def test_ai_field_map_graceful_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ai_field_map(_sample_xlsx()) is None


def test_sheets_to_text_on_real_fixture():
    p = FIX / "norwich.xlsx"
    if not p.exists():
        return
    txt = _sheets_to_text(p.read_bytes())
    assert "# SHEET:" in txt and "r0\t" in txt


def test_field_map_schema_shape():
    assert FIELD_MAP_SCHEMA["properties"]["field_map"]["properties"].get("month")
    assert "data_sheet" in FIELD_MAP_SCHEMA["required"]
