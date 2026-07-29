"""Tool definitions + executors for the finance chat agent.

Every tool is read-only against the local database except create_rule/delete_rule,
which manage reminders only. Nothing here can touch a bank.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..intelligence.insights import (
    month_bounds,
    net_worth,
    net_worth_history,
    spending_summary,
    upcoming_bills,
)
from ..intelligence.recurring import detect_recurring
from ..models import Account, Rule, Transaction
from ..rules.engine import RULE_KINDS

TOOLS: list[dict] = [
    {
        "name": "get_accounts",
        "description": "List all linked accounts with balances, owner labels, and last-updated times. Call this to see what accounts exist and current balances / net worth components.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "search_transactions",
        "description": "Search transactions. Call this whenever the user asks about specific spending, merchants, or activity. Amounts are negative for money out, positive for money in.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring match on description (case-insensitive)"},
                "account_id": {"type": "string"},
                "category": {"type": "string"},
                "since": {"type": "string", "description": "ISO date, inclusive"},
                "until": {"type": "string", "description": "ISO date, exclusive"},
                "min_abs_amount": {"type": "number", "description": "Only transactions with |amount| >= this"},
                "limit": {"type": "integer", "description": "Max rows, default 50, max 200"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "spending_summary",
        "description": "Income/spend/net and per-category totals for a period. Use month='YYYY-MM' for a calendar month, or since/until ISO dates. Call this for 'how much did we spend' style questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "YYYY-MM"},
                "since": {"type": "string"},
                "until": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "recurring_and_bills",
        "description": "Auto-detected recurring transactions (bills, subscriptions, paychecks) with predicted next dates, plus bills expected in the next N days. Call this for 'when is X due' or 'what subscriptions do we have'.",
        "input_schema": {
            "type": "object",
            "properties": {"days_ahead": {"type": "integer", "description": "Horizon for upcoming bills, default 30"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "net_worth_history",
        "description": "Daily total-balance history (sum across accounts) for trend questions.",
        "input_schema": {
            "type": "object",
            "properties": {"months": {"type": "integer", "description": "Lookback, default 6"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_rules",
        "description": "List configured reminders / alert rules and their recent firing state.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "create_rule",
        "description": (
            "Create a reminder or alert rule when the user asks to be reminded or notified. Kinds: "
            "reminder (params: day_of_month OR weekday 0=Mon), "
            "balance_below (params: threshold, optional account_id), "
            "large_transaction (params: threshold), "
            "bill_reminder (params: days_before), "
            "weekly_digest (params: weekday 0=Mon). "
            "Always confirm the schedule/threshold you chose in your reply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": RULE_KINDS},
                "params": {"type": "object"},
                "message": {"type": "string", "description": "Text included in the notification email"},
            },
            "required": ["name", "kind"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_rule",
        "description": "Disable a rule by id (from list_rules). Use when the user asks to stop a reminder/alert.",
        "input_schema": {
            "type": "object",
            "properties": {"rule_id": {"type": "string"}},
            "required": ["rule_id"],
            "additionalProperties": False,
        },
    },
]


def execute_tool(session: Session, name: str, tool_input: dict) -> str:
    try:
        result = _dispatch(session, name, tool_input or {})
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _dispatch(session: Session, name: str, args: dict):
    if name == "get_accounts":
        return net_worth(session)
    if name == "search_transactions":
        return _search_transactions(session, args)
    if name == "spending_summary":
        if args.get("month"):
            since, until = month_bounds(args["month"])
        else:
            until = date.fromisoformat(args["until"]) if args.get("until") else date.today() + timedelta(days=1)
            since = date.fromisoformat(args["since"]) if args.get("since") else until - timedelta(days=30)
        return spending_summary(session, since, until)
    if name == "recurring_and_bills":
        days = int(args.get("days_ahead") or 30)
        return {
            "recurring": [asdict(s) for s in detect_recurring(session)],
            "upcoming_bills": upcoming_bills(session, days=days),
        }
    if name == "net_worth_history":
        return net_worth_history(session, months=int(args.get("months") or 6))
    if name == "list_rules":
        rules = session.execute(select(Rule)).scalars().all()
        return [
            {
                "rule_id": r.id,
                "name": r.name,
                "kind": r.kind,
                "params": r.params,
                "enabled": r.enabled,
                "created_by": r.created_by,
            }
            for r in rules
        ]
    if name == "create_rule":
        rule = Rule(
            name=args["name"],
            kind=args["kind"],
            params=args.get("params") or {},
            message=args.get("message") or "",
            created_by="agent",
        )
        session.add(rule)
        session.flush()
        return {"created": True, "rule_id": rule.id, "name": rule.name, "kind": rule.kind}
    if name == "delete_rule":
        rule = session.get(Rule, args["rule_id"])
        if not rule:
            return {"error": "rule not found"}
        rule.enabled = False
        return {"disabled": True, "rule_id": rule.id, "name": rule.name}
    raise ValueError(f"unknown tool {name}")


def _search_transactions(session: Session, args: dict) -> list[dict]:
    query = select(Transaction).order_by(Transaction.posted.desc())
    if args.get("account_id"):
        query = query.where(Transaction.account_id == args["account_id"])
    if args.get("category"):
        query = query.where(Transaction.category == args["category"])
    if args.get("since"):
        query = query.where(Transaction.posted >= date.fromisoformat(args["since"]))
    if args.get("until"):
        query = query.where(Transaction.posted < date.fromisoformat(args["until"]))
    if args.get("query"):
        query = query.where(Transaction.description.ilike(f"%{args['query']}%"))
    limit = min(int(args.get("limit") or 50), 200)
    rows = session.execute(query.limit(500)).scalars().all()
    min_abs = args.get("min_abs_amount")
    if min_abs is not None:
        rows = [t for t in rows if abs(t.amount) >= float(min_abs)]
    account_names = {a.id: a.name for a in session.execute(select(Account)).scalars()}
    return [
        {
            "posted": t.posted.isoformat(),
            "amount": t.amount,
            "description": t.description,
            "category": t.category,
            "account": account_names.get(t.account_id, t.account_id),
            "pending": t.pending,
        }
        for t in rows[:limit]
    ]
