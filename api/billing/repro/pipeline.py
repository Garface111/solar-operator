"""
Repro pipeline — fill → render → VERIFY → refine.

The fill is deterministic and pixel-exact (it IS the operator's file), so the
thing that can still go wrong is a MISMAPPED column: the right layout with a
number in the wrong place. The verify loop guards exactly that:

  1. fill the workbook (optionally with an AI-corrected column map),
  2. headless-render to PDF,
  3. VERIFY — a deterministic numeric guard (is the expected Amount Due actually
     printed on it?) plus, when enabled, an AI vision check for visible breakage,
  4. if the guard fails and a remap is available, re-derive the column map and
     loop (bounded rounds); else stop.

`reproduce_invoice` returns ReproResult.ok: True (verified), False (verification
FAILED — caller should not ship this render), or None (couldn't verify, e.g. no
renderer). The send path attaches the pixel PDF only when ok is not False, so a
bad map falls back to the standard invoice instead of mailing wrong numbers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from . import render
from .verify import Verdict, ai_verify, amount_present

log = logging.getLogger(__name__)

# fill(field_map_override|None) -> xlsx bytes
Filler = Callable[[Optional[dict]], bytes]
# remap(mismatches) -> a corrected field_map, or None when no remap is possible
Remapper = Callable[[list], Optional[dict]]


@dataclass
class ReproResult:
    xlsx: bytes
    pdf: Optional[bytes]
    png: Optional[bytes]
    verdict: Verdict
    backend: str
    rounds: int
    ok: Optional[bool]                # True verified · False failed · None unverifiable
    numeric_ok: Optional[bool] = None

    @property
    def deliverable(self) -> bytes:
        """The PDF when we have one, else the .xlsx (still their exact format)."""
        return self.pdf if self.pdf else self.xlsx


def reproduce_invoice(fill: Filler, *,
                      expected_amount: Optional[float] = None,
                      reference_png: Optional[bytes] = None,
                      verify: bool = True,
                      remap: Optional[Remapper] = None,
                      max_rounds: int = 2) -> ReproResult:
    """Fill → render → verify, refining the column map up to max_rounds when the
    numeric guard fails. Never raises on a render/verify gap — degrades to a
    .xlsx-only result with ok=None."""
    override: Optional[dict] = None
    result: Optional[ReproResult] = None

    for rnd in range(1, max_rounds + 1):
        xlsx = fill(override)
        backend = render.active_backend()
        pdf: Optional[bytes] = None
        png: Optional[bytes] = None
        if render.renderer_available():
            try:
                pdf = render.render_xlsx_to_pdf(xlsx)
                png = render.render_pdf_first_page_png(pdf)
            except render.RenderError as e:
                log.warning("repro render failed (%s): %s", backend, e)

        # Deterministic guard — FAIL-CLOSED. Runs whenever we have a PDF; with no
        # expected amount it returns False (can't verify ⇒ don't trust the render).
        numeric_ok: Optional[bool] = None
        if pdf is not None:
            numeric_ok = amount_present(pdf, expected_amount)

        # AI vision check (optional, informational; only when asked + a PNG).
        verdict = Verdict(ok=None, summary="verification not requested")
        if verify and png is not None:
            verdict = ai_verify(png, reference_png)

        # Overall: ok is True ONLY when positively verified; anything uncertain is
        # False (→ caller falls back). Money gate: never ship the unverified.
        if pdf is None:
            ok: Optional[bool] = None                      # no render at all → caller's choice
        elif verdict.ok is False or numeric_ok is False:
            ok = False
        elif numeric_ok is True or verdict.ok is True:
            ok = True
        else:
            ok = False                                     # rendered but unverifiable → fail closed

        result = ReproResult(xlsx=xlsx, pdf=pdf, png=png, verdict=verdict,
                             backend=backend, rounds=rnd, ok=ok, numeric_ok=numeric_ok)

        if ok is not False or pdf is None:
            return result                                   # accept / unverifiable → done

        # Refine: ask for a corrected column map and re-fill next round.
        if not remap or rnd >= max_rounds:
            break
        try:
            new_override = remap(verdict.mismatches)
        except Exception as e:  # noqa: BLE001
            log.warning("repro remap failed: %s", e)
            new_override = None
        if not new_override or new_override == override:
            break
        log.info("repro: verify failed (round %d); refining column map and retrying", rnd)
        override = new_override

    return result  # type: ignore[return-value]


def reproduce_for_subscription(sub, period_data=None, *,
                               reference_png: Optional[bytes] = None,
                               verify: bool = True) -> ReproResult:
    """Reproduce the invoice for a billing subscription from its stored workbook,
    with the numeric guard + AI refine wired in."""
    from ..invoice_writer import populate_invoice_workbook
    from ..delivery import build_match

    match = build_match(sub)
    period = period_data if period_data is not None else match.latest_period
    expected = (match.computed_invoice or {}).get("amount_owed")

    def fill(field_map_override):
        xb = populate_invoice_workbook(sub, period, field_map_override=field_map_override)
        # Auto-fit columns so the renderer doesn't clip overflow text (same font-metric
        # issue as the operator-template path). Best-effort; original on any failure.
        try:
            import io
            from openpyxl import load_workbook
            from ..matcher import find_invoice_sheet
            from .template_repro import _autofit_columns
            wb = load_workbook(io.BytesIO(xb))
            ws = find_invoice_sheet(wb)
            if ws is not None:
                _autofit_columns(ws)
                b = io.BytesIO(); wb.save(b)
                return b.getvalue()
        except Exception:  # noqa: BLE001
            log.warning("autofit on workbook fill failed; using unfitted", exc_info=True)
        return xb

    def remap(_mismatches):
        from .analyze import ai_field_map
        wb = getattr(sub, "source_workbook", None)
        if not wb:
            return None
        r = ai_field_map(bytes(wb))
        return r.get("field_map") if r else None

    return reproduce_invoice(fill, expected_amount=expected,
                             reference_png=reference_png, verify=verify, remap=remap)
