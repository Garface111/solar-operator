"""Seed the database with realistic demo data so you can try BankAI before linking
real accounts. Safe to re-run (dedupe makes it a no-op). Usage:

    python scripts/seed_demo.py [--reset]
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bankai.db import init_db, session_scope  # noqa: E402
from bankai.ingest import TxnIn, ingest_transactions, upsert_account  # noqa: E402

random.seed(20260729)  # deterministic demo

GROCERS = ["HANNAFORD #8123", "CITY MARKET COOP", "COSTCO WHSE #0315", "TRADER JOE'S #512"]
DINING = ["PENNY CLUSE CAFE", "AMERICAN FLATBREAD", "DOORDASH*THAI HOUSE", "ONYX COFFEE"]
SHOPPING = ["AMAZON.COM*8H2KL99", "TARGET 00028915", "HOME DEPOT #4721"]
GAS = ["IRVING OIL #402", "SHELL 57442199", "CUMBERLAND FARMS 8112"]


def month_iter(months_back: int):
    today = date.today()
    for delta in range(months_back, -1, -1):
        y, m = today.year, today.month - delta
        while m <= 0:
            y, m = y - 1, m + 12
        yield y, m


def seed() -> None:
    with session_scope() as session:
        checking = upsert_account(
            session, source="csv", name="BofA Joint Checking", institution="Bank of America",
            kind="checking", owner="joint", balance=6421.37,
        )
        savings = upsert_account(
            session, source="csv", name="Fidelity Savings", institution="Fidelity",
            kind="savings", owner="joint", balance=24880.10,
        )
        amex = upsert_account(
            session, source="csv", name="Amex Gold", institution="American Express",
            kind="credit", owner="joint", balance=-1243.55,
        )
        apple = upsert_account(
            session, source="csv", name="Apple Card", institution="Goldman Sachs",
            kind="credit", owner="spouse", balance=-389.20,
        )

        chk: list[TxnIn] = []
        card: list[TxnIn] = []
        apple_txns: list[TxnIn] = []
        today = date.today()

        # Biweekly payroll for ~7 months
        payday = today - timedelta(days=200)
        while payday <= today:
            chk.append(TxnIn(payday, 3150.00, "ACME ENGINEERING PAYROLL DIRECT DEP"))
            payday += timedelta(days=14)

        for y, m in month_iter(6):
            first = date(y, m, 1)
            if first > today:
                continue

            def day(d: int) -> date:
                return date(y, m, min(d, 28))

            chk.append(TxnIn(day(1), -2350.00, "GUILD MORTGAGE COMPANY"))
            chk.append(TxnIn(day(12), -142.60 + random.uniform(-15, 15), "GREEN MOUNTAIN POWER BILLPAY"))
            chk.append(TxnIn(day(15), -89.99, "COMCAST XFINITY INTERNET"))
            chk.append(TxnIn(day(20), -212.40, "GEICO AUTO INSURANCE"))
            chk.append(TxnIn(day(25), -800.00, "ONLINE TRANSFER TO FIDELITY SAVINGS"))
            card.append(TxnIn(day(3), -15.49, "NETFLIX.COM"))
            card.append(TxnIn(day(6), -10.99, "SPOTIFY USA"))
            apple_txns.append(TxnIn(day(8), -9.99, "APPLE.COM/BILL ICLOUD"))
            for _ in range(5):
                card.append(TxnIn(day(random.randint(2, 27)),
                                  -random.uniform(35, 160), random.choice(GROCERS)))
            for _ in range(4):
                card.append(TxnIn(day(random.randint(2, 27)),
                                  -random.uniform(12, 85), random.choice(DINING)))
            for _ in range(2):
                card.append(TxnIn(day(random.randint(2, 27)),
                                  -random.uniform(20, 220), random.choice(SHOPPING)))
            for _ in range(3):
                apple_txns.append(TxnIn(day(random.randint(2, 27)),
                                        -random.uniform(25, 60), random.choice(GAS)))

        added = 0
        for account, txns in ((checking, chk), (amex, card), (apple, apple_txns)):
            txns = [t for t in txns if t.posted <= today]
            for t in txns:
                t.amount = round(t.amount, 2)
            added += ingest_transactions(session, account, txns).added
        # a couple of savings deposits so the account isn't empty
        added += ingest_transactions(
            session, savings,
            [TxnIn(today - timedelta(days=d), 800.00, "TRANSFER FROM CHECKING")
             for d in (5, 35, 65)],
        ).added
        print(f"Seeded demo data: {added} transactions across 4 accounts.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete bankai.db first")
    args = parser.parse_args()
    if args.reset:
        db_file = Path(__file__).resolve().parent.parent / "bankai.db"
        db_file.unlink(missing_ok=True)
        print("Removed existing bankai.db")
    init_db()
    seed()
