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
from ..models import Account, Document, MemoryNote, Property, Rule, Transaction, Valuation
from .. import realestate, vault
from ..rules.engine import RULE_KINDS

READ_PAGE_CHARS = 30_000

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
        "name": "list_documents",
        "description": (
            "List every document in the household vault (deeds, mortgage/closing papers, "
            "contracts, insurance policies, estate documents, tax records) with your saved "
            "summaries. Call this whenever a question might touch recorded documents, and "
            "when forming your picture of the household's legal situation."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_document",
        "description": (
            "Read a vault document's full extracted text. Long documents are paged: pass "
            "start_char to continue (each call returns up to 30000 chars and the total "
            "length). Reread source documents rather than relying on your recollection "
            "when details matter (dates, amounts, clauses, names)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "start_char": {"type": "integer", "description": "default 0"},
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_documents",
        "description": "Case-insensitive text search across every vault document; returns matching snippets with document ids. Use to locate a clause, name, address, or amount across the whole vault.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "annotate_document",
        "description": "Save/replace your digest of a vault document (key parties, dates, amounts, obligations, deadlines, anything protective to remember). Do this after reading any new document — annotations show up in list_documents and survive forever.",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["document_id", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_property_valuation",
        "description": (
            "List tracked real-estate properties with their current value (the account "
            "balance in net worth), latest valuation record, comps on file, and a fresh "
            "comps-based estimate. Call this for any question about home value, equity, "
            "or the local market picture."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "add_property_comp",
        "description": (
            "Record a comparable sale/listing near a tracked property — use when the "
            "household mentions one ('the place two doors down sold for 1.5M') or you "
            "learn of one. Comps feed the value estimate, so only record ones you have "
            "a concrete source for, and say where it came from in your reply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "string"},
                "address": {"type": "string"},
                "price": {"type": "number"},
                "status": {"type": "string", "enum": ["sold", "active", "pending"]},
                "sale_date": {"type": "string", "description": "ISO date, if known"},
                "sqft": {"type": "integer"},
                "distance_miles": {"type": "number"},
            },
            "required": ["property_id", "address", "price"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_property_value",
        "description": (
            "Adjust a tracked property's value — this changes the account balance and "
            "therefore net worth, and is recorded with your reasoning. Only do this on "
            "comps/market evidence or an owner's instruction, never a hunch; always "
            "restate the old value, new value, and basis in your reply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "string"},
                "value": {"type": "number"},
                "reasoning": {"type": "string", "description": "Evidence for this value"},
            },
            "required": ["property_id", "value", "reasoning"],
            "additionalProperties": False,
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Save or update a persistent memory note for yourself (upsert by title). "
            "Your notes are always shown in your context, across every conversation and "
            "restart. Use this proactively whenever you learn a durable fact: account "
            "nicknames, preferences, financial goals, standing decisions, corrections. "
            "Keep notes short and current — update rather than duplicate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short stable title, e.g. 'Account nicknames'"},
                "content": {"type": "string"},
            },
            "required": ["title", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_memory",
        "description": "Delete one of your memory notes by title when it is stale or wrong.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
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
    if name == "save_memory":
        note = session.execute(
            select(MemoryNote).where(MemoryNote.title == args["title"])
        ).scalar_one_or_none()
        if note:
            note.content = args["content"]
        else:
            note = MemoryNote(title=args["title"], content=args["content"])
            session.add(note)
        session.flush()
        return {"saved": True, "title": note.title}
    if name == "delete_memory":
        note = session.execute(
            select(MemoryNote).where(MemoryNote.title == args["title"])
        ).scalar_one_or_none()
        if not note:
            return {"error": "no memory note with that title"}
        session.delete(note)
        return {"deleted": True, "title": args["title"]}
    if name == "delete_rule":
        rule = session.get(Rule, args["rule_id"])
        if not rule:
            return {"error": "rule not found"}
        rule.enabled = False
        return {"disabled": True, "rule_id": rule.id, "name": rule.name}
    if name == "get_property_valuation":
        props = session.execute(select(Property)).scalars().all()
        out = []
        for p in props:
            latest = max(p.valuations, key=lambda v: v.created_at, default=None)
            comps = sorted(
                realestate.usable_comps(p),
                key=lambda c: (c.sale_date or date.min),
                reverse=True,
            )
            out.append({
                "property_id": p.id,
                "address": f"{p.street}, {p.city}, {p.state} {p.zip_code}".strip(),
                "specs": {"sqft": p.sqft, "beds": p.beds, "baths": p.baths,
                          "year_built": p.year_built},
                "current_value": p.account.balance,
                "auto_update": p.auto_update,
                "fresh_comps_estimate": realestate.estimate_from_comps(p),
                "latest_valuation": (
                    {"value": latest.value, "method": latest.method,
                     "applied": latest.applied, "at": latest.created_at.isoformat(),
                     "detail": latest.detail}
                    if latest else None
                ),
                "comps": [
                    {"address": c.address, "price": c.price, "status": c.status,
                     "sale_date": c.sale_date.isoformat() if c.sale_date else None,
                     "sqft": c.sqft, "distance_miles": c.distance_miles,
                     "source": c.source}
                    for c in comps[:15]
                ],
            })
        return out
    if name == "add_property_comp":
        prop = session.get(Property, args["property_id"])
        if not prop:
            return {"error": "property not found — call get_property_valuation for ids"}
        sale_date = date.fromisoformat(args["sale_date"]) if args.get("sale_date") else None
        comp, created = realestate.upsert_comp(
            session, prop, source="agent", address=args["address"],
            price=float(args["price"]), status=args.get("status") or "sold",
            sale_date=sale_date, sqft=args.get("sqft"),
            distance_miles=args.get("distance_miles"),
        )
        return {
            "recorded": True, "created": created, "comp_id": comp.id,
            "fresh_estimate": realestate.estimate_from_comps(prop),
        }
    if name == "set_property_value":
        prop = session.get(Property, args["property_id"])
        if not prop:
            return {"error": "property not found — call get_property_valuation for ids"}
        if prop.account.kind != "property":
            return {"error": "linked account is not a property account"}
        old = prop.account.balance
        valuation = Valuation(
            property_id=prop.id, value=float(args["value"]), method="agent",
            detail=args["reasoning"],
        )
        session.add(valuation)
        session.flush()
        realestate.apply_value(session, prop, valuation)
        return {"updated": True, "old_value": old, "new_value": prop.account.balance,
                "reasoning_recorded": True}
    if name == "list_documents":
        docs = session.execute(select(Document).order_by(Document.added_at)).scalars().all()
        return [
            {
                "document_id": d.id,
                "title": d.title,
                "category": d.category,
                "filename": d.filename,
                "size_bytes": d.size_bytes,
                "added_at": d.added_at.isoformat(),
                "total_chars": len(d.content_text),
                "summary": d.summary or "(not yet annotated — read it, then annotate_document)",
            }
            for d in docs
        ]
    if name == "read_document":
        doc = session.get(Document, args["document_id"])
        if not doc:
            return {"error": "document not found — call list_documents for valid ids"}
        text = doc.content_text
        start = max(0, int(args.get("start_char") or 0))
        chunk = text[start:start + READ_PAGE_CHARS]
        result = {
            "document_id": doc.id,
            "title": doc.title,
            "category": doc.category,
            "total_chars": len(text),
            "start_char": start,
            "text": chunk,
        }
        if start + len(chunk) < len(text):
            result["next_start_char"] = start + len(chunk)
        if not text.strip():
            result["note"] = (
                "no extractable text — likely a scanned image; ask the household for a "
                "text-layer copy or the key facts"
            )
        return result
    if name == "search_documents":
        return vault.search_documents(session, args.get("query") or "")
    if name == "annotate_document":
        doc = session.get(Document, args["document_id"])
        if not doc:
            return {"error": "document not found — call list_documents for valid ids"}
        doc.summary = args["summary"]
        session.flush()
        return {"annotated": True, "document_id": doc.id, "title": doc.title}
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
