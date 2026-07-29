"""Tool definitions + executors for the finance chat agent.

Every tool is read-only against the local database except create_rule/delete_rule,
which manage reminders only. Nothing here can touch a bank.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..connectors import email_harvest, sheets
from ..intelligence.forecast import cash_flow_forecast, spending_anomalies
from ..intelligence.horizon import affordability, project_wealth
from ..intelligence.insights import (
    month_bounds,
    net_worth,
    net_worth_history,
    spending_summary,
    upcoming_bills,
)
from ..intelligence.recurring import detect_recurring
from ..ingest import _snapshot_balance, normalize_manual_balance
from ..models import (
    Account,
    AgentAction,
    Document,
    MemoryNote,
    Property,
    Rule,
    Transaction,
    Valuation,
)
from .. import goals as goals_lib, realestate, skills_lib, vault, watchpoints
from ..rules.engine import RULE_KINDS
from ..watchpoints import WATCHPOINT_KINDS

READ_PAGE_CHARS = 30_000

# Fixed so the copilot's projections don't wobble between identical questions.
# The seed is echoed in every result, so any run is reproducible.
HORIZON_SEED = 20260101

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
        "name": "cash_flow_forecast",
        "description": (
            "Project the household's liquid balance forward by replaying every "
            "detected recurring paycheck and bill on its own schedule. Shows each "
            "upcoming event, the running balance, and the lowest point. Call this for "
            "'can we afford', 'will we be tight', or any forward-looking cash question. "
            "One-off spending is not included — say so when you cite it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Horizon, default 60, max 180"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "spending_anomalies",
        "description": (
            "Compare this month's spending per category against its trailing 3-month "
            "average and list large first-time merchants. Call this for 'anything "
            "unusual', monthly reviews, or when spend looks off."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "project_wealth",
        "description": (
            "Project the household's net worth years or decades out, in today's "
            "dollars: a deterministic median path plus p10/p50/p90 Monte Carlo bands. "
            "Built from their OBSERVED median monthly savings and current account "
            "split — call this for retirement, 'are we on track', 'what if we saved "
            "another $500/month' (monthly_savings_delta), or any multi-year wealth "
            "question. cash_flow_forecast is the tool for weeks and months; this one "
            "is for years. Always report the p10/p90 range, not just the median, and "
            "repeat the confidence/data_thin flags the result carries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "years": {"type": "integer", "description": "Horizon, default 10, max 40"},
                "monthly_savings_delta": {
                    "type": "number",
                    "description": "Change to the observed monthly savings rate; positive = saving more. Default 0.",
                },
                "annual_return": {
                    "type": "number",
                    "description": "Expected real-ish annual investment return as a decimal, default 0.06",
                },
                "return_volatility": {
                    "type": "number",
                    "description": "Annual return standard deviation, default 0.12",
                },
                "inflation": {"type": "number", "description": "Annual inflation, default 0.03"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "affordability_check",
        "description": (
            "Test a specific large purchase (usually a house): monthly principal and "
            "interest, total interest over the term, what the down payment does to "
            "liquid savings and months of reserve, and projected p10/p50/p90 net worth "
            "WITH versus WITHOUT the purchase on identical simulated markets. Returns a "
            "plain-language 'verdict' with explicit confidence framing. Call this "
            "whenever a purchase with a loan is being weighed. Pass extra_monthly_costs "
            "for property tax, insurance, HOA, and maintenance — they are NOT in the "
            "mortgage payment, and omitting them makes the answer too optimistic. State "
            "the verdict AND the assumptions you fed it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "purchase_price": {"type": "number"},
                "down_payment": {"type": "number"},
                "annual_rate": {
                    "type": "number",
                    "description": "Mortgage rate as a decimal, e.g. 0.0675 for 6.75%",
                },
                "term_years": {"type": "integer", "description": "Loan term, e.g. 30"},
                "extra_monthly_costs": {
                    "type": "number",
                    "description": "Property tax + insurance + HOA + maintenance per month. Default 0.",
                },
                "years": {"type": "integer", "description": "Comparison horizon, default 10, max 40"},
            },
            "required": ["purchase_price", "down_payment", "annual_rate", "term_years"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_watchpoint",
        "description": (
            "Plant a flag for your future self: something to RECONSIDER later, not a "
            "notification. Give the note you'd want to read months from now — what you "
            "decided, why, and what would change the answer. When it fires you are woken "
            "in this thread with that note and the live numbers, and you reassess. Kinds: "
            "on_date (params: date 'YYYY-MM-DD' — renewals, promo-rate expirations, "
            "'revisit in 6 months' promises), net_worth_below / net_worth_above (params: "
            "threshold), account_balance_below (params: account_id, threshold), "
            "liquid_below (params: threshold — checking + savings combined). Use this "
            "whenever you defer a decision or say you'll come back to something; say in "
            "your reply what you planted and what will wake you."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short, e.g. 'Reconsider the HELOC'"},
                "note": {
                    "type": "string",
                    "description": "What future-you should reconsider and why — the reasoning, not just the fact",
                },
                "kind": {"type": "string", "enum": WATCHPOINT_KINDS},
                "params": {"type": "object", "description": "Condition params for the kind"},
            },
            "required": ["title", "note", "kind", "params"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_watchpoints",
        "description": (
            "Every flag you have planted, with status (armed/fired/cancelled), the note "
            "you wrote, and what each is waiting for. Check before planting a duplicate, "
            "and when the household asks what you are keeping an eye on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["armed", "fired", "cancelled"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_watchpoint",
        "description": (
            "Disarm a watchpoint by id (from list_watchpoints) when the question it was "
            "watching is settled or the household says to drop it. A watchpoint that "
            "already fired cannot be cancelled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"watchpoint_id": {"type": "string"}},
            "required": ["watchpoint_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_skills",
        "description": (
            "List your skills library — durable operating manuals you wrote for "
            "yourself on bill negotiation, US tax levers, insurance claims and "
            "appeals, consumer-protection law (FCRA/FDCPA/EFTA/FCBA), and "
            "subscription cancellation. Returns each skill's name and a WHEN TO "
            "USE line. Check this BEFORE advising on a negotiation, a dispute, a "
            "denied claim, or a tax question — then read the relevant one."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_skill",
        "description": (
            "Read one skill from your library in full — scripts, thresholds, "
            "statutes, deadlines, and letter structures. Read the skill before "
            "you draft a cancellation email, an appeal, a dispute letter, or a "
            "negotiation plan, and follow its procedure rather than improvising."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skill name from list_skills"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_goal",
        "description": (
            "Record a household goal — an emergency fund, a down payment, a debt "
            "payoff, a purchase. Link the account whose balance measures it "
            "(linked_account_id from get_accounts) so progress is read from the "
            "ledger, never from memory. starting_amount defaults to that "
            "account's balance today, so progress counts from now. Create a goal "
            "whenever the household states one out loud; restate the target, "
            "date, and linked account in your reply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "e.g. 'Six-month emergency fund'"},
                "target_amount": {"type": "number", "description": "Amount to save, or debt to pay off"},
                "category": {"type": "string", "enum": ["savings", "debt_payoff", "purchase", "other"]},
                "target_date": {"type": "string", "description": "ISO date, must be in the future"},
                "linked_account_id": {"type": "string", "description": "Account whose balance measures this goal"},
                "starting_amount": {"type": "number", "description": "Override the baseline; defaults to today's balance"},
                "note": {"type": "string"},
            },
            "required": ["name", "target_amount"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_goals",
        "description": (
            "Every household goal with computed progress: current amount, percent "
            "complete, required monthly pace to hit the target date, observed "
            "pace from balance history, and on_track. on_track is null when it "
            "cannot be determined honestly — say so rather than guessing. Each "
            "goal carries a 'basis' string explaining exactly how it was "
            "computed; cite it when you report progress. Call this in monthly "
            "reviews and whenever money decisions touch a stated goal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "achieved", "abandoned", "all"],
                    "description": "default active",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "update_goal_status",
        "description": (
            "Mark a goal achieved or abandoned (or reactivate it). Mark achieved "
            "when list_goals shows the target reached — and tell the household."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string"},
                "status": {"type": "string", "enum": ["active", "achieved", "abandoned"]},
            },
            "required": ["goal_id", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_email",
        "description": (
            "Search the household inbox (metadata only: sender, subject, date, "
            "attachment names). Gmail query syntax, e.g. "
            "'from:lawyer has:attachment trust'. Use this to LOCATE documents — "
            "deeds, trust papers, statements — before asking the humans for them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "default 20"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "harvest_email_documents",
        "description": (
            "Sweep the inbox and file matching attachments (PDF/Word) straight into "
            "the document vault with provenance. Without a query it runs the standing "
            "financial/legal-document sweep (trust, deed, insurance, tax, statements). "
            "Duplicates are skipped automatically. After a harvest, read and annotate "
            "what arrived."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Optional Gmail query override"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "subscription_audit",
        "description": (
            "Every detected recurring outflow with its annualized cost, flagging "
            "likely subscriptions (streaming, memberships, software). Use it for cost "
            "reviews — and periodically pick ONE and check in with the household: "
            "'still using this?'. If they say no, draft the cancellation with "
            "propose_action."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "propose_action",
        "description": (
            "Propose a real-world action for household approval — today's kind: "
            "email_support (a cancellation/inquiry email to a company, sent from the "
            "household's own address). Propose only AFTER the household agreed in "
            "conversation; it still executes ONLY when a human clicks Approve & run "
            "in the dashboard. Write the email ready-to-send: firm, brief, includes "
            "account identifiers you know, requests written confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "e.g. 'Cancel PlayStation Plus'"},
                "rationale": {"type": "string", "description": "Why — cite the conversation/evidence"},
                "to_email": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["title", "rationale", "to_email", "subject", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_planning_sheet",
        "description": (
            "Read the household's own Google Sheets planning model — a daily "
            "cash-flow ledger with each spouse's running balance, money in and "
            "out, cash on hand, stocks, and credit-card projections. This is the "
            "model THEY plan against, so read it before advising on cash timing, "
            "and treat it as their intent rather than something to overwrite."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Rows either side of today, default 45"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "reconcile_planning_sheet",
        "description": (
            "Compare the planning sheet's cash figure against the real account "
            "balances and report the gap. Use it when they ask whether the sheet "
            "is right, or when your numbers disagree with theirs — name the "
            "difference and the date, and ask which is correct."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "email_household",
        "description": (
            "Start an email thread with BOTH spouses — use it when something is "
            "worth their attention away from the dashboard, or when you need "
            "something only they can provide (a document, a decision, a "
            "confirmation). Both are always addressed, so it is one conversation "
            "rather than a message to whoever you happened to write to; their "
            "replies come back to you and continue this same thread. Write it as "
            "a person would: say what you know, what you need, and why it matters "
            "to them — not a form letter. This reaches only the household; "
            "anything addressed to an outside company goes through propose_action."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain text. No markdown — this is email."},
            },
            "required": ["subject", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "publish_actuals_to_sheet",
        "description": (
            "Write the household's real current figures — cash, investments, cards, "
            "property, debts, net worth, and every account balance — into the "
            "'BankAI Actuals' tab of their planning spreadsheet, so their own "
            "formulas can reference live numbers instead of hand-typed ones. Safe "
            "to run repeatedly; it rewrites the same block. It never writes into "
            "their planning columns, so their formulas are never overwritten. Do "
            "this after a sync, when they ask you to update the sheet, or when "
            "reconciliation shows the sheet has drifted."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "update_account_balance",
        "description": (
            "Correct the balance of a MANUALLY tracked account — the mortgage, a "
            "loan, a vehicle, anything no bank feed reports. Use it the moment you "
            "learn a better number (a mortgage statement gives the real payoff "
            "balance; a payment reduces principal), instead of caveating a stale "
            "figure forever. Enter liabilities as the amount owed and it is stored "
            "as a negative. This moves net worth, so it is recorded with your "
            "reasoning and snapshotted into history — always restate the old value, "
            "the new value, and your source. Bank-synced accounts cannot be edited "
            "here: their balance comes from the institution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "From get_accounts"},
                "balance": {"type": "number", "description": "Liabilities: the amount owed"},
                "reasoning": {"type": "string", "description": "Where this number came from"},
            },
            "required": ["account_id", "balance", "reasoning"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_code_change",
        "description": (
            "Propose a change to your OWN source code — a new tool, a better "
            "calculation, a fix for something you noticed while working. You cannot "
            "edit yourself directly and should not want to: a bad edit to a system "
            "holding this household's finances is worse than a missing feature. "
            "Write the proposal so a developer can act on it without rediscovering "
            "anything: which file, what is wrong or missing today, the concrete "
            "change, and how it should be tested. It appears in the dashboard for "
            "review. Use this when you hit your own limits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "e.g. 'Add a per-category budget tool'"},
                "file_path": {"type": "string", "description": "Repo-relative path, e.g. bankai/intelligence/forecast.py"},
                "problem": {"type": "string", "description": "What is wrong or missing today, concretely"},
                "change": {"type": "string", "description": "The proposed change, specific enough to implement"},
                "test_plan": {"type": "string", "description": "How to prove it works and what it must not break"},
            },
            "required": ["title", "problem", "change"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_actions",
        "description": "The action audit trail: everything you have proposed, with status (proposed/executed/declined/failed) and results. Check before proposing duplicates.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
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
    if name == "cash_flow_forecast":
        days = min(int(args.get("days") or 60), 180)
        return cash_flow_forecast(session, days=days)
    if name == "spending_anomalies":
        return spending_anomalies(session)
    if name == "project_wealth":
        return project_wealth(
            session,
            min(int(args.get("years") or 10), 40),
            monthly_savings_delta=float(args.get("monthly_savings_delta") or 0.0),
            annual_return=float(args.get("annual_return") or 0.06),
            return_volatility=float(args.get("return_volatility") or 0.12),
            inflation=float(args.get("inflation") or 0.03),
            seed=HORIZON_SEED,
            simulations=500,
        )
    if name == "affordability_check":
        return affordability(
            session,
            purchase_price=float(args["purchase_price"]),
            down_payment=float(args["down_payment"]),
            annual_rate=float(args["annual_rate"]),
            term_years=int(args["term_years"]),
            extra_monthly_costs=float(args.get("extra_monthly_costs") or 0.0),
            years=min(int(args.get("years") or 10), 40),
            seed=HORIZON_SEED,
            simulations=500,
        )
    if name == "set_watchpoint":
        watchpoint = watchpoints.create_watchpoint(
            session,
            title=args["title"],
            kind=args["kind"],
            note=args.get("note") or "",
            params=args.get("params") or {},
            created_by="agent",
        )
        return {
            "created": True,
            "watchpoint_id": watchpoint.id,
            "title": watchpoint.title,
            "kind": watchpoint.kind,
            "params": watchpoint.params,
            "waits_for": watchpoints.describe_condition(watchpoint, session),
        }
    if name == "list_watchpoints":
        rows = watchpoints.list_watchpoints(session, status=args.get("status"))
        return [
            {
                "watchpoint_id": w.id,
                "title": w.title,
                "note": w.note,
                "kind": w.kind,
                "params": w.params,
                "status": w.status,
                "waits_for": watchpoints.describe_condition(w, session),
                "created_by": w.created_by,
                "created_at": w.created_at.isoformat(),
                "fired_at": w.fired_at.isoformat() if w.fired_at else None,
            }
            for w in rows
        ]
    if name == "cancel_watchpoint":
        watchpoint = watchpoints.cancel_watchpoint(session, args["watchpoint_id"])
        return {"cancelled": True, "watchpoint_id": watchpoint.id, "title": watchpoint.title}
    if name == "list_skills":
        return {
            "skills": skills_lib.list_skills(),
            "note": "read_skill(name) for the full manual before acting on it",
        }
    if name == "read_skill":
        return {"name": args["name"], "text": skills_lib.read_skill(args["name"])}
    if name == "create_goal":
        goal = goals_lib.create_goal(
            session,
            name=args["name"],
            target_amount=float(args["target_amount"]),
            category=args.get("category") or "savings",
            target_date=(
                date.fromisoformat(args["target_date"]) if args.get("target_date") else None
            ),
            linked_account_id=args.get("linked_account_id"),
            starting_amount=args.get("starting_amount"),
            note=args.get("note") or "",
        )
        return {"created": True, **goals_lib.goal_progress(session, goal)}
    if name == "list_goals":
        status = args.get("status") or "active"
        return goals_lib.list_goals_with_progress(
            session, status=None if status == "all" else status
        )
    if name == "update_goal_status":
        goal = goals_lib.update_goal_status(session, args["goal_id"], args["status"])
        return {"updated": True, "goal_id": goal.id, "name": goal.name, "status": goal.status}
    if name == "search_email":
        return email_harvest.search_email(
            args["query"], limit=min(int(args.get("limit") or 20), 50)
        )
    if name == "harvest_email_documents":
        return email_harvest.harvest(session, args.get("query"))
    if name == "subscription_audit":
        multiplier = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "yearly": 1}
        subs_hint = (
            "playstation|netflix|spotify|hulu|disney|hbo|max |youtube|apple|prime|"
            "peacock|paramount|audible|patreon|gym|fitness|club|membership|storage|"
            "icloud|dropbox|adobe|github|domain|vpn|news|times|substack"
        )
        import re as _re
        hint = _re.compile(subs_hint, _re.I)
        rows = []
        for s in detect_recurring(session):
            if not s.is_bill:
                continue
            annual = round(abs(s.avg_amount) * multiplier.get(s.cadence, 12), 2)
            rows.append({
                "merchant": s.merchant,
                "cadence": s.cadence,
                "per_charge": round(abs(s.avg_amount), 2),
                "annualized": annual,
                "last_seen": s.last_date.isoformat(),
                "likely_subscription": bool(hint.search(s.merchant)) or abs(s.avg_amount) <= 50,
            })
        rows.sort(key=lambda r: -r["annualized"])
        return {
            "recurring_outflows": rows,
            "total_annualized": round(sum(r["annualized"] for r in rows), 2),
            "note": "Bank data cannot show USAGE — ask the household before judging a subscription idle.",
        }
    if name == "propose_action":
        action = AgentAction(
            kind="email_support",
            title=args["title"][:200],
            rationale=args["rationale"],
            to_email=args["to_email"][:200],
            subject=args["subject"][:300],
            body=args["body"],
        )
        session.add(action)
        session.flush()
        return {
            "proposed": True, "action_id": action.id, "title": action.title,
            "next_step": "a human must click Approve & run in the dashboard's Copilot actions panel",
        }
    if name == "read_planning_sheet":
        if not sheets.configured():
            return {"error": "SHEETS_ID is not set in .env — no planning sheet linked"}
        return sheets.read_plan(limit_days=min(int(args.get("days") or 45), 120))
    if name == "reconcile_planning_sheet":
        if not sheets.configured():
            return {"error": "SHEETS_ID is not set in .env — no planning sheet linked"}
        return sheets.reconcile(session)
    if name == "email_household":
        from ..messaging import email_thread

        if not email_thread.configured():
            return {"error": "the email channel is not configured"}
        return email_thread.start_thread(session, args["subject"], args["body"])
    if name == "publish_actuals_to_sheet":
        if not sheets.can_write():
            return {
                "error": (
                    "writing to the spreadsheet is not set up yet. The quick route "
                    "is the Apps Script bridge in scripts/sheet_bridge.gs: paste it "
                    "into the sheet's Extensions > Apps Script, deploy it as a web "
                    "app, and put its URL and secret in SHEETS_WEBHOOK_URL / "
                    "SHEETS_WEBHOOK_SECRET. Reading already works."
                )
            }
        return sheets.write_actuals(session)
    if name == "update_account_balance":
        account = session.get(Account, args["account_id"])
        if not account:
            return {"error": "account not found — call get_accounts for ids"}
        if account.source != "manual":
            return {
                "error": (
                    f"'{account.name}' is synced from {account.source}; its balance "
                    "comes from the institution and cannot be set by hand. Only "
                    "manually tracked accounts (mortgage, loans, vehicles, property) "
                    "are editable."
                )
            }
        old = account.balance
        new = normalize_manual_balance(account.kind, float(args["balance"]))
        account.balance = new
        account.balance_date = datetime.utcnow()
        session.flush()
        _snapshot_balance(session, account)
        # Why this number is what it is, upserted per account so the note stays
        # current instead of growing a log nobody reads.
        title = f"Balance basis — {account.name}"[:120]
        note = session.execute(
            select(MemoryNote).where(MemoryNote.title == title)
        ).scalar_one_or_none()
        basis = (
            f"{date.today().isoformat()}: {new:,.2f} (was {old:,.2f} if known). "
            f"{args['reasoning']}"
        )
        if note:
            note.content = basis
        else:
            session.add(MemoryNote(title=title, content=basis))
        session.flush()
        return {
            "updated": True,
            "account": account.name,
            "old_balance": old,
            "new_balance": new,
            "net_worth": net_worth(session)["total"],
        }
    if name == "propose_code_change":
        body = "\n\n".join([
            f"FILE: {args.get('file_path') or '(not specified)'}",
            f"PROBLEM:\n{args['problem']}",
            f"CHANGE:\n{args['change']}",
            f"TEST PLAN:\n{args.get('test_plan') or '(none given)'}",
        ])
        action = AgentAction(
            kind="code_change",
            title=args["title"][:200],
            rationale=args["problem"],
            subject=args.get("file_path", "")[:300],
            body=body,
        )
        session.add(action)
        session.flush()
        return {
            "proposed": True,
            "action_id": action.id,
            "note": (
                "Filed for human review in the dashboard. It will NOT be applied "
                "automatically — say plainly that you have proposed it, not done it."
            ),
        }
    if name == "list_actions":
        actions = session.execute(
            select(AgentAction).order_by(AgentAction.proposed_at.desc()).limit(30)
        ).scalars().all()
        return [
            {"action_id": a.id, "kind": a.kind, "title": a.title, "status": a.status,
             "to_email": a.to_email, "proposed_at": a.proposed_at.isoformat(),
             "result": a.result[:300]}
            for a in actions
        ]
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
        if vault.is_image(doc.filename):
            path = vault.stored_path(doc)
            return {
                "document_id": doc.id,
                "title": doc.title,
                "category": doc.category,
                "kind": "image",
                "image_path": str(path) if path else None,
                "note": (
                    "This is an image (a screenshot or photo), so it has no text to "
                    "return. Open it with your own Read tool at image_path to look at "
                    "it, then annotate_document with what it shows."
                    if path else
                    "This image's original file is missing from disk."
                ),
            }
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
