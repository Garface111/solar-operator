"""Debt paydown & APR optimization — turn a pile of cards into a plan.

Given the household's real card balances and whatever APRs and minimums are on
file, this models the month-by-month payoff under different strategies so the
copilot can say, concretely: pay THIS card first, you'll be debt-free in N
months, and it saves you $X of interest versus paying minimums.

Two honesty rules run through it:

* Estimates are labeled. A card with no APR on file is modeled at a flagged
  default so a plan still exists, and the snapshot says exactly which cards need
  their real APR for the numbers to be exact — the tool drives data completeness
  instead of quietly guessing.
* The minimums-only baseline is always computed, because the whole point of a
  paydown plan is the contrast: what your money buys you versus the cost of
  drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts_terms import terms_by_account
from ..models import Account

# A card with no APR on file is modeled here, clearly flagged. Chosen high
# because credit-card APRs are, and an under-estimate would make drift look
# cheaper than it is.
DEFAULT_CARD_APR = 24.0
MIN_FLOOR = 25.0        # a card minimum rarely drops below this
MIN_PCT = 0.02          # ...or ~2% of the balance, whichever is larger
MONTH_CAP = 600         # 50 years: a safety stop for an infeasible budget

# Revolving CARD debt is what this optimizer is for — high APR, no fixed term,
# the stuff worth attacking. Installment loans (auto, personal) and the mortgage
# are cheaper fixed-term debt you keep on schedule, so they are opt-in only and
# never swamp the card plan by default.
DEBT_KINDS = ("credit",)


@dataclass
class Debt:
    account_id: str
    name: str
    owed: float          # positive dollars owed
    apr: float           # annual %, e.g. 24.0
    minimum: float       # recorded minimum, or 0 if unknown
    apr_estimated: bool
    min_estimated: bool


@dataclass
class PlanResult:
    strategy: str
    months: int
    total_interest: float
    total_paid: float
    payoff_month: dict = field(default_factory=dict)  # account_id -> month paid off
    order: list = field(default_factory=list)          # names, in the order they clear
    feasible: bool = True


def _effective_min(balance: float) -> float:
    return min(balance, max(MIN_FLOOR, balance * MIN_PCT))


def snapshot(session: Session, include_loans: bool = False,
             include_mortgage: bool = False) -> list[Debt]:
    """Every interest-bearing debt with balance, APR, and minimum — flagged
    where a figure had to be estimated. Cards only by default; installment loans
    and the mortgage are opt-in."""
    kinds = DEBT_KINDS
    if include_loans:
        kinds = kinds + ("loan",)
    if include_mortgage:
        kinds = kinds + ("mortgage",)
    terms = terms_by_account(session)
    debts: list[Debt] = []
    for account in session.execute(select(Account)).scalars():
        if account.kind not in kinds or account.balance is None or account.balance >= 0:
            continue
        owed = round(-account.balance, 2)
        t = terms.get(account.id, {})
        apr = t.get("apr")
        minimum = t.get("minimum_payment")
        debts.append(Debt(
            account_id=account.id,
            name=account.name,
            owed=owed,
            apr=float(apr) if apr is not None else DEFAULT_CARD_APR,
            minimum=float(minimum) if minimum is not None else 0.0,
            apr_estimated=apr is None,
            min_estimated=minimum is None,
        ))
    debts.sort(key=lambda d: d.owed, reverse=True)
    return debts


def _pick_target(remaining: list[str], bals: dict, aprs: dict, strategy: str) -> str:
    if strategy == "snowball":
        return min(remaining, key=lambda i: bals[i])
    return max(remaining, key=lambda i: aprs[i])  # avalanche: highest APR first


def _simulate(debts: list[Debt], monthly_budget: float, strategy: str) -> PlanResult:
    """Month-by-month: accrue interest, pay each card's minimum, throw every
    spare dollar at the strategy's target, until everything clears."""
    bals = {d.account_id: d.owed for d in debts}
    aprs = {d.account_id: d.apr / 100.0 for d in debts}
    names = {d.account_id: d.name for d in debts}
    total_interest = total_paid = 0.0
    payoff_month: dict[str, int] = {}
    order: list[str] = []
    month = 0
    while any(b > 0.005 for b in bals.values()) and month < MONTH_CAP:
        month += 1
        for i, b in bals.items():
            if b > 0:
                interest = b * aprs[i] / 12.0
                bals[i] = b + interest
                total_interest += interest
        budget = monthly_budget
        # minimums first, on every card
        for i in bals:
            if bals[i] <= 0 or budget <= 0:
                continue
            pay = min(bals[i], _effective_min(bals[i]), budget)
            bals[i] -= pay
            budget -= pay
            total_paid += pay
        # every spare dollar to the target
        while budget > 0.005:
            remaining = [i for i in bals if bals[i] > 0.005]
            if not remaining:
                break
            target = _pick_target(remaining, bals, aprs, strategy)
            pay = min(bals[target], budget)
            bals[target] -= pay
            budget -= pay
            total_paid += pay
        for i in bals:
            if bals[i] <= 0.005 and i not in payoff_month:
                payoff_month[i] = month
                order.append(names[i])
    feasible = all(b <= 0.005 for b in bals.values())
    return PlanResult(
        strategy=strategy, months=month, total_interest=round(total_interest, 2),
        total_paid=round(total_paid, 2), payoff_month=payoff_month, order=order,
        feasible=feasible,
    )


def _simulate_minimums(debts: list[Debt]) -> PlanResult:
    """The do-nothing-extra baseline: pay only each card's minimum, forever."""
    bals = {d.account_id: d.owed for d in debts}
    aprs = {d.account_id: d.apr / 100.0 for d in debts}
    total_interest = total_paid = 0.0
    month = 0
    while any(b > 0.005 for b in bals.values()) and month < MONTH_CAP:
        month += 1
        for i, b in bals.items():
            if b > 0:
                interest = b * aprs[i] / 12.0
                bals[i] = b + interest
                total_interest += interest
        for i in bals:
            if bals[i] <= 0:
                continue
            pay = _effective_min(bals[i])
            # a minimum below the monthly interest never retires the card
            bals[i] -= pay
            total_paid += pay
    feasible = all(b <= 0.005 for b in bals.values())
    return PlanResult(
        strategy="minimums_only", months=month, total_interest=round(total_interest, 2),
        total_paid=round(total_paid, 2), feasible=feasible,
    )


def _plan_dict(p: PlanResult) -> dict:
    return {
        "strategy": p.strategy,
        "months_to_debt_free": p.months if p.feasible else None,
        "years": round(p.months / 12, 1) if p.feasible else None,
        "total_interest": p.total_interest,
        "payoff_order": p.order,
        "feasible": p.feasible,
    }


def optimize(session: Session, *, monthly_budget: float,
             include_loans: bool = False, include_mortgage: bool = False) -> dict:
    """Compare avalanche, snowball, and minimums-only for a monthly budget."""
    debts = snapshot(session, include_loans=include_loans, include_mortgage=include_mortgage)
    if not debts:
        return {"error": "no interest-bearing debts on file"}
    total_owed = round(sum(d.owed for d in debts), 2)
    total_min = round(sum(_effective_min(d.owed) for d in debts), 2)

    result: dict = {
        "total_owed": total_owed,
        "monthly_budget": monthly_budget,
        "total_monthly_minimums": total_min,
        "debts": [
            {
                "name": d.name, "owed": d.owed, "apr": d.apr,
                "monthly_interest": round(d.owed * d.apr / 100 / 12, 2),
                "apr_estimated": d.apr_estimated, "min_estimated": d.min_estimated,
            }
            for d in debts
        ],
        "needs_real_apr": [d.name for d in debts if d.apr_estimated],
    }
    if monthly_budget < total_min:
        result["warning"] = (
            f"${monthly_budget:,.0f}/mo is below the ~${total_min:,.0f} of combined "
            "minimums — at this budget the balances grow. Raise the budget or the "
            "plan cannot retire the debt."
        )

    avalanche = _simulate(debts, monthly_budget, "avalanche")
    snowball = _simulate(debts, monthly_budget, "snowball")
    minimums = _simulate_minimums(debts)
    result["avalanche"] = _plan_dict(avalanche)
    result["snowball"] = _plan_dict(snowball)
    result["minimums_only"] = _plan_dict(minimums)
    if avalanche.feasible and minimums.feasible:
        result["interest_saved_vs_minimums"] = round(
            minimums.total_interest - avalanche.total_interest, 2)
        result["months_saved_vs_minimums"] = minimums.months - avalanche.months
    result["recommended"] = "avalanche"  # least total interest by construction
    result["note"] = (
        "Avalanche (highest APR first) always pays the least interest; snowball "
        "(smallest balance first) clears individual cards sooner for momentum. "
        "Figures for cards without a real APR on file are estimates at "
        f"{DEFAULT_CARD_APR:.0f}% — get those APRs for an exact plan."
    )
    return result


def balance_transfer(
    session: Session, *, account_name: str, promo_apr: float, promo_months: int,
    fee_pct: float, monthly_payment: float,
) -> dict:
    """Model moving one card's balance to a promo-rate offer: the upfront fee,
    interest across the promo window, and whether the payment clears it in time."""
    debts = {d.name.lower(): d for d in snapshot(session)}
    match = next((d for name, d in debts.items() if account_name.lower() in name), None)
    if match is None:
        return {"error": f"no card matching {account_name!r} — see the snapshot names"}

    balance = match.owed
    fee = round(balance * fee_pct / 100, 2)
    transferred = balance + fee
    monthly_rate = promo_apr / 100 / 12
    bal = transferred
    interest = 0.0
    months = 0
    while bal > 0.005 and months < MONTH_CAP:
        months += 1
        i = bal * monthly_rate
        interest += i
        bal = bal + i - min(bal + i, monthly_payment)
        if months == promo_months:
            promo_end_balance = round(bal, 2)
    paid_in_promo = months <= promo_months
    # what staying put would have cost over the same payoff horizon
    stay = _simulate([match], monthly_payment, "avalanche")
    return {
        "card": match.name,
        "balance": balance,
        "transfer_fee": fee,
        "promo_apr": promo_apr,
        "promo_months": promo_months,
        "monthly_payment": monthly_payment,
        "months_to_clear": months,
        "cleared_within_promo": paid_in_promo,
        "balance_when_promo_ends": None if paid_in_promo else promo_end_balance,
        "interest_under_transfer": round(interest, 2),
        "interest_if_you_stay": stay.total_interest,
        "net_savings": round(stay.total_interest - interest - fee, 2),
        "note": (
            "Net savings = interest saved minus the transfer fee. If it does not "
            "clear within the promo window, the leftover reverts to the card's "
            "regular APR — factor that risk in."
            + ("" if not match.apr_estimated else
               f" {match.name}'s current APR is an estimate; get the real one.")
        ),
    }
