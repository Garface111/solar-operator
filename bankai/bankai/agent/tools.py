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
from ..ingest import MANUAL_KINDS, _snapshot_balance, normalize_manual_balance, upsert_account
from ..models import (
    Account,
    AgentAction,
    CategoryRule,
    Document,
    MemoryNote,
    Property,
    Rule,
    Transaction,
    Valuation,
)
from .. import accounts_terms, goals as goals_lib, realestate, skills_lib, vault, watchpoints
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
            "to them — not a form letter. WRITE IN MARKDOWN: it is rendered into "
            "a clean, styled HTML email, so use ## section headings, **bold**, "
            "bullet or numbered lists, | pipe tables | for anything tabular (a "
            "payment calendar, a comparison), and colored callout panels for what "
            "must not be missed — a callout is a blockquote whose first line is "
            "[!TIP], [!IMPORTANT], [!WARNING], or [!SUCCESS]. SHOW THE NUMBERS "
            "VISUALLY, leaning on charts as much as prose because a picture of "
            "where the money went lands harder than a sentence. Three fenced "
            "chart blocks render inline: a ```barchart block (lines 'Label | "
            "value', magnitude sizes the bars) for any breakdown; a ```stats "
            "block (lines 'Value | Label') as a row of big-number tiles near the "
            "top; and a ```progress block (lines 'Label | current | target') as a "
            "meter for a goal or debt payoff. This reaches only "
            "the household; anything addressed to an outside company goes through "
            "propose_action."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Markdown — headings, bold, lists, tables, and [!TIP]/[!IMPORTANT] callouts all render."},
            },
            "required": ["subject", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "print_weekly_report",
        "description": (
            "Regenerate the household's weekly report page with FRESH numbers — "
            "net worth and balances, this week vs last week vs the month, expense "
            "breakdown, and the forward projection — and send it to the household "
            "printer (the Epson by the desk). You write the 'From your copilot' "
            "summary that appears on the page. Use when they ask for the report, "
            "ask to reprint it, or when a Saturday print failed and the printer "
            "is back. The PDF is saved either way, so a dead printer is reported, "
            "not fatal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "4-8 plain-text sentences for the printed page: how the "
                        "week went vs last week and the month, what deserves "
                        "attention, and the road ahead. No markdown."
                    ),
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "print_page",
        "description": (
            "Print anything you write on the household printer as a clean titled "
            "page — a shopping list, a draft letter, a plan, numbers they asked "
            "to hold in their hands. Plain text body (no markdown); long text "
            "flows onto more pages. Use when the household asks you to print "
            "something, or when paper genuinely beats a chat message. The PDF is "
            "saved either way, so a dead printer is reported, not fatal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Heading on the page"},
                "body": {"type": "string", "description": "Plain text. No markdown."},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "print_document",
        "description": (
            "Print a document from the vault on the household printer. A stored "
            "PDF prints as the original, page for page; other formats print as "
            "their extracted text. Use when the household asks for a paper copy "
            "of something you hold — a statement, a contract, a policy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "recategorize_transactions",
        "description": (
            "Fix mislabeled transactions — the categorizer's guess is not the "
            "household's truth. Select by description_contains (case-insensitive "
            "substring, min 4 chars) and/or explicit transaction_ids, optionally "
            "narrowed by current_category, and set new_category. Use category "
            "'transfer' for money moving between the household's own accounts "
            "(card payments, trust redemptions) — transfers are excluded from "
            "every spend/income total automatically. With remember=true (the "
            "default when description_contains is given) the correction becomes "
            "a standing rule: future syncs label matching transactions the same "
            "way, so the fix holds beyond this week. Say what you changed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description_contains": {"type": "string"},
                "transaction_ids": {"type": "array", "items": {"type": "string"}},
                "current_category": {"type": "string"},
                "new_category": {"type": "string"},
                "remember": {"type": "boolean", "description": "Persist as a standing rule (needs description_contains)"},
                "reason": {"type": "string", "description": "One line: why this reclassification is right"},
            },
            "required": ["new_category"],
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
        "name": "set_account_terms",
        "description": (
            "Record what a statement says about ANY account — including bank-synced "
            "ones — so its due date and minimum appear next to its balance. Bank "
            "feeds carry balances but not due dates, so without this the biggest "
            "cards show a number with no deadline, which is the half that actually "
            "governs whether a payment gets missed. This never changes a synced "
            "balance; the institution still owns that. Always give as_of (the "
            "statement date) so a figure cannot look current when it is months old."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "From get_accounts"},
                "statement_balance": {"type": "number", "description": "Amount owed per the statement"},
                "minimum_payment": {"type": "number"},
                "due_day_of_month": {
                    "type": "integer",
                    "description": (
                        "PREFER THIS for a card: the day of the month it is always "
                        "due (1-31). A card's cycle does not move, so recorded once "
                        "this stays right forever and the next date is computed. A "
                        "fixed payment_due_date goes out of date every month."
                    ),
                },
                "payment_due_date": {"type": "string", "description": "ISO date, for a one-off"},
                "apr": {"type": "number"},
                "as_of": {"type": "string", "description": "ISO statement date"},
                "source": {"type": "string", "description": "Which statement this came from"},
            },
            "required": ["account_id", "source"],
            "additionalProperties": False,
        },
    },
    {
        "name": "track_account",
        "description": (
            "Add or update an account no bank feed can reach — the Apple Card is "
            "the case this exists for — so it appears in the account list and "
            "counts in net worth like everything else. Give what the statement "
            "says: the FULL amount owed (for Apple Card that is 'Total Balance', "
            "which includes remaining Monthly Installments, not the smaller "
            "'Monthly Balance'), the minimum payment, the due date, and the "
            "statement date it came from. Enter what is owed as a positive number "
            "on a credit account; it is stored as a negative. Calling it again "
            "with the same name updates that account rather than creating a "
            "second one. Use it the moment a statement gives you better numbers — "
            "leaving a card out entirely is worse than showing a dated figure, "
            "because a missing card reads as a card with nothing owed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "e.g. 'Apple Card'"},
                "kind": {"type": "string", "enum": MANUAL_KINDS},
                "balance": {"type": "number", "description": "Amount owed, for a liability"},
                "owner": {"type": "string", "description": "ford | gaurav | joint"},
                "minimum_payment": {"type": "number"},
                "payment_due_date": {"type": "string", "description": "ISO date from the statement"},
                "apr": {"type": "number"},
                "as_of": {"type": "string", "description": "ISO statement date these figures are from"},
                "source": {"type": "string", "description": "Which document/statement this came from"},
            },
            "required": ["name", "kind", "balance", "source"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_expense",
        "description": (
            "Record one cash / off-ledger transaction NO linked account will ever "
            "see — cash out of a wallet, Venmo or Zelle between people, a "
            "reimbursement, selling something, paying the sitter. This is how "
            "money mentioned in conversation becomes a real ledger row: it lands "
            "in the manual 'Cash & untracked' account and counts in spending "
            "summaries, anomalies, and the weekly report. Money SPENT is a "
            "positive amount here and is stored negative; set received=true for "
            "money that came IN. NEVER log a spend that will appear on a linked "
            "card or bank feed — the sync will bring it in and this would count "
            "it twice. A spend on a STATEMENT-FED account (the Apple Card — no "
            "feed, data arrives only when an export is emailed in) is not cash "
            "either: use note_pending_expense for those. Re-logging the same "
            "expense same-day is deduplicated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Dollars, positive (12.50 for $12.50 spent)"},
                "description": {"type": "string", "description": "What it was — 'Dog sitter, weekend' beats 'payment'"},
                "spender": {"type": "string", "description": "Ford | Gaurav | joint — who the money moved for"},
                "date": {"type": "string", "description": "ISO date it happened; omit for today"},
                "category": {"type": "string", "description": "Override the auto-category when you know better"},
                "received": {"type": "boolean", "description": "true = money IN (a reimbursement arriving, cash from a sale)"},
            },
            "required": ["amount", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_subscription",
        "description": (
            "EXECUTE a subscription cancellation — your one standing power to "
            "act on the outside world without the dashboard gate, granted by "
            "Ford on 2026-08-10. Hard boundaries, enforced in code: it runs "
            "ONLY on a household member's explicit instruction (instructed_by "
            "= who told you, in this conversation — your own initiative stays "
            "propose_action until a spouse says yes), and guarded categories "
            "(insurance, health, utilities, phone/internet, debt) are refused "
            "here and must go through the dashboard. What it does: sends a "
            "fixed-template cancellation notice to the merchant's support "
            "address — as the household's own email when possible, so replies "
            "land where you can read them — records the action in the audit "
            "trail, and plants a watchpoint ~35 days out to verify the "
            "charges actually stopped (charged again = tell the household + "
            "draft the FCBA dispute). Before calling: consult the "
            "subscription-cancellation skill for this merchant's route; if "
            "the merchant only cancels by portal, phone, or letter (Planet "
            "Fitness is letter/in-person), do NOT pretend email works — say "
            "so, produce the artifact (print_page a signed-ready letter), and "
            "still plant the verification by asking for a watchpoint. Find "
            "the right support email before calling; a wrong recipient is a "
            "no-op you would falsely trust."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string", "description": "As it appears in transactions, e.g. 'PLANET FITNESS'"},
                "service_name": {"type": "string", "description": "Human name, e.g. 'Planet Fitness membership'"},
                "support_email": {"type": "string", "description": "The merchant's support/cancellation address"},
                "instructed_by": {"type": "string", "description": "Ford | Gaurav — who told you to cancel"},
                "account_name": {"type": "string", "description": "Whose account it is, e.g. 'Ford Genereaux'"},
                "account_identifier": {"type": "string", "description": "Member/account number when known"},
            },
            "required": ["merchant", "service_name", "support_email", "instructed_by", "account_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "open_initiative",
        "description": (
            "Start a standing PROJECT you will carry to completion over days — "
            "this is how you actually finish multi-step work instead of losing "
            "it between turns. Give it a goal (what 'done' looks like), a plan "
            "(your own steps), and the single next concrete action. On your "
            "free cycles you'll advance the highest-priority active one a step "
            "at a time. Use this for the real work you keep noticing you could "
            "own — the debt triage desk, the surrogacy finance file, syncing "
            "the planning sheet — not for one-turn tasks. An initiative "
            "organizes your work; it grants no new power, so every step still "
            "goes through the normal gates (propose side-effects, household-only "
            "email, spouse-instructed cancellations). Lower priority = sooner."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "e.g. 'Debt triage desk'"},
                "goal": {"type": "string", "description": "What done looks like, concretely"},
                "plan": {"type": "string", "description": "Your step list to get there"},
                "next_action": {"type": "string", "description": "The single next concrete step"},
                "priority": {"type": "integer", "description": "Lower = sooner (default 100)"},
            },
            "required": ["title", "goal"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_initiative",
        "description": (
            "Advance one of your projects: append what you just did to its "
            "worklog, set the next action, revise the plan, change priority, or "
            "close it (status 'done' when the goal is met, 'abandoned' if it no "
            "longer matters). If you are stuck waiting on the household for "
            "something — a document, a decision, a number only they have — set "
            "blocked_on to exactly what you need; that moves it to blocked and "
            "surfaces the ask instead of silently stalling. Record progress "
            "every time you work an initiative, even a small step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "initiative_id": {"type": "string", "description": "From list_initiatives"},
                "worklog_entry": {"type": "string", "description": "What you just did"},
                "next_action": {"type": "string"},
                "plan": {"type": "string"},
                "status": {"type": "string", "enum": ["active", "blocked", "done", "abandoned"]},
                "priority": {"type": "integer"},
                "blocked_on": {"type": "string", "description": "What you need from the household to proceed"},
            },
            "required": ["initiative_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_initiatives",
        "description": (
            "Your standing projects with goal, plan, next action, status, and "
            "recent worklog. Check before opening a new one (don't duplicate), "
            "at the start of a free cycle to decide what to advance, and when "
            "asked what you are working on. include_closed=true adds done/"
            "abandoned ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"include_closed": {"type": "boolean"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "generate_report",
        "description": (
            "Produce a titled multi-section PDF and deliver it to the household "
            "— printed on the house printer when reachable, and always emailed "
            "to both spouses. Use it when a question deserves a real document "
            "rather than a chat reply: a debt-paydown plan, a net-worth "
            "one-pager, a surrogacy paid-vs-remaining ledger, a monthly deep "
            "dive. Compose the sections yourself from real figures you looked "
            "up; this only renders and delivers what you write. Goes ONLY to "
            "the household, like every other thing you send."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "e.g. 'Debt paydown plan — Aug 2026'"},
                "sections": {
                    "type": "array",
                    "description": "Ordered sections, each a heading + body",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["heading", "body"],
                        "additionalProperties": False,
                    },
                },
                "print_copy": {"type": "boolean", "description": "Also print it (default true)"},
            },
            "required": ["title", "sections"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_source",
        "description": (
            "Read your OWN source code so you can improve it. Path is repo-"
            "relative and limited to your package and tests (bankai/… or "
            "tests/…); secrets, the database, and the vault are out of scope "
            "by design and refused. Use list_source first to see what exists. "
            "This is how you actually understand yourself before proposing a "
            "change — read the file you mean to edit, don't guess its contents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "e.g. bankai/pending.py"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_source",
        "description": "List your own source files under a package subdir (default 'bankai'). Read-only, in-scope paths only.",
        "input_schema": {
            "type": "object",
            "properties": {"subdir": {"type": "string", "description": "e.g. 'bankai' or 'bankai/connectors' or 'tests'"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_patch",
        "description": (
            "Propose a REAL change to your own code — the full new contents of "
            "each file you want to change, not a description. This is how you "
            "improve yourself: read the relevant source with read_source, write "
            "the corrected/extended files, and submit them with the tests that "
            "prove the change. Your proposal is recorded and diffed for review; "
            "its tests run in an isolated sandbox; and a human ships it. You "
            "cannot deploy yourself — that gate is deliberate and it protects "
            "everyone, so propose freely and let Ford merge. ALWAYS include or "
            "update a test under tests/ that would fail without your change; a "
            "patch without a test proving it is far less likely to be shipped. "
            "Only bankai/… and tests/… paths are accepted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short imperative, e.g. 'Flag duplicate recurring charges'"},
                "rationale": {"type": "string", "description": "What is wrong or missing, and why this change is right"},
                "files": {
                    "type": "object",
                    "description": "Map of repo-relative path -> the FULL new file contents",
                    "additionalProperties": {"type": "string"},
                },
                "test_paths": {"type": "string", "description": "Tests that prove it, e.g. 'tests/test_recurring.py'"},
            },
            "required": ["title", "rationale", "files"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_code_proposals",
        "description": (
            "Your open self-improvement proposals with status (proposed / "
            "awaiting_sandbox / passed / failed) and the tail of any test "
            "output. Check before proposing something you already proposed, and "
            "to see whether a proposal's tests passed in the sandbox. "
            "include_closed=true adds shipped/rejected ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"include_closed": {"type": "boolean"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "record_life_fact",
        "description": (
            "Add one inference to your LIFE MODEL of this household — the "
            "living picture their data has taught you, injected into every "
            "conversation so you never rediscover it. Kinds: 'event' (something "
            "happened — a trip, a big purchase, a new pet), 'rhythm' (a "
            "recurring pattern of how they live), 'prediction' (what the model "
            "says comes next — check these later and confirm or refute "
            "honestly), 'opportunity' (a concrete improvement you could own). "
            "Always attach the evidence (merchants, dates, amounts) and an "
            "honest confidence — a life model built on vibes is worse than "
            "none. One fact per statement; update_life_fact revises or retires."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "statement": {"type": "string", "description": "The inference, one sentence, e.g. 'They built a home gym in late July'"},
                "kind": {"type": "string", "enum": ["event", "rhythm", "prediction", "opportunity"]},
                "evidence": {"type": "string", "description": "The data behind it: merchants, dates, amounts"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["statement", "kind", "evidence"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_life_fact",
        "description": (
            "Revise the life model as reality answers back: raise or lower "
            "confidence, sharpen a statement, add evidence, or close a fact "
            "out — status 'confirmed' (they said so / the data proved it), "
            "'refuted' (you were wrong — keep it, wrongness is information), "
            "'retired' (true then, life moved on). An honest model prunes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact_id": {"type": "string", "description": "From list_life_facts"},
                "statement": {"type": "string"},
                "evidence": {"type": "string"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "status": {"type": "string", "enum": ["active", "confirmed", "retired", "refuted"]},
            },
            "required": ["fact_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_life_facts",
        "description": (
            "The full life model with ids — everything you currently believe "
            "about how this household lives, with evidence and confidence. "
            "include_closed=true adds retired/refuted facts (the model's "
            "track record)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_closed": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "note_pending_expense",
        "description": (
            "Record a mentioned spend that WILL eventually appear in the data "
            "but hasn't yet — the canonical case is the Apple Card, which has "
            "no bank feed: its transactions arrive only when someone emails a "
            "Wallet export, weeks later. A pending expense is how you hold that "
            "knowledge honestly in between: it shows up when the household asks "
            "where money is going (say it's 'mentioned, not yet posted'), it is "
            "matched AUTOMATICALLY against the statement when the export "
            "arrives, and if it never appears you can raise it. Amount is "
            "positive dollars spent. Use log_expense instead for cash/P2P money "
            "no source will ever show; use neither for live-feed accounts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Dollars spent, positive"},
                "description": {"type": "string", "description": "What it was, e.g. 'New tires, Costco'"},
                "account": {"type": "string", "description": "Where they said it went, e.g. 'Apple Card'"},
                "date": {"type": "string", "description": "ISO date it happened/was mentioned; omit for today"},
                "spender": {"type": "string", "description": "Ford | Gaurav | joint"},
            },
            "required": ["amount", "description", "account"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_pending_expenses",
        "description": (
            "Every mentioned-but-not-yet-posted spend still waiting for data "
            "(see note_pending_expense), oldest first with age. Consult this "
            "whenever spending, a balance, or a discrepancy is discussed — a "
            "gap between what the household says and what the ledger shows is "
            "very often just Apple Card spending waiting on the next export. "
            "Entries flagged stale have waited 45+ days: ask for a fresh Wallet "
            "export, or question the charge."
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


def _send_to_printer(pdf_path, title: str) -> dict:
    """One honest shape for every print tool: the job id when it printed, the
    saved path and a plain-speech note when it did not."""
    from .. import reports

    try:
        job = reports.print_pdf(pdf_path, title=title)
        return {"printed": True, "job": job, "pdf": str(pdf_path)}
    except Exception as exc:
        return {
            "printed": False,
            "error": str(exc)[:200],
            "pdf_saved": str(pdf_path),
            "note": "tell the household plainly that the page did not print",
        }


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
    if name == "print_weekly_report":
        from .. import reports

        data = reports.gather_weekly_data(session)
        pdf_path = reports.REPORTS_DIR / f"weekly-{data['date']}.pdf"
        reports.render_pdf(data, args["summary"], pdf_path)
        return _send_to_printer(pdf_path, "household weekly report")
    if name == "print_page":
        import re as _re

        from .. import reports

        slug = _re.sub(r"[^a-z0-9]+", "-", args["title"].lower()).strip("-")[:40] or "page"
        pdf_path = reports.REPORTS_DIR / f"page-{slug}.pdf"
        reports.render_text_page(args["title"], args["body"], pdf_path)
        return _send_to_printer(pdf_path, args["title"][:60])
    if name == "print_document":
        # NB: import only reports — a local `from .. import vault` here would
        # shadow the module-level vault for the WHOLE function and break every
        # earlier branch that touches it (UnboundLocalError).
        from .. import reports

        doc = session.get(Document, args["document_id"])
        if doc is None:
            return {"error": f"no document with id {args['document_id']}"}
        original = vault.stored_path(doc)
        if original is not None and original.suffix.lower() == ".pdf":
            return _send_to_printer(original, doc.title[:60])
        if not doc.content_text:
            return {"error": "that document has no printable text extracted"}
        pdf_path = reports.REPORTS_DIR / f"doc-{doc.id}.pdf"
        reports.render_text_page(doc.title, doc.content_text, pdf_path)
        return _send_to_printer(pdf_path, doc.title[:60])
    if name == "recategorize_transactions":
        pattern = (args.get("description_contains") or "").strip()
        ids = args.get("transaction_ids") or []
        new_category = args["new_category"].strip()
        if not pattern and not ids:
            return {"error": "give description_contains or transaction_ids — refusing to relabel everything"}
        if pattern and len(pattern) < 4:
            return {"error": "description_contains must be at least 4 characters — shorter patterns catch strangers"}
        query = select(Transaction)
        if pattern:
            query = query.where(Transaction.description.ilike(f"%{pattern}%"))
        if ids:
            query = query.where(Transaction.id.in_(ids))
        if args.get("current_category"):
            query = query.where(Transaction.category == args["current_category"])
        rows = session.execute(query).scalars().all()
        for row in rows:
            row.category = new_category
        rule_saved = False
        if pattern and args.get("remember", True):
            rule = session.execute(
                select(CategoryRule).where(CategoryRule.pattern == pattern)
            ).scalar_one_or_none()
            if rule:
                rule.category = new_category
                rule.reason = args.get("reason", "") or rule.reason
            else:
                session.add(CategoryRule(
                    pattern=pattern, category=new_category,
                    reason=args.get("reason", ""),
                ))
            rule_saved = True
        session.flush()
        return {
            "updated": len(rows),
            "new_category": new_category,
            "rule_saved": rule_saved,
            "sample": [r.description[:80] for r in rows[:3]],
        }
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
    if name == "set_account_terms":
        account = session.get(Account, args["account_id"])
        if not account:
            return {"error": "account not found — call get_accounts for ids"}

        def _d(key):
            raw = (args.get(key) or "").strip()
            return date.fromisoformat(raw) if raw else None

        accounts_terms.set_terms(
            session, account.id,
            statement_balance=args.get("statement_balance"),
            minimum_payment=args.get("minimum_payment"),
            payment_due_date=_d("payment_due_date"),
            due_day_of_month=args.get("due_day_of_month"),
            apr=args.get("apr"),
            as_of=_d("as_of"),
            source=args["source"],
        )
        return {
            "recorded": True,
            "account": account.name,
            "note": "Balance untouched — terms only." if account.source != "manual" else None,
        }
    if name == "track_account":
        kind = (args.get("kind") or "other").strip().lower()
        if kind not in MANUAL_KINDS:
            return {"error": f"kind must be one of {MANUAL_KINDS}"}
        label = args["name"].strip()
        if not label:
            return {"error": "name is required"}
        existing = session.execute(
            select(Account).where(Account.source == "manual", Account.name == label)
        ).scalar_one_or_none()
        if existing is not None and existing.source != "manual":
            return {"error": f"'{label}' is a synced account and cannot be set by hand"}

        old = existing.balance if existing else None
        account = upsert_account(
            session, source="manual", name=label, kind=kind,
            balance=normalize_manual_balance(kind, float(args["balance"])),
        )
        if args.get("owner"):
            account.owner = args["owner"].strip().lower()
        session.flush()

        def _date(key):
            raw = (args.get(key) or "").strip()
            return date.fromisoformat(raw) if raw else None

        accounts_terms.set_terms(
            session, account.id,
            statement_balance=abs(float(args["balance"])),
            minimum_payment=args.get("minimum_payment"),
            payment_due_date=_date("payment_due_date"),
            apr=args.get("apr"),
            as_of=_date("as_of"),
            source=args["source"],
        )
        return {
            "tracked": True,
            "created": old is None,
            "account": account.name,
            "old_balance": old,
            "balance": account.balance,
            "net_worth": net_worth(session)["total"],
        }
    if name == "log_expense":
        from ..ingest import TxnIn, ingest_transactions

        try:
            amount = abs(float(args["amount"]))
        except (TypeError, ValueError):
            return {"error": "amount must be a number"}
        if amount == 0:
            return {"error": "amount must be non-zero"}
        description = (args.get("description") or "").strip()
        if not description:
            return {"error": "description is required"}
        spender = (args.get("spender") or "").strip()
        if spender:
            description = f"{description} ({spender})"
        raw_date = (args.get("date") or "").strip()
        try:
            posted = date.fromisoformat(raw_date) if raw_date else date.today()
        except ValueError:
            return {"error": f"date {raw_date!r} is not ISO (YYYY-MM-DD)"}
        signed = amount if args.get("received") else -amount

        account = session.execute(
            select(Account).where(
                Account.source == "manual", Account.name == "Cash & untracked"
            )
        ).scalar_one_or_none()
        if account is None:
            # kind "other": a spending log, not an asset — no balance, so it
            # feeds spending summaries without inventing a net-worth figure.
            account = upsert_account(
                session, source="manual", name="Cash & untracked", kind="other"
            )
        result = ingest_transactions(
            session, account, [TxnIn(posted=posted, amount=signed, description=description)]
        )
        if result.added and args.get("category"):
            txn = session.get(Transaction, result.ids[0])
            txn.category = str(args["category"]).strip().lower()
        session.flush()
        return {
            "logged": bool(result.added),
            "duplicate": result.skipped > 0,
            "account": account.name,
            "posted": posted.isoformat(),
            "amount": signed,
            "description": description,
        }
    if name == "cancel_subscription":
        from .. import cancellations

        merchant = (args.get("merchant") or "").strip()
        if not merchant:
            return {"error": "merchant is required"}
        reason = cancellations.guard_reason(session, merchant)
        if reason:
            return {
                "refused": True,
                "reason": reason,
                "next_step": (
                    "This category is too consequential for the standing "
                    "authorization. Use propose_action so the household can "
                    "approve it in the dashboard, and explain the stakes "
                    "(coverage gap, service loss, credit impact) when you do."
                ),
            }
        return cancellations.execute(
            session,
            merchant=merchant,
            service_name=(args.get("service_name") or merchant).strip(),
            support_email=(args.get("support_email") or "").strip(),
            instructed_by=(args.get("instructed_by") or "").strip(),
            account_name=(args.get("account_name") or "").strip(),
            account_identifier=(args.get("account_identifier") or "").strip(),
        )
    if name == "open_initiative":
        from .. import initiatives

        title = (args.get("title") or "").strip()
        if not title:
            return {"error": "title is required"}
        row = initiatives.open_initiative(
            session,
            title=title,
            goal=(args.get("goal") or "").strip(),
            plan=(args.get("plan") or "").strip(),
            next_action=(args.get("next_action") or "").strip(),
            priority=int(args.get("priority") or 100),
        )
        return {"opened": True, "initiative_id": row.id, "title": row.title,
                "open_count": len(initiatives.as_dicts(session))}
    if name == "update_initiative":
        from .. import initiatives

        row = initiatives.update_initiative(
            session,
            (args.get("initiative_id") or "").strip(),
            worklog_entry=args.get("worklog_entry"),
            next_action=args.get("next_action"),
            plan=args.get("plan"),
            status=args.get("status"),
            priority=args.get("priority"),
            blocked_on=args.get("blocked_on"),
        )
        if row is None:
            return {"error": "initiative not found — call list_initiatives for ids"}
        return {"updated": True, "initiative_id": row.id, "status": row.status,
                "next_action": row.next_action, "blocked_on": row.blocked_on}
    if name == "list_initiatives":
        from .. import initiatives

        return {"initiatives": initiatives.as_dicts(
            session, include_closed=bool(args.get("include_closed"))
        )}
    if name == "generate_report":
        import re as _re

        from .. import reports
        from ..messaging import email_thread

        title = (args.get("title") or "").strip()
        sections = args.get("sections") or []
        if not title or not isinstance(sections, list) or not sections:
            return {"error": "title and a non-empty sections list are required"}
        clean_sections = [
            {"heading": str(s.get("heading", "")), "body": str(s.get("body", ""))}
            for s in sections if isinstance(s, dict)
        ]
        slug = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "report"
        pdf_path = reports.REPORTS_DIR / f"report-{slug}.pdf"
        reports.render_report_pdf(title, clean_sections, pdf_path)

        printed = None
        if args.get("print_copy", True):
            printed = _send_to_printer(pdf_path, title[:60])

        emailed = None
        if email_thread.configured():
            body = f"{title}\n\n" + "\n\n".join(
                f"{s['heading']}\n{s['body']}".strip() for s in clean_sections
            )
            try:
                emailed = email_thread.start_thread(session, subject=title[:120], body=body)
            except Exception as exc:  # delivery failure must be honest, not hidden
                emailed = {"error": str(exc)[:200]}
        return {
            "generated": True,
            "pdf": str(pdf_path),
            "printed": printed,
            "emailed_to_household": bool(emailed and not emailed.get("error")),
            "delivery_note": (
                "Rendered. " + ("Printed. " if printed and printed.get("job") else "")
                + ("Emailed to both spouses." if emailed and not emailed.get("error")
                   else "Email channel not configured — it is saved and printed.")
            ),
        }
    if name == "read_source":
        from .. import selfimprove

        try:
            return selfimprove.read_source((args.get("path") or "").strip())
        except selfimprove.PathRefused as exc:
            return {"error": str(exc)}
    if name == "list_source":
        from .. import selfimprove

        return selfimprove.list_source((args.get("subdir") or "bankai").strip())
    if name == "propose_patch":
        from .. import selfimprove

        files = args.get("files")
        if not isinstance(files, dict) or not files:
            return {"error": "files must be a non-empty object of path -> contents"}
        try:
            row = selfimprove.record_proposal(
                session,
                title=(args.get("title") or "").strip(),
                rationale=(args.get("rationale") or "").strip(),
                files={str(k): v for k, v in files.items()},
                test_paths=(args.get("test_paths") or "").strip(),
            )
        except (selfimprove.PathRefused, ValueError) as exc:
            return {"error": str(exc)}
        return {
            "proposed": True,
            "proposal_id": row.id,
            "status": row.status,
            "files": sorted(selfimprove.files_of(row)),
            "diff_preview": (row.diff or "")[:1500],
            "note": (
                "Recorded and diffed. Its tests run in the sandbox (or await a "
                "trusted reviewer); a human ships it. You cannot deploy it "
                "yourself — surface it to Ford so he can review and merge."
            ),
        }
    if name == "list_code_proposals":
        from .. import selfimprove

        return {"proposals": selfimprove.as_dicts(
            session, include_closed=bool(args.get("include_closed"))
        )}
    if name == "record_life_fact":
        from .. import lifemodel

        statement = (args.get("statement") or "").strip()
        if not statement:
            return {"error": "statement is required"}
        row = lifemodel.record(
            session,
            statement=statement,
            kind=(args.get("kind") or "event").strip(),
            evidence=(args.get("evidence") or "").strip(),
            confidence=(args.get("confidence") or "medium").strip(),
        )
        return {"recorded": True, "fact_id": row.id, "kind": row.kind,
                "statement": row.statement}
    if name == "update_life_fact":
        from .. import lifemodel

        row = lifemodel.update(
            session,
            (args.get("fact_id") or "").strip(),
            statement=args.get("statement"),
            evidence=args.get("evidence"),
            confidence=args.get("confidence"),
            status=args.get("status"),
        )
        if row is None:
            return {"error": "fact not found — call list_life_facts for ids"}
        return {"updated": True, "fact_id": row.id, "status": row.status,
                "confidence": row.confidence}
    if name == "list_life_facts":
        from .. import lifemodel

        return {"facts": lifemodel.as_dicts(
            session, include_closed=bool(args.get("include_closed"))
        )}
    if name == "note_pending_expense":
        from .. import pending as pending_lib

        try:
            amount = abs(float(args["amount"]))
        except (TypeError, ValueError):
            return {"error": "amount must be a number"}
        if amount == 0:
            return {"error": "amount must be non-zero"}
        description = (args.get("description") or "").strip()
        if not description:
            return {"error": "description is required"}
        raw_date = (args.get("date") or "").strip()
        try:
            mentioned = date.fromisoformat(raw_date) if raw_date else date.today()
        except ValueError:
            return {"error": f"date {raw_date!r} is not ISO (YYYY-MM-DD)"}
        row, created = pending_lib.note(
            session,
            amount=amount,
            description=description,
            account_hint=(args.get("account") or "").strip(),
            speaker=(args.get("spender") or "").strip(),
            mentioned_on=mentioned,
        )
        return {
            "noted": created,
            "duplicate_of": None if created else row.id,
            "pending_id": row.id,
            "amount": row.amount,
            "account": row.account_hint,
            "open_pending_count": len(pending_lib.open_items(session)),
        }
    if name == "list_pending_expenses":
        from .. import pending as pending_lib

        items = pending_lib.open_items(session)
        return {
            "open": items,
            "total_open_amount": round(sum(i["amount"] for i in items), 2),
            "note": (
                "These are spends the household mentioned that no statement or "
                "feed has shown yet. Count them when asked where money went; "
                "stale ones deserve a nudge for a fresh Apple Card export."
            ),
        }
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
