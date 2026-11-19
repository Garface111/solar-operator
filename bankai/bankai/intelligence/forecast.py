"""Forward-looking intelligence: cash-flow projection and spending anomalies.

The forecast replays every detected recurring series (paychecks in, bills out)
across the horizon on its own cadence, walking the household's liquid balance
day by day — so "will we dip low before the next paycheck" has a real answer.
Anomalies compare the current month's category spend against its trailing
average and surface large first-time merchants.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, Transaction
from .recurring import detect_recurring

CADENCE_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30, "quarterly": 91, "yearly": 365}

LIQUID_KINDS = ("checking", "savings")


def cash_flow_forecast(session: Session, days: int = 60) -> dict:
    today = date.today()
    horizon = today + timedelta(days=days)
    liquid = sum(
        a.balance or 0.0
        for a in session.execute(select(Account)).scalars()
        if a.kind in LIQUID_KINDS
    )
    events: list[dict] = []
    for s in detect_recurring(session):
        step = CADENCE_DAYS.get(s.cadence)
        if not step:
            continue
        d = s.next_date
        # A series whose predicted date already passed still projects forward.
        while d < today:
            d += timedelta(days=step)
        while d <= horizon:
            events.append(
                {"date": d, "merchant": s.merchant, "amount": s.avg_amount,
                 "is_bill": s.is_bill, "cadence": s.cadence}
            )
            d += timedelta(days=step)
    events.sort(key=lambda e: e["date"])
    running = liquid
    lowest = {"date": today.isoformat(), "balance": round(running, 2)}
    for e in events:
        running += e["amount"]
        e["projected_balance"] = round(running, 2)
        e["date"] = e["date"].isoformat()
        if e["projected_balance"] < lowest["balance"]:
            lowest = {"date": e["date"], "balance": e["projected_balance"]}
    return {
        "as_of": today.isoformat(),
        "horizon_days": days,
        "starting_liquid": round(liquid, 2),
        "liquid_kinds": list(LIQUID_KINDS),
        "projected_end_balance": round(running, 2),
        "lowest_point": lowest,
        "events": events,
        "note": (
            "Projection replays detected recurring income/bills only — one-off "
            "spending is NOT included, so treat the balances as an upper bound."
        ),
    }


def spending_anomalies(
    session: Session, *, ratio: float = 1.5, min_delta: float = 100.0,
    new_merchant_min: float = 200.0,
) -> dict:
    today = date.today()
    this_month_start = today.replace(day=1)
    # three full prior calendar months
    prior_start = (this_month_start - timedelta(days=1)).replace(day=1)
    for _ in range(2):
        prior_start = (prior_start - timedelta(days=1)).replace(day=1)

    txns = session.execute(
        select(Transaction).where(Transaction.posted >= prior_start)
    ).scalars().all()

    def month_of(d: date) -> str:
        return d.strftime("%Y-%m")

    current_key = month_of(today)
    by_cat_month: dict[tuple[str, str], float] = {}
    for t in txns:
        if t.amount >= 0:
            continue
        key = (t.category or "uncategorized", month_of(t.posted))
        by_cat_month[key] = by_cat_month.get(key, 0.0) + abs(t.amount)

    prior_months = sorted({m for (_, m) in by_cat_month if m != current_key})
    categories: list[dict] = []
    for cat in {c for (c, _) in by_cat_month}:
        current = by_cat_month.get((cat, current_key), 0.0)
        priors = [by_cat_month.get((cat, m), 0.0) for m in prior_months]
        baseline = sum(priors) / len(priors) if priors else 0.0
        if current > baseline * ratio and current - baseline >= min_delta:
            categories.append({
                "category": cat,
                "this_month": round(current, 2),
                "monthly_baseline": round(baseline, 2),
                "over_by": round(current - baseline, 2),
            })
    categories.sort(key=lambda c: -c["over_by"])

    seen_before = {
        t.normalized_desc for t in txns if t.posted < this_month_start
    }
    new_merchants = [
        {"posted": t.posted.isoformat(), "description": t.description,
         "amount": t.amount, "category": t.category}
        for t in txns
        if t.posted >= this_month_start
        and t.amount <= -new_merchant_min
        and t.normalized_desc not in seen_before
    ]
    new_merchants.sort(key=lambda m: m["amount"])
    return {
        "month": current_key,
        "baseline_months": prior_months,
        "category_spikes": categories,
        "large_new_merchants": new_merchants[:15],
        "note": (
            f"Spikes = category spend over {ratio}x its trailing average and at "
            f"least ${min_delta:.0f} above it. Partial current month can understate."
        ),
    }
