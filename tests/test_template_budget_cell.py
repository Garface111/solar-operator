"""Regression: a template's 'Fixed Monthly Budget Payment' cell is a per-offtaker
budget, tokenized as {{ budget }}, so the replicator fills each offtaker's OWN
budget and BLANKS it for offtakers who aren't on a budget — instead of leaking
the template author's budget (Fairlee $1,250) onto them and tripping the
sample-leak guard (the 'Norwich falls back to generic invoice' bug).
"""
from api.billing.matcher import _TOKEN_LABELS
from api.billing.repro.template_repro import offtaker_values_from_match, _TOKEN_KEYS


def test_fixed_monthly_budget_label_is_tokenized():
    assert dict(_TOKEN_LABELS).get("fixed monthly budget") == "{{ budget }}"
    assert "budget" in _TOKEN_KEYS


class _Match:
    latest_period = None
    def __init__(self, ci):
        self.computed_invoice = ci


def test_budget_value_present_only_for_budget_offtakers():
    # On a budget (budget_override True) -> budget = the fixed amount owed
    on = offtaker_values_from_match(_Match({"amount_owed": 1250.0, "budget_override": True}))
    assert on.get("budget") == 1250.0
    # NOT on a budget -> 'budget' absent from values, so the mapped cell BLANKS
    # (never shows the template author's number).
    off = offtaker_values_from_match(_Match({"amount_owed": 699.49}))
    assert "budget" not in off
