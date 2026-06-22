"""Smoke test: models imported LAZILY (inside route/delivery functions) must
exist in api.models.

A lazy `from ..models import X` inside a request handler fails only at REQUEST
time (a 500) — not at import/module-load — so if a concurrent rebase drops the
model class from models.py, nothing catches it until prod throws. This test
fails LOUDLY in pytest the moment such a model goes missing.

History (2026-06-22): OfftakerInvoiceTemplate (the offtaker invoice-template
feature, imported lazily in api/billing/routes.py + api/billing/delivery.py) was
dropped from models.py by concurrent rebases more than once, 500ing
/v1/array-operator/billing/invoice-template with
    ImportError: cannot import name 'OfftakerInvoiceTemplate' from 'api.models'
This guards against the recurrence.
"""
import importlib


def test_lazily_imported_models_exist():
    m = importlib.import_module("api.models")
    # Names that route/delivery modules import lazily (inside functions), so a
    # drop would 500 at request time rather than fail at module load.
    required = [
        "OfftakerInvoiceTemplate",
        "Bill",
        "DailyGeneration",
        "Tenant",
    ]
    missing = [n for n in required if not hasattr(m, n)]
    assert not missing, (
        f"api.models is missing {missing} — these are imported lazily inside "
        f"request handlers, so a missing one 500s in prod instead of failing at "
        f"import. Likely dropped in a concurrent rebase; re-add the class(es)."
    )
