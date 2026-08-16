"""Long-horizon wealth projection — the decade-scale sibling of ``forecast.py``.

``forecast.cash_flow_forecast`` answers "will we dip below zero before the next
paycheck" by replaying detected recurring events day by day. This module answers
"where does this household land in ten years, and what does buying that house do
to the answer" — same data, different time constant.

The model is deliberately small enough to explain out loud:

  * A **baseline** is derived from the observed record: the median *complete*
    calendar month's net (income minus spend, transfers excluded), plus the
    current liquid / investment / property / debt split of the accounts.
  * A **projection** walks that baseline forward one year at a time. Investments
    compound at a sampled annual return; liquid grows only by savings (cash earns
    nothing here — an explicit, conservative choice); property tracks inflation
    (so it is flat in real terms); existing debt is held flat in nominal dollars.
    Every figure is reported in **today's dollars**.
  * A **Monte Carlo** run repeats that walk with returns drawn from a normal
    distribution and reports p10 / p50 / p90 bands. Randomness always comes from
    an explicit ``random.Random(seed)``; the resolved seed is echoed in the
    output so any run can be reproduced exactly.

Nothing here is a black box and nothing here is advice. Every function returns a
JSON-serializable dict carrying an ``assumptions`` list, and every function that
leans on thin data says so in the output rather than quietly guessing.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, Transaction
from .recurring import detect_recurring

# Account-kind buckets. Kinds not listed fall into "other" and are carried flat.
LIQUID_KINDS = ("checking", "savings")
INVESTMENT_KINDS = ("investment", "retirement", "brokerage")
PROPERTY_KINDS = ("property", "vehicle")
DEBT_KINDS = ("credit", "mortgage", "loan")

# Recurring cadence -> occurrences per month (used only for the income picture).
CADENCE_PER_MONTH = {
    "weekly": 52.0 / 12.0,
    "biweekly": 26.0 / 12.0,
    "monthly": 1.0,
    "quarterly": 1.0 / 3.0,
    "yearly": 1.0 / 12.0,
}

BASELINE_LOOKBACK_MONTHS = 24
# Below this many complete months the baseline is called out as thin.
THIN_DATA_MONTHS = 6
MIN_CONFIDENT_MONTHS = 3
# A recurring inflow smaller than this is treated as noise, not household income.
INCOME_SERIES_MIN = 200.0
# A sampled annual return below this is clipped: -100% would zero the portfolio.
MIN_ANNUAL_RETURN = -0.95

MAX_YEARS = 50
MAX_SIMULATIONS = 5000


def _r(value: float) -> float:
    """Round a dollar figure, normalizing -0.0 to 0.0."""
    rounded = round(float(value), 2)
    return 0.0 if rounded == 0.0 else rounded


def _month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def _months_back(anchor: date, months: int) -> date:
    """First day of the month ``months`` before ``anchor``'s month."""
    total = anchor.year * 12 + (anchor.month - 1) - months
    return date(total // 12, total % 12 + 1, 1)


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile over an already-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return sorted_values[int(pos)]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #


def account_buckets(session: Session) -> dict[str, float]:
    """Current balances split into liquid / investments / property / debt / other.

    The five buckets sum to the same total as ``insights.net_worth``.
    """
    buckets = {"liquid": 0.0, "investments": 0.0, "property": 0.0, "debt": 0.0, "other": 0.0}
    for account in session.execute(select(Account)).scalars():
        balance = account.balance
        if balance is None:
            continue
        kind = (account.kind or "other").lower()
        if kind in LIQUID_KINDS:
            buckets["liquid"] += balance
        elif kind in INVESTMENT_KINDS:
            buckets["investments"] += balance
        elif kind in PROPERTY_KINDS:
            buckets["property"] += balance
        elif kind in DEBT_KINDS:
            buckets["debt"] += balance
        else:
            buckets["other"] += balance
    return buckets


def _monthly_nets(
    session: Session, *, today: date, lookback_months: int
) -> tuple[list[dict], bool]:
    """Per-calendar-month income/spend/net, transfers and pendings excluded.

    Returns ``(months, used_partial_month)``. The in-progress month is dropped —
    a half-finished month understates spend and would bias the median low. If
    dropping it leaves nothing, it is kept and the flag comes back True.
    """
    since = _months_back(today, lookback_months)
    rows = session.execute(
        select(Transaction.posted, Transaction.amount).where(
            Transaction.posted >= since,
            Transaction.pending.is_(False),
            Transaction.category != "transfer",
        )
    ).all()

    income: dict[str, float] = {}
    spend: dict[str, float] = {}
    for posted, amount in rows:
        key = _month_key(posted)
        if amount >= 0:
            income[key] = income.get(key, 0.0) + amount
        else:
            spend[key] = spend.get(key, 0.0) + amount

    current = _month_key(today)
    all_keys = sorted(set(income) | set(spend))
    complete = [k for k in all_keys if k < current]
    used_partial = False
    if not complete and all_keys:
        complete = all_keys
        used_partial = True

    months = [
        {
            "month": key,
            "income": _r(income.get(key, 0.0)),
            "spend": _r(spend.get(key, 0.0)),
            "net": _r(income.get(key, 0.0) + spend.get(key, 0.0)),
        }
        for key in complete
    ]
    return months, used_partial


def _income_cadence(session: Session) -> dict:
    """Detected recurring inflows, summarized into a cadence + implied monthly income."""
    series = [
        s
        for s in detect_recurring(session)
        if not s.is_bill and s.avg_amount >= INCOME_SERIES_MIN
    ]
    streams = [
        {
            "merchant": s.merchant,
            "cadence": s.cadence,
            "per_payment": _r(s.avg_amount),
            "implied_monthly": _r(s.avg_amount * CADENCE_PER_MONTH.get(s.cadence, 1.0)),
            "seen_count": s.count,
            "last_seen": s.last_date.isoformat(),
        }
        for s in series
    ]
    streams.sort(key=lambda s: -s["implied_monthly"])
    return {
        "dominant_cadence": streams[0]["cadence"] if streams else None,
        "implied_monthly_income": _r(sum(s["implied_monthly"] for s in streams)),
        "streams": streams,
        "note": (
            "Income cadence comes from detect_recurring — a paycheck that varies by "
            "more than ~30% or has fewer than 3 occurrences will not be detected."
            if not streams
            else "Detected recurring inflows only; bonuses and one-off income are excluded."
        ),
    }


def derive_baseline(session: Session, *, lookback_months: int = BASELINE_LOOKBACK_MONTHS,
                    today: date | None = None) -> dict:
    """Observed savings rate, balance split, and income cadence — with honesty flags.

    ``monthly_savings`` is the **median** complete calendar month's net (income
    minus spend, transfers excluded). Median rather than mean so one bonus or one
    roof replacement does not redefine the household's normal.
    """
    today = today or date.today()
    months, used_partial = _monthly_nets(
        session, today=today, lookback_months=lookback_months
    )
    buckets = account_buckets(session)

    nets = [m["net"] for m in months]
    spends = [abs(m["spend"]) for m in months]
    incomes = [m["income"] for m in months]
    months_observed = len(months)

    monthly_savings = _r(median(nets)) if nets else 0.0
    monthly_spend = _r(median(spends)) if spends else 0.0
    monthly_income = _r(median(incomes)) if incomes else 0.0
    savings_rate = _r(monthly_savings / monthly_income * 100.0) if monthly_income > 0 else None

    if months_observed >= THIN_DATA_MONTHS:
        confidence = "high"
    elif months_observed >= MIN_CONFIDENT_MONTHS:
        confidence = "moderate"
    else:
        confidence = "low"

    caveats: list[str] = []
    if months_observed == 0:
        caveats.append(
            "NO complete month of transaction history — the savings rate is 0 by "
            "default, not by observation. Any projection from here is a placeholder."
        )
    elif months_observed < MIN_CONFIDENT_MONTHS:
        caveats.append(
            f"Only {months_observed} complete month(s) of history — the median is "
            "barely a median. Treat the savings rate as provisional."
        )
    elif months_observed < THIN_DATA_MONTHS:
        caveats.append(
            f"{months_observed} complete months of history — enough for a rough "
            f"rate, short of the {THIN_DATA_MONTHS} months that make it stable."
        )
    if used_partial:
        caveats.append(
            "The only month available is still in progress, so its spend is "
            "incomplete and the savings rate is biased HIGH."
        )
    if monthly_income <= 0 and months_observed:
        caveats.append(
            "No positive income landed in the transaction record — paychecks may "
            "post to an unlinked account, which makes the savings rate meaningless."
        )
    if buckets["investments"] == 0 and buckets["liquid"] > 0:
        caveats.append(
            "No investment account is linked, so the projection compounds nothing "
            "and will read pessimistically."
        )

    return {
        "as_of": today.isoformat(),
        "months_observed": months_observed,
        "months_requested": lookback_months,
        "data_thin": months_observed < THIN_DATA_MONTHS,
        "confidence": confidence,
        "monthly_savings": monthly_savings,
        "monthly_income": monthly_income,
        "monthly_spend": monthly_spend,
        "savings_rate_pct": savings_rate,
        "balances": {
            "liquid": _r(buckets["liquid"]),
            "investments": _r(buckets["investments"]),
            "property": _r(buckets["property"]),
            "debt": _r(buckets["debt"]),
            "other": _r(buckets["other"]),
            "net_worth": _r(sum(buckets.values())),
        },
        "income": _income_cadence(session),
        "months": months,
        "caveats": caveats,
        "assumptions": [
            "Monthly savings = median of COMPLETE calendar months (income + spend); "
            "the in-progress month is excluded.",
            "Transactions categorized 'transfer' are excluded so moving money between "
            "own accounts is not counted as income or spend.",
            "Pending transactions are excluded.",
            f"Lookback window is {lookback_months} months.",
            "Balances are whatever each account last reported — a stale feed or a "
            "hand-entered property value flows straight through.",
        ],
    }


# --------------------------------------------------------------------------- #
# Projection engine
# --------------------------------------------------------------------------- #


@dataclass
class _Loan:
    """An amortizing loan added by a scenario (e.g. a new mortgage)."""

    principal: float
    monthly_rate: float
    monthly_payment: float
    term_months: int


@dataclass
class _Scenario:
    """Starting state plus the monthly flow that drives one projected path."""

    liquid: float
    investments: float
    property_value: float
    debt: float
    other: float
    monthly_savings: float
    loan: _Loan | None = None


def _loan_balance(loan: _Loan | None, months_elapsed: int) -> float:
    """Remaining principal after ``months_elapsed`` payments (closed form)."""
    if loan is None:
        return 0.0
    if months_elapsed >= loan.term_months:
        return 0.0
    if loan.monthly_rate == 0.0:
        return max(0.0, loan.principal - loan.monthly_payment * months_elapsed)
    growth = (1.0 + loan.monthly_rate) ** months_elapsed
    balance = loan.principal * growth - loan.monthly_payment * (growth - 1.0) / loan.monthly_rate
    return max(0.0, balance)


def _walk(scenario: _Scenario, annual_returns: list[float], inflation: float) -> list[dict]:
    """One deterministic pass. ``annual_returns`` has one entry per projected year.

    Real (today's-dollar) figures are the nominal path deflated by ``inflation``.
    """
    liquid = scenario.liquid
    investments = scenario.investments
    property_value = scenario.property_value
    points: list[dict] = []

    def point(year: int) -> dict:
        loan_balance = _loan_balance(scenario.loan, year * 12)
        nominal = (
            liquid + investments + property_value + scenario.debt + scenario.other - loan_balance
        )
        deflator = (1.0 + inflation) ** year
        return {
            "year": year,
            "liquid": _r(liquid / deflator),
            "investments": _r(investments / deflator),
            "property": _r(property_value / deflator),
            "debt": _r((scenario.debt - loan_balance) / deflator),
            "other": _r(scenario.other / deflator),
            "net_worth": _r(nominal / deflator),
            "net_worth_nominal": _r(nominal),
        }

    points.append(point(0))
    for index, annual_return in enumerate(annual_returns, start=1):
        investments *= 1.0 + annual_return
        liquid += scenario.monthly_savings * 12.0
        property_value *= 1.0 + inflation
        points.append(point(index))
    return points


def _bands(
    scenario: _Scenario,
    *,
    years: int,
    annual_return: float,
    return_volatility: float,
    inflation: float,
    simulations: int,
    rng: random.Random,
) -> list[dict]:
    """Monte Carlo p10/p50/p90 of real net worth at each year."""
    per_year: list[list[float]] = [[] for _ in range(years + 1)]
    for _ in range(simulations):
        draws = [
            max(MIN_ANNUAL_RETURN, rng.gauss(annual_return, return_volatility))
            for _ in range(years)
        ]
        for point in _walk(scenario, draws, inflation):
            per_year[point["year"]].append(point["net_worth"])
    out: list[dict] = []
    for year, values in enumerate(per_year):
        values.sort()
        out.append(
            {
                "year": year,
                "p10": _r(_percentile(values, 0.10)),
                "p50": _r(_percentile(values, 0.50)),
                "p90": _r(_percentile(values, 0.90)),
            }
        )
    return out


def _cash_out_year(points: list[dict]) -> int | None:
    for point in points:
        if point["liquid"] < 0:
            return point["year"]
    return None


def _resolve_seed(seed: int | None) -> int:
    """Never leave randomness implicit: an unspecified seed is drawn and reported."""
    if seed is not None:
        return int(seed)
    return random.SystemRandom().randrange(2**32)


def _projection_assumptions(
    *, annual_return: float, return_volatility: float, inflation: float, simulations: int
) -> list[str]:
    return [
        "All dollar figures are REAL (today's dollars): the nominal path is deflated "
        f"by {inflation:.1%} annual inflation.",
        f"Investments compound at {annual_return:.1%} expected annual return with "
        f"{return_volatility:.1%} volatility, drawn from a normal distribution "
        f"({simulations} simulations); returns are independent year to year, which "
        "understates real-world crash clustering.",
        "New savings accumulate as CASH and earn no return — if the household invests "
        "them instead, the real outcome is higher than shown.",
        "Property tracks inflation exactly, so it is flat in real terms; no "
        "appreciation, depreciation, transaction costs, or maintenance are modeled.",
        "Existing debt balances are held flat in nominal dollars — payoff schedules "
        "are unknown to the model, so paydown is NOT credited.",
        "The monthly savings rate is assumed constant for the whole horizon: no "
        "raises, job changes, retirement, kids, or inheritance.",
        "Taxes, employer matches, Social Security, and healthcare shocks are not modeled.",
    ]


def project_wealth(
    session: Session,
    years: int,
    *,
    monthly_savings_delta: float = 0.0,
    annual_return: float = 0.06,
    return_volatility: float = 0.12,
    inflation: float = 0.03,
    seed: int | None = None,
    simulations: int = 500,
) -> dict:
    """Project net worth ``years`` out: a deterministic path plus p10/p50/p90 bands.

    ``monthly_savings_delta`` shifts the observed savings rate (positive = saving
    more) so "what if we put another $500/month away" is one call.
    """
    years = max(1, min(int(years), MAX_YEARS))
    simulations = max(1, min(int(simulations), MAX_SIMULATIONS))
    resolved_seed = _resolve_seed(seed)

    baseline = derive_baseline(session)
    buckets = baseline["balances"]
    monthly_savings = _r(baseline["monthly_savings"] + monthly_savings_delta)
    scenario = _Scenario(
        liquid=buckets["liquid"],
        investments=buckets["investments"],
        property_value=buckets["property"],
        debt=buckets["debt"],
        other=buckets["other"],
        monthly_savings=monthly_savings,
    )

    deterministic = _walk(scenario, [annual_return] * years, inflation)
    bands = _bands(
        scenario,
        years=years,
        annual_return=annual_return,
        return_volatility=return_volatility,
        inflation=inflation,
        simulations=simulations,
        rng=random.Random(resolved_seed),
    )

    caveats = list(baseline["caveats"])
    cash_out = _cash_out_year(deterministic)
    if monthly_savings < 0:
        caveats.append(
            f"The observed monthly net is NEGATIVE (${monthly_savings:,.2f}/month): "
            "this projection draws the household down, it does not build wealth."
        )
    if cash_out is not None:
        caveats.append(
            f"Liquid savings go negative in year {cash_out} — the model keeps "
            "spending past zero rather than stopping, so read past that year as "
            "'this does not work', not as a real balance."
        )
    if baseline["data_thin"]:
        caveats.append(
            "Baseline is thin (see months_observed) — the width of the p10/p90 band "
            "reflects market risk only, NOT the uncertainty in the savings rate "
            "itself, which is larger here."
        )

    final_band = bands[-1]
    return {
        "as_of": baseline["as_of"],
        "years": years,
        "seed": resolved_seed,
        "simulations": simulations,
        "months_observed": baseline["months_observed"],
        "data_thin": baseline["data_thin"],
        "confidence": baseline["confidence"],
        "monthly_savings_used": monthly_savings,
        "monthly_savings_observed": baseline["monthly_savings"],
        "monthly_savings_delta": _r(monthly_savings_delta),
        "starting": {
            "liquid": buckets["liquid"],
            "investments": buckets["investments"],
            "property": buckets["property"],
            "debt": buckets["debt"],
            "other": buckets["other"],
            "net_worth": buckets["net_worth"],
        },
        "deterministic": deterministic,
        "bands": bands,
        "summary": {
            "starting_net_worth": buckets["net_worth"],
            "median_net_worth": final_band["p50"],
            "downside_net_worth": final_band["p10"],
            "upside_net_worth": final_band["p90"],
            "median_gain": _r(final_band["p50"] - buckets["net_worth"]),
            "cash_runs_out_year": cash_out,
        },
        "caveats": caveats,
        "assumptions": _projection_assumptions(
            annual_return=annual_return,
            return_volatility=return_volatility,
            inflation=inflation,
            simulations=simulations,
        )
        + [
            "Savings rate is the observed median monthly net"
            + (
                f" adjusted by ${monthly_savings_delta:,.2f}/month."
                if monthly_savings_delta
                else " (unadjusted)."
            ),
        ],
    }


# --------------------------------------------------------------------------- #
# Affordability
# --------------------------------------------------------------------------- #


def mortgage_payment(principal: float, annual_rate: float, term_years: int) -> float:
    """Level monthly payment for a fully amortizing loan (principal + interest only).

    Standard annuity formula ``P * r / (1 - (1+r)^-n)`` with ``r`` the monthly
    rate; a 0% rate degrades to straight-line principal.
    """
    months = int(term_years) * 12
    if months <= 0 or principal <= 0:
        return 0.0
    monthly_rate = annual_rate / 12.0
    if monthly_rate == 0.0:
        return principal / months
    factor = (1.0 + monthly_rate) ** -months
    return principal * monthly_rate / (1.0 - factor)


def affordability(
    session: Session,
    *,
    purchase_price: float,
    down_payment: float,
    annual_rate: float,
    term_years: int,
    extra_monthly_costs: float = 0.0,
    years: int = 10,
    annual_return: float = 0.06,
    return_volatility: float = 0.12,
    inflation: float = 0.03,
    seed: int | None = None,
    simulations: int = 500,
) -> dict:
    """Can this household buy this thing? Payment math, cash impact, and both futures.

    The buy and no-buy scenarios are simulated against the SAME sampled market
    returns (paired draws from one seed), so the difference between them is the
    purchase and nothing else.
    """
    years = max(1, min(int(years), MAX_YEARS))
    simulations = max(1, min(int(simulations), MAX_SIMULATIONS))
    resolved_seed = _resolve_seed(seed)

    baseline = derive_baseline(session)
    buckets = baseline["balances"]

    loan_principal = _r(max(0.0, purchase_price - down_payment))
    payment = _r(mortgage_payment(loan_principal, annual_rate, term_years))
    monthly_cost = _r(payment + extra_monthly_costs)
    down_pct = _r(down_payment / purchase_price * 100.0) if purchase_price else 0.0

    liquid_after = _r(buckets["liquid"] - down_payment)
    monthly_spend = baseline["monthly_spend"]
    reserve_months = (
        _r(liquid_after / monthly_spend) if monthly_spend > 0 and liquid_after > 0 else 0.0
    )
    savings_after = _r(baseline["monthly_savings"] - monthly_cost)

    loan = _Loan(
        principal=loan_principal,
        monthly_rate=annual_rate / 12.0,
        monthly_payment=payment,
        term_months=int(term_years) * 12,
    )
    without = _Scenario(
        liquid=buckets["liquid"],
        investments=buckets["investments"],
        property_value=buckets["property"],
        debt=buckets["debt"],
        other=buckets["other"],
        monthly_savings=baseline["monthly_savings"],
    )
    with_purchase = _Scenario(
        liquid=liquid_after,
        investments=buckets["investments"],
        property_value=_r(buckets["property"] + purchase_price),
        debt=buckets["debt"],
        other=buckets["other"],
        monthly_savings=savings_after,
        loan=loan,
    )

    band_kwargs = {
        "years": years,
        "annual_return": annual_return,
        "return_volatility": return_volatility,
        "inflation": inflation,
        "simulations": simulations,
    }
    bands_without = _bands(without, rng=random.Random(resolved_seed), **band_kwargs)
    bands_with = _bands(with_purchase, rng=random.Random(resolved_seed), **band_kwargs)
    path_with = _walk(with_purchase, [annual_return] * years, inflation)

    final_without = bands_without[-1]
    final_with = bands_with[-1]
    delta_p50 = _r(final_with["p50"] - final_without["p50"])
    cash_out = _cash_out_year(path_with)
    total_interest = _r(payment * loan.term_months - loan_principal)

    signals = {
        "down_payment_exceeds_liquid": liquid_after < 0,
        "monthly_surplus_goes_negative": savings_after < 0,
        "reserve_months_after_purchase": reserve_months,
        "thin_reserve": bool(monthly_spend > 0 and reserve_months < 3.0),
        "cash_runs_out_year": cash_out,
        "net_worth_delta_p50": delta_p50,
    }

    confidence_line = _confidence_sentence(baseline)
    if liquid_after < 0:
        verdict = (
            f"No — not as structured. The ${down_payment:,.0f} down payment is more "
            f"than the ${buckets['liquid']:,.0f} of liquid savings on record, so the "
            f"purchase cannot be funded from cash today. {confidence_line}"
        )
    elif savings_after < 0:
        verdict = (
            f"Stretch. ${monthly_cost:,.0f}/month of housing cost turns the "
            f"household's ${baseline['monthly_savings']:,.0f}/month surplus into a "
            f"${abs(savings_after):,.0f}/month deficit"
            + (
                f", and liquid savings run out around year {cash_out}. "
                if cash_out is not None
                else ", which draws down savings every month. "
            )
            + f"{confidence_line}"
        )
    elif signals["thin_reserve"]:
        verdict = (
            f"Possible but thin. The payment fits — ${monthly_cost:,.0f}/month against "
            f"a ${baseline['monthly_savings']:,.0f}/month surplus — but the down "
            f"payment leaves only {reserve_months:.1f} months of expenses in cash, "
            f"under the 3-6 month cushion usually held. {confidence_line}"
        )
    elif delta_p50 >= 0:
        verdict = (
            f"Affordable on these numbers. ${monthly_cost:,.0f}/month leaves a "
            f"${savings_after:,.0f}/month surplus and {reserve_months:.1f} months of "
            f"cash reserve, and median net worth in {years} years is about "
            f"${delta_p50:,.0f} HIGHER with the purchase than without. {confidence_line}"
        )
    else:
        verdict = (
            f"Affordable but costly. The household can carry ${monthly_cost:,.0f}/month "
            f"and keeps {reserve_months:.1f} months of reserve, but median net worth in "
            f"{years} years comes out about ${abs(delta_p50):,.0f} LOWER than not "
            f"buying, mostly interest and forgone savings. {confidence_line}"
        )

    caveats = list(baseline["caveats"])
    caveats.append(
        "Closing costs, property tax, insurance, HOA, and maintenance are NOT in the "
        "payment — pass them via extra_monthly_costs or the picture is too rosy."
    )
    caveats.append(
        "The purchased property is assumed to hold its value in real terms; a "
        "different appreciation view changes the comparison materially."
    )
    if cash_out is not None:
        caveats.append(
            f"With the purchase, liquid savings go negative in year {cash_out}."
        )

    return {
        "as_of": baseline["as_of"],
        "years": years,
        "seed": resolved_seed,
        "simulations": simulations,
        "confidence": baseline["confidence"],
        "months_observed": baseline["months_observed"],
        "data_thin": baseline["data_thin"],
        "purchase": {
            "purchase_price": _r(purchase_price),
            "down_payment": _r(down_payment),
            "down_payment_pct": down_pct,
            "loan_principal": loan_principal,
            "annual_rate_pct": _r(annual_rate * 100.0),
            "term_years": int(term_years),
            "monthly_principal_interest": payment,
            "extra_monthly_costs": _r(extra_monthly_costs),
            "total_monthly_cost": monthly_cost,
            "total_interest_over_term": total_interest,
        },
        "cash_impact": {
            "liquid_before": buckets["liquid"],
            "liquid_after": liquid_after,
            "monthly_spend_baseline": monthly_spend,
            "reserve_months_after": reserve_months,
            "monthly_surplus_before": baseline["monthly_savings"],
            "monthly_surplus_after": savings_after,
        },
        "projection": {
            "without_purchase": {
                "p10": final_without["p10"],
                "p50": final_without["p50"],
                "p90": final_without["p90"],
            },
            "with_purchase": {
                "p10": final_with["p10"],
                "p50": final_with["p50"],
                "p90": final_with["p90"],
            },
            "delta_p50": delta_p50,
            "bands_without": bands_without,
            "bands_with": bands_with,
        },
        "signals": signals,
        "verdict": verdict,
        "caveats": caveats,
        "assumptions": _projection_assumptions(
            annual_return=annual_return,
            return_volatility=return_volatility,
            inflation=inflation,
            simulations=simulations,
        )
        + [
            "Monthly payment is principal + interest on a fully amortizing fixed-rate "
            f"loan of ${loan_principal:,.2f} at {annual_rate:.3%} over {int(term_years)} "
            "years; no PMI, points, or rate changes.",
            "The new loan IS amortized (its balance falls on schedule); the full "
            "payment is deducted from monthly savings, so only interest and extra "
            "costs erode net worth.",
            "The down payment comes entirely out of liquid savings on the day of purchase.",
            "Both futures are simulated against identical sampled market returns, so "
            "the difference between them isolates the purchase.",
            "Purchase price is added to property value at cost — no instant equity "
            "and no transaction costs.",
        ],
    }


def _confidence_sentence(baseline: dict) -> str:
    months = baseline["months_observed"]
    if months == 0:
        return (
            "Confidence: NONE — there is no complete month of transaction history "
            "behind this, so the surplus figure is a placeholder, not an observation. "
            "Treat this as arithmetic, not a recommendation."
        )
    if months < MIN_CONFIDENT_MONTHS:
        return (
            f"Confidence: LOW — only {months} complete month(s) of transaction history "
            "stand behind the savings rate; one unusual month could flip this answer."
        )
    if months < THIN_DATA_MONTHS:
        return (
            f"Confidence: MODERATE — {months} complete months of history; enough for a "
            "direction, not enough to bet the decision on."
        )
    return (
        f"Confidence: reasonable — {months} complete months of history behind the "
        "savings rate, though the market path remains the dominant unknown."
    )
