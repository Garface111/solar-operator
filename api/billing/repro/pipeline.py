"""
Repro pipeline — fill → render → verify, with a refine hook.

reproduce_invoice() is renderer/AI-agnostic: give it a `xlsx_filler` (a callable
that returns the filled workbook bytes) and it renders to PDF/PNG and verifies.
reproduce_for_subscription() wires the filler to the proven pixel-exact fill
(invoice_writer.populate_invoice_workbook), so the common case is one call.

The deterministic fill means a single round is usually enough — the numbers come
straight from the period data, not from the model. The refine loop exists for the
case the VERIFY step finds a structural miss (a column mapped to the wrong cell):
the hook can re-derive the field_map (repro.analyze) and re-fill. Wired as a
1-round default now; the loop body is where that correction lands next.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from . import render
from .verify import Verdict, ai_verify

log = logging.getLogger(__name__)


@dataclass
class ReproResult:
    xlsx: bytes                       # the filled workbook — pixel-exact (it IS their file)
    pdf: Optional[bytes]              # rendered PDF, or None when no renderer is configured
    png: Optional[bytes]             # first-page PNG (for verify / preview), best-effort
    verdict: Verdict                 # AI fidelity check (verdict.skipped when not run)
    backend: str                     # 'gotenberg' | 'soffice' | 'none'
    rounds: int                      # refine rounds taken

    @property
    def deliverable(self) -> bytes:
        """What to actually send: the rendered PDF when we have one, else the
        .xlsx itself (still their exact format — just not flattened to PDF)."""
        return self.pdf if self.pdf else self.xlsx


def reproduce_invoice(xlsx_filler: Callable[[], bytes], *,
                      reference_png: Optional[bytes] = None,
                      verify: bool = True,
                      max_rounds: int = 1) -> ReproResult:
    """Fill → render → verify. Never raises on a render/verify gap — it degrades
    (no PDF, skipped verdict) so a caller can still deliver the .xlsx."""
    xlsx = xlsx_filler()
    pdf: Optional[bytes] = None
    png: Optional[bytes] = None
    backend = render.active_backend()
    rounds = 1

    if render.renderer_available():
        try:
            pdf = render.render_xlsx_to_pdf(xlsx)
            png = render.render_pdf_first_page_png(pdf)
        except render.RenderError as e:
            log.warning("repro render failed (%s); delivering .xlsx: %s", backend, e)
    else:
        log.info("repro: no renderer configured; delivering .xlsx only")

    verdict = Verdict(ok=None, summary="verification not requested")
    if verify:
        verdict = ai_verify(png, reference_png)

    # Refine hook: when verify finds a STRUCTURAL miss (not just a number), a
    # future round re-derives the field_map from verdict.mismatches and re-fills.
    # The fill is deterministic from data, so we don't loop on numeric noise.
    # (Left as a single round until the remap-from-verdict path is built.)
    return ReproResult(xlsx=xlsx, pdf=pdf, png=png, verdict=verdict,
                       backend=backend, rounds=rounds)


def reproduce_for_subscription(sub, period_data=None, *,
                               reference_png: Optional[bytes] = None,
                               verify: bool = True) -> ReproResult:
    """Reproduce the invoice for a billing subscription using its stored workbook
    (the pixel-exact fill), then render + verify."""
    from ..invoice_writer import populate_invoice_workbook
    return reproduce_invoice(
        lambda: populate_invoice_workbook(sub, period_data),
        reference_png=reference_png, verify=verify)
