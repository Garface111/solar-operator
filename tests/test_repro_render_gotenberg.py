"""Gotenberg render path — exercised against a MOCK HTTP endpoint.

The scaffold tests (test_repro_scaffold.py) prove the wrapper DEGRADES when no
renderer is configured. These prove the OTHER half of the contract: when a
Gotenberg service IS configured, the code

  * POSTs the workbook to {GOTENBERG_URL}/forms/libreoffice/convert,
  * as a multipart 'files' field carrying the exact bytes + a .xlsx filename,
  * returns the PDF bytes on a %PDF response,
  * rejects a non-PDF body (fail-closed),
  * and FALLS BACK to a bundled soffice when Gotenberg errors.

No live service and no extra deps: httpx.post is monkeypatched with a fake that
records the request and hands back a canned response, so this runs anywhere the
unit suite does. Live end-to-end verification still needs the Railway Gotenberg
service (see docs/REPRO-PIXEL-RUNBOOK.md) and is out of scope for CI.
"""
import io
import os

os.environ.setdefault("SOLAR_DATA_DIR", "/tmp/ao_repro_test")

import httpx
import pytest
from openpyxl import Workbook

from api.billing.repro import render as R

GOTENBERG = "https://gotenberg.internal:3000"
FAKE_PDF = b"%PDF-1.7\n%mock gotenberg output\n%%EOF"


def _xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Month", "kWh", "Amount Due"])
    ws.append(["May", 1000, 182.34])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _FakeResponse:
    """Minimal stand-in for httpx.Response used by _render_gotenberg."""

    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None)


def _install_capture(monkeypatch, *, content=FAKE_PDF, status=200):
    """Point the render module at a Gotenberg URL and capture the POST call.

    Returns a dict the test can inspect after render() runs.
    """
    monkeypatch.setattr(R, "GOTENBERG_URL", GOTENBERG)
    monkeypatch.setattr(R, "SOFFICE_BIN", None)  # isolate the Gotenberg path
    captured: dict = {}

    def fake_post(url, files=None, timeout=None, **kw):
        captured["url"] = url
        captured["files"] = files
        captured["timeout"] = timeout
        return _FakeResponse(content, status)

    monkeypatch.setattr(httpx, "post", fake_post)
    return captured


def test_gotenberg_posts_correct_payload_and_returns_pdf(monkeypatch):
    cap = _install_capture(monkeypatch)
    xb = _xlsx()

    out = R.render_xlsx_to_pdf(xb)

    # PDF bytes flow straight through.
    assert out == FAKE_PDF
    assert out[:4] == b"%PDF"

    # POSTed to the LibreOffice convert route on the configured service.
    assert cap["url"] == f"{GOTENBERG}/forms/libreoffice/convert"
    # multipart 'files' field: (filename, bytes, content-type).
    field, payload = "files", cap["files"]["files"]
    assert field in cap["files"]
    filename, body, content_type = payload
    assert filename == "invoice.xlsx"          # extension drives Gotenberg's converter
    assert body == xb                           # exact workbook bytes, unmodified
    assert content_type == "application/octet-stream"
    # timeout is passed through from RENDER_TIMEOUT_S.
    assert cap["timeout"] == R.RENDER_TIMEOUT_S


def test_gotenberg_honors_office_filename_extension(monkeypatch):
    cap = _install_capture(monkeypatch)
    R.render_office_to_pdf(b"docxbytes", "template.docx")
    filename, body, _ = cap["files"]["files"]
    assert filename == "template.docx"          # .docx preserved so the right converter runs
    assert body == b"docxbytes"


def test_gotenberg_non_pdf_response_fails_closed(monkeypatch):
    # A misconfigured Gotenberg (or an HTML error page with 200) must NOT be
    # mistaken for a valid render — money gate: fail closed, never ship garbage.
    _install_capture(monkeypatch, content=b"<html>error</html>")
    with pytest.raises(R.RenderError):
        R.render_xlsx_to_pdf(_xlsx())


def test_gotenberg_http_error_fails_closed_without_fallback(monkeypatch):
    # 502 from Gotenberg and no local soffice → RenderError (caller falls back to
    # the standard invoice, never a blank/half render).
    _install_capture(monkeypatch, content=b"", status=502)
    with pytest.raises(R.RenderError):
        R.render_xlsx_to_pdf(_xlsx())


def test_gotenberg_failure_falls_back_to_soffice(monkeypatch):
    # When Gotenberg is unreachable BUT a bundled soffice exists, the render must
    # transparently fall back rather than fail — the railpack libreoffice-calc
    # safety net. Assert the local backend is invoked and its PDF returned.
    monkeypatch.setattr(R, "GOTENBERG_URL", GOTENBERG)
    monkeypatch.setattr(R, "SOFFICE_BIN", "/usr/bin/soffice")  # pretend it's installed

    def boom(*a, **k):
        raise httpx.ConnectError("gotenberg down")

    monkeypatch.setattr(httpx, "post", boom)

    called = {}

    def fake_soffice(file_bytes, filename="invoice.xlsx"):
        called["bytes"] = file_bytes
        called["filename"] = filename
        return b"%PDF-soffice-fallback"

    monkeypatch.setattr(R, "_render_soffice", fake_soffice)

    xb = _xlsx()
    out = R.render_office_to_pdf(xb, "invoice.xlsx")
    assert out == b"%PDF-soffice-fallback"
    assert called["bytes"] == xb
    assert called["filename"] == "invoice.xlsx"


def test_pipeline_uses_gotenberg_render(monkeypatch):
    # End-to-end through the pipeline: with Gotenberg configured, reproduce_invoice
    # renders a PDF and the numeric guard passes when the expected amount is on it.
    from api.billing.repro.pipeline import reproduce_invoice

    monkeypatch.setattr(R, "GOTENBERG_URL", GOTENBERG)
    monkeypatch.setattr(R, "SOFFICE_BIN", None)

    # A PDF whose extracted text carries the expected amount so amount_present passes.
    monkeypatch.setattr(
        R, "render_xlsx_to_pdf", lambda xb: FAKE_PDF)
    monkeypatch.setattr(
        R, "render_pdf_first_page_png", lambda pdf, dpi=120: None)

    from api.billing.repro import verify as V
    monkeypatch.setattr(V, "extract_pdf_numbers", lambda pdf: [182.34])
    # pipeline imports amount_present into its own namespace at module load;
    # patch the source so both see the stub.
    import api.billing.repro.pipeline as P
    monkeypatch.setattr(P, "amount_present", lambda pdf, expected: True)

    res = reproduce_invoice(lambda _ov=None: _xlsx(),
                            expected_amount=182.34, verify=False)
    assert res.pdf == FAKE_PDF
    assert res.backend == "gotenberg"
    assert res.ok is True                       # rendered + numeric guard passed
    assert res.deliverable == FAKE_PDF
