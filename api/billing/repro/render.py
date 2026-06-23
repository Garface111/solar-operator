"""
Headless XLSX → PDF / PNG rendering — the "to the pixel" delivery step.

The operator's filled workbook (invoice_writer.populate_invoice_workbook) is
already pixel-exact — it IS their file. To deliver/preview it as a fixed
document we render it with a real office engine, NOT xhtml2pdf (which can't read
a workbook and wouldn't match Excel's layout anyway).

Two interchangeable backends, picked by env:
  * GOTENBERG_URL — a Gotenberg service (Docker) that does LibreOffice
    conversion over HTTP. Preferred for prod: no binary in the app image, scales
    independently. POST the xlsx to {GOTENBERG_URL}/forms/libreoffice/convert.
  * SOFFICE_BIN / `soffice` on PATH — LibreOffice headless subprocess. Good for
    local/dev or an app image that bundles libreoffice-calc.

renderer_available() lets callers degrade gracefully (e.g. fall back to
delivering the .xlsx itself) when neither backend is configured — so turning the
wrapper on never hard-fails a send.

INFRA NOTE: neither backend ships in the current Railway image. To go live, add
ONE of: a Gotenberg service (set GOTENBERG_URL), or `libreoffice-calc` to the
build (nixpacks `nixPkgs`/apt) and leave SOFFICE_BIN unset to auto-detect.
"""
from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
import tempfile

log = logging.getLogger(__name__)

GOTENBERG_URL = os.getenv("GOTENBERG_URL", "").rstrip("/") or None
SOFFICE_BIN = (os.getenv("SOFFICE_BIN")
               or shutil.which("soffice")
               or shutil.which("libreoffice"))
RENDER_TIMEOUT_S = int(os.getenv("REPRO_RENDER_TIMEOUT_S", "120"))


class RenderUnavailable(RuntimeError):
    """No headless renderer is configured — caller should fall back to .xlsx."""


class RenderError(RuntimeError):
    """A renderer was configured but the conversion failed."""


def renderer_available() -> bool:
    return bool(GOTENBERG_URL or SOFFICE_BIN)


def active_backend() -> str:
    if GOTENBERG_URL:
        return "gotenberg"
    if SOFFICE_BIN:
        return "soffice"
    return "none"


def render_office_to_pdf(file_bytes: bytes, filename: str = "invoice.xlsx") -> bytes:
    """Render any LibreOffice-convertible office doc (xlsx/xls/docx/doc/odt/ods…)
    to a single PDF, preserving the original's exact layout. The filename's
    extension tells the engine which converter to use. Raises RenderUnavailable
    when no backend is configured, RenderError on failure."""
    if GOTENBERG_URL:
        return _render_gotenberg(file_bytes, filename)
    if SOFFICE_BIN:
        return _render_soffice(file_bytes, filename)
    raise RenderUnavailable(
        "no headless renderer configured (set GOTENBERG_URL or install libreoffice-calc)")


def render_xlsx_to_pdf(xlsx_bytes: bytes) -> bytes:
    """Render a workbook to PDF (thin wrapper over render_office_to_pdf)."""
    return render_office_to_pdf(xlsx_bytes, "invoice.xlsx")


def _render_gotenberg(file_bytes: bytes, filename: str = "invoice.xlsx") -> bytes:
    import httpx
    url = f"{GOTENBERG_URL}/forms/libreoffice/convert"
    files = {"files": (filename, file_bytes, "application/octet-stream")}
    try:
        r = httpx.post(url, files=files, timeout=RENDER_TIMEOUT_S)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        raise RenderError(f"gotenberg convert failed: {e}") from e
    if r.content[:4] != b"%PDF":
        raise RenderError("gotenberg returned non-PDF content")
    return r.content


def _safe_office_name(filename: str) -> str:
    """A safe local filename keeping the original extension (so LibreOffice picks
    the right converter)."""
    ext = ("." + filename.rsplit(".", 1)[1].lower()) if "." in filename else ".xlsx"
    if len(ext) > 6 or not ext[1:].isalnum():
        ext = ".xlsx"
    return "doc" + ext


def _render_soffice(file_bytes: bytes, filename: str = "invoice.xlsx") -> bytes:
    with tempfile.TemporaryDirectory(prefix="repro-render-") as tmp:
        tmpd = pathlib.Path(tmp)
        src = tmpd / _safe_office_name(filename)
        src.write_bytes(file_bytes)
        # A private profile dir avoids clashing with any interactive LibreOffice.
        profile = (tmpd / "profile").as_uri()
        cmd = [
            SOFFICE_BIN, "--headless", "--nologo", "--nofirststartwizard",
            f"-env:UserInstallation={profile}",
            "--convert-to", "pdf", "--outdir", str(tmpd), str(src),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=RENDER_TIMEOUT_S)
        except subprocess.CalledProcessError as e:  # noqa: PERF203
            raise RenderError(
                f"soffice failed (rc={e.returncode}): {e.stderr.decode('utf-8','replace')[:400]}") from e
        except subprocess.TimeoutExpired as e:
            raise RenderError(f"soffice timed out after {RENDER_TIMEOUT_S}s") from e
        out = tmpd / (src.stem + ".pdf")
        if not out.exists():
            raise RenderError("soffice produced no PDF")
        return out.read_bytes()


def render_pdf_first_page_png(pdf_bytes: bytes, dpi: int = 120) -> bytes | None:
    """First page of a PDF as PNG, for the AI verify step. Best-effort: needs
    pdftoppm (poppler) or pymupdf; returns None when neither is available so
    verification degrades to text-only rather than crashing."""
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        with tempfile.TemporaryDirectory(prefix="repro-png-") as tmp:
            tmpd = pathlib.Path(tmp)
            (tmpd / "in.pdf").write_bytes(pdf_bytes)
            try:
                subprocess.run(
                    [pdftoppm, "-png", "-r", str(dpi), "-f", "1", "-l", "1",
                     str(tmpd / "in.pdf"), str(tmpd / "page")],
                    check=True, capture_output=True, timeout=RENDER_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                log.warning("pdftoppm failed: %s", e)
                return None
            pngs = sorted(tmpd.glob("page*.png"))
            return pngs[0].read_bytes() if pngs else None
    try:
        import fitz  # pymupdf
    except Exception:  # noqa: BLE001
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pix = doc.load_page(0).get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    except Exception as e:  # noqa: BLE001
        log.warning("pymupdf render failed: %s", e)
        return None


# Labels that mark an actual invoice page (vs a Data ledger / Trends / chart sheet).
_INVOICE_PAGE_MARKERS = ("amount due", "amount owed", "total due", "payable to",
                         "bill to", "invoice number", "invoice no", "remit")


def trim_pdf_to_invoice_page(pdf_bytes: bytes, expected_amount: float | None) -> bytes:
    """A rendered multi-SHEET workbook is a multi-page PDF (Data ledger, Trends,
    annual True-Up…); the offtaker should receive ONLY their invoice. Keep the
    contiguous run of pages that look like an invoice AND carry THIS invoice's
    expected total — which excludes the Data/Trends pages (no markers) and a SEPARATE
    annual true-up invoice (different total). Deterministic and best-effort: falls
    back to markers-only, then to the original PDF, so it can never blank a send."""
    try:
        import io as _io
        import pdfplumber
        from pypdf import PdfReader, PdfWriter
        from .verify import _parse_signed
    except Exception as e:  # noqa: BLE001
        log.warning("trim_pdf_to_invoice_page: deps unavailable (%s); leaving PDF whole", e)
        return pdf_bytes
    try:
        with pdfplumber.open(_io.BytesIO(pdf_bytes)) as pdf:
            texts = [(pg.extract_text() or "").lower() for pg in pdf.pages]
    except Exception as e:  # noqa: BLE001
        log.warning("trim_pdf_to_invoice_page: unreadable PDF (%s); leaving whole", e)
        return pdf_bytes
    if len(texts) <= 1:
        return pdf_bytes

    def markers(t: str) -> bool:
        return any(k in t for k in _INVOICE_PAGE_MARKERS)

    def amount(t: str) -> bool:
        return expected_amount is not None and any(
            abs(n - float(expected_amount)) <= 0.01 for n in _parse_signed(t))

    keep = [i for i, t in enumerate(texts) if markers(t) and amount(t)]
    if not keep:
        keep = [i for i, t in enumerate(texts) if markers(t)]
    if not keep:
        return pdf_bytes                                 # nothing recognizable → don't trim
    # contiguous run from the first invoice page (keeps a 2-page invoice whole, but
    # stops before a later separate invoice that opens its own "invoice number").
    start = keep[0]
    run = [start]
    for i in range(start + 1, len(texts)):
        if i in keep or (markers(texts[i]) and "invoice number" not in texts[i]):
            run.append(i)
        else:
            break
    if len(run) == len(texts):
        return pdf_bytes
    try:
        reader = PdfReader(_io.BytesIO(pdf_bytes))
        writer = PdfWriter()
        for i in run:
            writer.add_page(reader.pages[i])
        out = _io.BytesIO()
        writer.write(out)
        log.info("trim_pdf_to_invoice_page: %d→%d pages (kept %s)",
                 len(texts), len(run), [i + 1 for i in run])
        return out.getvalue()
    except Exception as e:  # noqa: BLE001
        log.warning("trim_pdf_to_invoice_page: rewrite failed (%s); leaving whole", e)
        return pdf_bytes
