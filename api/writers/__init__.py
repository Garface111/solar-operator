"""Writers — workbook generators.

GMCS-format quarterly NEPOOL-ready workbook is the default for all tenants
(matches Bruce's master file). The legacy month-grid writer is kept as
`legacy_writer` for the rare case a tenant wants the old layout.
"""
from .gmcs_writer import build_workbook  # default
from . import default_writer as legacy_writer  # noqa: F401
