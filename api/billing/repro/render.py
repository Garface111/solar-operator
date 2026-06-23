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


def render_xlsx_to_pdf(xlsx_bytes: bytes) -> bytes:
    """Render a workbook to a single PDF, preserving the operator's exact layout.
    Raises RenderUnavailable when no backend is configured, RenderError on failure."""
    if GOTENBERG_URL:
        return _render_gotenberg(xlsx_bytes)
    if SOFFICE_BIN:
        return _render_soffice(xlsx_bytes)
    raise RenderUnavailable(
        "no headless renderer configured (set GOTENBERG_URL or install libreoffice-calc)")


def _render_gotenberg(xlsx_bytes: bytes) -> bytes:
    import httpx
    url = f"{GOTENBERG_URL}/forms/libreoffice/convert"
    files = {"files": ("invoice.xlsx", xlsx_bytes,
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    try:
        r = httpx.post(url, files=files, timeout=RENDER_TIMEOUT_S)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        raise RenderError(f"gotenberg convert failed: {e}") from e
    if r.content[:4] != b"%PDF":
        raise RenderError("gotenberg returned non-PDF content")
    return r.content


def _render_soffice(xlsx_bytes: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="repro-render-") as tmp:
        tmpd = pathlib.Path(tmp)
        src = tmpd / "invoice.xlsx"
        src.write_bytes(xlsx_bytes)
        # A private profile dir avoids clashing with any interactive LibreOffice.
        profile = (tmpd / "profile").as_uri()
        cmd = [
            SOFFICE_BIN, "--headless", "--nologo", "--nofirststartwizard",
            f"-env:UserInstallation={profile}",
            "--convert-to", "pdf:calc_pdf_Export", "--outdir", str(tmpd), str(src),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=RENDER_TIMEOUT_S)
        except subprocess.CalledProcessError as e:  # noqa: PERF203
            raise RenderError(
                f"soffice failed (rc={e.returncode}): {e.stderr.decode('utf-8','replace')[:400]}") from e
        except subprocess.TimeoutExpired as e:
            raise RenderError(f"soffice timed out after {RENDER_TIMEOUT_S}s") from e
        out = tmpd / "invoice.pdf"
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
