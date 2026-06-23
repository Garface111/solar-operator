"""
Report Reproduction wrapper — reproduce an operator's report "down to the pixel".

The thesis (decided with Ford, Jun 2026): for a spreadsheet operator the
pixel-perfect source already exists — THEIR OWN FILE. We don't redraw the
invoice in our layout (that path is api/billing/template_render.py → xhtml2pdf,
which only ever approximates). We:

  1. FILL their real workbook with the period's data, preserving every style,
     formula, merged cell and their bespoke Template sheet
     (api/billing/invoice_writer.populate_invoice_workbook — already pixel-exact).
  2. RENDER that workbook to PDF with a headless engine (LibreOffice / Gotenberg)
     so what we deliver is byte-for-byte their format (repro.render).
  3. (AI, thoughtful) ANALYZE the workbook when the heuristic matcher can't map
     the data cells, so the fill targets the right cells (repro.analyze).
  4. (AI, to-the-pixel) VERIFY the rendered output against a reference image and
     surface mismatches, optionally looping until it matches (repro.verify).

"Thoughtful" = the AI figures out WHERE the data goes and whether the result
matches. "Mechanical" = deterministic openpyxl fill + headless render. The AI
never draws the invoice; it only locates fields and checks fidelity.

Feature-flagged (REPRO_ENABLED). Nothing here is wired into the live send path
yet — it's the foundation. Entry point: repro.pipeline.reproduce_invoice.
"""
from __future__ import annotations

import os


def repro_enabled() -> bool:
    """Master flag — keep the wrapper dark in prod until explicitly switched on."""
    return os.getenv("REPRO_ENABLED", "").lower() in ("1", "true", "yes", "on")


__all__ = ["repro_enabled"]
