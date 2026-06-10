"""Writers — workbook generators.

Dispatch is by ``Array.fuel_type`` via the writer registry (``registry.py``):

  - 'solar'  → gmcs_writer  (the SACRED pixel-matched GMCS quarterly report,
               byte-for-byte identical to Bruce's master — never altered)
  - 'wind' / 'hydro' / 'digester' / 'storage' → rec_writer (the generic
               fuel-aware REC workbook: same arrays×months×MWh→REC(floor)
               layout, fuel-correct labels + a generic REC-attestation footnote)

The exported ``build_workbook`` is the registry dispatcher; it has the same
signature as the underlying writers, so existing callers
(``from api.writers import build_workbook``) are unchanged and solar reports
keep routing to the GMCS writer.

The legacy month-grid writer is kept as ``legacy_writer`` for the rare case a
tenant wants the old layout.
"""
from .registry import build_workbook, WRITERS  # noqa: F401
from . import gmcs_writer  # noqa: F401  (direct import path preserved)
from . import default_writer as legacy_writer  # noqa: F401
