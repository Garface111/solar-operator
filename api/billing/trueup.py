"""Annual budget true-up for offtaker invoicing.

Budget billing charges a fixed monthly amount all year. At year-end (Sept
scheduler run, or on demand) we:

  1. Sum budgeted amounts billed over the trailing 12 months
  2. Sum the REAL solar credit value for those same months
  3. Settlement = actual − budgeted
       • actual > budgeted  → CHARGE the difference on a true-up invoice
       • actual < budgeted  → CREDIT the difference onto the next regular bill(s)

This closes the gap the scheduler previously documented as "no annual
reconciliation implemented" (#18) — utility-bill offtakers were skipped so we
wouldn't mislabel one month as a true-up.
"""
from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)


def trueup_window(as_of: Optional[date] = None) -> tuple[date, date, list[str]]:
    """Trailing 12 full calendar months ending the month BEFORE `as_of`.

    Sept 1 2026 true-up → months 2025-09 … 2026-08 (labels YYYY-MM).
    Matches the UI label "Annual true-up (Sept)".
    """
    as_of = as_of or date.today()
    # End = last day of previous calendar month
    if as_of.month == 1:
        end_y, end_m = as_of.year - 1, 12
    else:
        end_y, end_m = as_of.year, as_of.month - 1
    end = date(end_y, end_m, monthrange(end_y, end_m)[1])
    # Start = first day of the month 11 months before end_m
    start_m_idx = end_m - 11
    start_y = end_y
    if start_m_idx <= 0:
        start_m_idx += 12
        start_y -= 1
    start = date(start_y, start_m_idx, 1)

    labels: list[str] = []
    y, m = start_y, start_m_idx
    for _ in range(12):
        labels.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return start, end, labels


@dataclass
class MonthTrueup:
    period_label: str
    budgeted_usd: Optional[float] = None
    actual_usd: Optional[float] = None
    included: bool = False
    reason: Optional[str] = None  # skip reason when not included


@dataclass
class TrueupSettlement:
    ok: bool
    window_start: date
    window_end: date
    period_labels: list[str] = field(default_factory=list)
    months: list[MonthTrueup] = field(default_factory=list)
    total_budgeted: float = 0.0
    total_actual: float = 0.0
    difference: float = 0.0  # actual − budgeted (+ charge, − credit)
    charge_usd: float = 0.0
    credit_usd: float = 0.0
    months_included: int = 0
    error: Optional[str] = None
    customer_name: Optional[str] = None
    budget_amount_usd: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "period_labels": list(self.period_labels),
            "months": [
                {
                    "period_label": m.period_label,
                    "budgeted_usd": m.budgeted_usd,
                    "actual_usd": m.actual_usd,
                    "included": m.included,
                    "reason": m.reason,
                }
                for m in self.months
            ],
            "total_budgeted": round(self.total_budgeted, 2),
            "total_actual": round(self.total_actual, 2),
            "difference": round(self.difference, 2),
            "charge_usd": round(self.charge_usd, 2),
            "credit_usd": round(self.credit_usd, 2),
            "months_included": self.months_included,
            "error": self.error,
            "customer_name": self.customer_name,
            "budget_amount_usd": self.budget_amount_usd,
        }


def _period_figures(sub, period_label: str) -> MonthTrueup:
    """One month: budgeted fixed amount vs calculated solar credit value."""
    from .delivery import build_match

    row = MonthTrueup(period_label=period_label)
    budget = getattr(sub, "budget_amount_usd", None)
    try:
        match = build_match(sub, period_label=period_label)
    except Exception as exc:  # noqa: BLE001
        row.reason = f"unreadable: {exc}"
        return row
    if match is None or not getattr(match, "matched", False):
        row.reason = "no match for period"
        return row
    ci = getattr(match, "computed_invoice", None) or {}
    # Utility-bill offtakers: skip months with no settled bill (don't invent).
    if (not getattr(sub, "source_workbook", None)
            and getattr(sub, "utility_account_id", None) is not None
            and ci.get("has_utility_bill") is not True):
        row.reason = "no utility bill for period"
        return row
    # Actual = pre-budget solar credit when budget override is on.
    if ci.get("budget_override") and ci.get("solar_credit_value") is not None:
        actual = float(ci["solar_credit_value"])
    elif ci.get("amount_owed") is not None:
        # No budget, or budget path failed to preserve credit — use amount_owed
        # only when it is NOT a budget override of a different figure.
        if ci.get("budget_override") and budget is not None:
            # amount_owed is the budget; actual missing — treat as 0 actual
            actual = 0.0
            row.reason = "actual credit unavailable"
        else:
            actual = float(ci["amount_owed"])
    else:
        row.reason = "no amount for period"
        return row

    if budget is not None:
        budgeted = float(budget)
    else:
        # Without a fixed budget, "true-up" is a no-op (already billed actual).
        budgeted = actual

    row.budgeted_usd = round(budgeted, 2)
    row.actual_usd = round(actual, 2)
    row.included = True
    return row


def compute_annual_trueup(sub, *, as_of: Optional[date] = None) -> TrueupSettlement:
    """Compute year-end budget vs actual settlement for one offtaker.

    Requires a fixed `budget_amount_usd` to be meaningful. Without it, returns
    ok=False with an explanatory error (scheduler treats as benign skip).
    """
    as_of = as_of or date.today()
    start, end, labels = trueup_window(as_of)
    settlement = TrueupSettlement(
        ok=False,
        window_start=start,
        window_end=end,
        period_labels=labels,
        customer_name=getattr(sub, "customer_name", None),
        budget_amount_usd=getattr(sub, "budget_amount_usd", None),
    )

    budget = getattr(sub, "budget_amount_usd", None)
    if budget is None:
        settlement.error = (
            "Annual true-up needs a fixed monthly budget amount — set "
            "budget_amount_usd on this offtaker, or clear annual_trueup."
        )
        return settlement

    # Idempotency: don't re-true-up the same window.
    last = getattr(sub, "last_trueup_window_end", None)
    if last is not None:
        try:
            last_d = last.date() if isinstance(last, datetime) else last
            if last_d >= end:
                settlement.error = (
                    f"True-up for window ending {end.isoformat()} already "
                    f"settled (last={last_d.isoformat()})."
                )
                return settlement
        except Exception:
            pass

    months: list[MonthTrueup] = []
    total_b = 0.0
    total_a = 0.0
    n = 0
    for lab in labels:
        row = _period_figures(sub, lab)
        months.append(row)
        if row.included and row.budgeted_usd is not None and row.actual_usd is not None:
            total_b += row.budgeted_usd
            total_a += row.actual_usd
            n += 1

    settlement.months = months
    settlement.total_budgeted = round(total_b, 2)
    settlement.total_actual = round(total_a, 2)
    settlement.months_included = n
    if n == 0:
        settlement.error = (
            "No billable months in the true-up window — wait until utility "
            "bills cover the year, then re-run."
        )
        return settlement

    diff = round(total_a - total_b, 2)
    settlement.difference = diff
    if diff > 0.005:
        settlement.charge_usd = diff
        settlement.credit_usd = 0.0
    elif diff < -0.005:
        settlement.charge_usd = 0.0
        settlement.credit_usd = round(-diff, 2)
    else:
        settlement.charge_usd = 0.0
        settlement.credit_usd = 0.0
    settlement.ok = True
    return settlement


def apply_pending_credit(amount_owed: float, pending_credit: float
                         ) -> tuple[float, float, float]:
    """Apply pending credit to a regular invoice amount.

    Returns (new_amount_owed, credit_applied, credit_remaining).
    """
    try:
        due = float(amount_owed or 0.0)
    except (TypeError, ValueError):
        due = 0.0
    try:
        credit = float(pending_credit or 0.0)
    except (TypeError, ValueError):
        credit = 0.0
    if credit <= 0 or due <= 0:
        return round(max(due, 0.0), 2), 0.0, round(max(credit, 0.0), 2)
    applied = min(credit, due)
    return round(due - applied, 2), round(applied, 2), round(credit - applied, 2)


def build_trueup_match(sub, settlement: TrueupSettlement, *, operator: str = "Array Operator"):
    """Build a BillingMatch shaped for the true-up settlement invoice.

    amount_owed = charge (0 when the offtaker is owed a credit — credit is
    banked on the subscription and applied to the next regular bill).
    """
    from .matcher import BillingMatch, Period, compute_invoice

    end = settlement.window_end
    start = settlement.window_start
    label = f"True-up {start.isoformat()} → {end.isoformat()}"
    charge = float(settlement.charge_usd or 0.0)

    # compute_invoice with fixed_amount for the charge (0 is valid — credit path)
    computed = compute_invoice(
        match_customer_kwh=0.0,
        tariff=0.0,
        adder=0.0,
        billing_rate=1.0,
        billing_model="fixed_budget",
        fixed_amount=charge,
    )
    computed["invoice_number"] = f"TU-{end.year}{end.month:02d}"
    computed["period_start"] = start.isoformat()
    computed["period_end"] = end.isoformat()
    computed["month"] = label
    computed["kwh"] = 0.0
    computed["project_total_kwh"] = 0.0
    computed["has_utility_bill"] = True  # settlement is derived from bills
    computed["kwh_source"] = "trueup_settlement"
    computed["is_trueup"] = True
    computed["trueup"] = settlement.to_dict()
    # Surface both sides for invoice/email (like budget_override dual lines)
    computed["trueup_actual_usd"] = settlement.total_actual
    computed["trueup_budgeted_usd"] = settlement.total_budgeted
    computed["trueup_charge_usd"] = settlement.charge_usd
    computed["trueup_credit_usd"] = settlement.credit_usd
    # solar_credit_value = full-year actual (reference); amount_owed = charge due
    computed["solar_credit_value"] = settlement.total_actual
    if settlement.credit_usd > 0:
        computed["trueup_note"] = (
            f"You overpaid ${settlement.credit_usd:,.2f} vs actual solar value "
            f"this year. That credit will apply to your next invoice(s)."
        )
    elif settlement.charge_usd > 0:
        computed["trueup_note"] = (
            f"Actual solar value exceeded budgeted payments by "
            f"${settlement.charge_usd:,.2f}. This invoice settles the difference."
        )
    else:
        computed["trueup_note"] = (
            "Budgeted payments matched actual solar value for the year — "
            "nothing further is due."
        )

    period = Period(
        month=label, start=start, end=end,
        array_kwh=0.0, customer_kwh=0.0, tariff=0.0, adder=0.0,
        bill=charge, value=settlement.total_actual,
    )
    return BillingMatch(
        matched=True, confidence=1.0, source="trueup", data_sheet=None,
        customer={"name": sub.customer_name, "email": getattr(sub, "client_email", None)},
        allocation_pct=getattr(sub, "allocation_pct", None) or 0.0,
        billing_rate=1.0, billing_model="fixed_budget",
        periods=[period], latest_period=period,
        template={"title": "Annual True-Up — Solar Power", "operator": operator},
        computed_invoice=computed,
        project_totals={
            "total_array_kwh": 0.0,
            "total_customer_kwh": 0.0,
            "trueup": settlement.to_dict(),
        },
        warnings=[],
    )
