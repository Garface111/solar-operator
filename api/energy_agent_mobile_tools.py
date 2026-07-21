"""Energy Agent tools that close mobile-app capability gaps.

The owner-web /m and owner-native apps invite the agent to: create offtakers,
measure marketplace vacancy, capture demand leads, and start Stripe Connect.
Those flows existed as HTTP endpoints but were not agent tools — so the agent
could only narrate "go to desktop". This module registers the missing tools
and handlers so every mobile Ask-Agent CTA has a real tool path.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

log = logging.getLogger("energy_agent.mobile_tools")

# ── Tool definitions (OpenAI/Anthropic function schema) ─────────────────────

TOOL_DEFS_EXTRA: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "create_offtaker",
            "description": (
                "CREATE a new offtaker (BillingReportSubscription) for THIS tenant. "
                "Use when the owner wants to add a subscriber/customer for solar-credit "
                "invoices. Minimum: name. Prefer email + share_pct (percent 25 or fraction 0.25) "
                "+ array_name or array_id when known. "
                "Set needs_confirm=false when the user already stated the exact offtaker "
                "(name, email, share). On demo accounts returns a clear demo-blocked error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Customer / offtaker display name"},
                    "email": {"type": "string"},
                    "share_pct": {
                        "type": "number",
                        "description": "Share as percent (25) or fraction (0.25)",
                    },
                    "array_id": {"type": "integer"},
                    "array_name": {
                        "type": "string",
                        "description": "Resolve master array/group by name (partial ok)",
                    },
                    "utility_account_id": {"type": "integer"},
                    "utility_account_name": {"type": "string"},
                    "delivery_mode": {
                        "type": "string",
                        "enum": ["approval", "auto"],
                        "description": "Default approval",
                    },
                    "send_mode": {
                        "type": "string",
                        "enum": ["to_me", "to_client", "to_both"],
                    },
                    "net_rate_per_kwh": {"type": "number"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "marketplace_vacancy",
            "description": (
                "Compute THIS tenant's unallocated solar-credit vacancy (kWh + $) "
                "per array using the Offtaker Exchange engine (bill-side + registry). "
                "Use for Marketplace / 'how much excess can I sell / list' questions. "
                "Does NOT list other tenants' data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_months": {
                        "type": "integer",
                        "description": "Trailing settled months (default engine default)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_exchange_demand",
            "description": (
                "List THIS operator's Offtaker Exchange waitlist leads (people who want "
                "credits). Newest first. Use for Marketplace demand / waitlist questions."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_exchange_demand",
            "description": (
                "Add a demand lead to THIS operator's Offtaker Exchange waitlist "
                "(someone who wants solar credits). Stores only — sends nothing, takes no money. "
                "Need at least contact_name or contact_email. "
                "Set needs_confirm=false when the user already gave name/email in this turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string"},
                    "contact_email": {"type": "string"},
                    "contact_phone": {"type": "string"},
                    "utility": {
                        "type": "string",
                        "description": "Territory key e.g. gmp | vec | wec",
                    },
                    "desired_band": {
                        "type": "string",
                        "description": "Free-text kWh/mo band or size ask",
                    },
                    "monthly_bill_usd": {"type": "number"},
                    "notes": {"type": "string"},
                    "suggested_array_id": {"type": "integer"},
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "payments_connect_status",
            "description": (
                "Offtaker online-pay (Stripe Connect Express) status for THIS owner: "
                "connected, charges_enabled, ready, fee. Use for 'can offtakers pay online' "
                "and Stripe Connect walkthroughs."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_payments_connect",
            "description": (
                "Start or resume Stripe Connect Express onboarding for offtaker payouts. "
                "Returns a Stripe-hosted URL the owner must open to finish bank setup. "
                "Demo accounts are blocked. Set needs_confirm=false when the user clearly "
                "asked to enable online pay / connect Stripe now."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "needs_confirm": {"type": "boolean", "default": True},
                },
                "required": [],
            },
        },
    },
]


def run_mobile_tool(
    name: str,
    args: dict,
    tenant,
    session,
    db,
    *,
    user_text: str = "",
) -> dict[str, Any] | None:
    """Handle a mobile-extra tool. Returns None if name is not ours."""
    if name == "create_offtaker":
        return _create_offtaker(args, tenant, session, db, user_text=user_text)
    if name == "marketplace_vacancy":
        return _marketplace_vacancy(args, tenant, db)
    if name == "list_exchange_demand":
        return _list_exchange_demand(tenant, db)
    if name == "create_exchange_demand":
        return _create_exchange_demand(args, tenant, session, db, user_text=user_text)
    if name == "payments_connect_status":
        return _payments_connect_status(tenant, db)
    if name == "start_payments_connect":
        return _start_payments_connect(args, tenant, session, db, user_text=user_text)
    return None


def _share_to_fraction(share_pct) -> float | None:
    if share_pct is None:
        return None
    try:
        sp = float(share_pct)
    except (TypeError, ValueError):
        return None
    if sp > 1.0:
        sp = sp / 100.0
    if sp <= 0 or sp > 1.0:
        return None
    return sp


def _resolve_array(db, tid: str, array_id=None, array_name: str | None = None):
    from sqlalchemy import select
    from .models import Array

    if array_id is not None:
        try:
            aid = int(array_id)
        except (TypeError, ValueError):
            return None, f"invalid array_id: {array_id}"
        a = db.get(Array, aid)
        if a is None or a.tenant_id != tid or getattr(a, "deleted_at", None):
            return None, f"array #{aid} not found"
        return a, None
    name_q = (array_name or "").strip()
    if not name_q:
        return None, None
    rows = db.execute(
        select(Array).where(
            Array.tenant_id == tid,
            Array.deleted_at.is_(None),
            Array.name.ilike(f"%{name_q}%"),
        ).limit(8)
    ).scalars().all()
    if not rows:
        return None, f"no array matching '{name_q}'"
    if len(rows) > 1:
        exact = [a for a in rows if (a.name or "").lower() == name_q.lower()]
        if len(exact) == 1:
            return exact[0], None
        return None, {
            "error": "multiple arrays match — pass array_id",
            "matches": [{"id": a.id, "name": a.name} for a in rows],
        }
    return rows[0], None


def _create_offtaker(args, tenant, session, db, *, user_text: str = "") -> dict:
    from .models import BillingReportSubscription, Client

    tid = tenant.id
    if bool(getattr(tenant, "is_demo", False)):
        return {
            "ok": False,
            "error": "demo_blocked",
            "message": (
                "Demo accounts cannot create offtakers. Sign into a real Array Operator "
                "account to add subscribers."
            ),
        }

    name = (args.get("name") or args.get("customer_name") or "").strip()
    if not name:
        return {"ok": False, "error": "name is required"}
    email = (args.get("email") or args.get("client_email") or "").strip() or None
    share = _share_to_fraction(args.get("share_pct") or args.get("allocation_pct"))
    delivery_mode = (args.get("delivery_mode") or "approval").strip() or "approval"
    send_mode = (args.get("send_mode") or "to_me").strip() or "to_me"
    if delivery_mode not in ("approval", "auto"):
        delivery_mode = "approval"
    if send_mode not in ("to_me", "to_client", "to_both"):
        send_mode = "to_me"

    arr, err = _resolve_array(
        db, tid, args.get("array_id"), args.get("array_name")
    )
    if isinstance(err, dict):
        return err
    if isinstance(err, str) and args.get("array_name"):
        return {"ok": False, "error": err}

    array_id = arr.id if arr else args.get("array_id")
    try:
        array_id = int(array_id) if array_id is not None else None
    except (TypeError, ValueError):
        array_id = None

    preview = {
        "name": name,
        "email": email,
        "share_fraction": share,
        "array_id": array_id,
        "array_name": arr.name if arr else None,
        "delivery_mode": delivery_mode,
        "send_mode": send_mode,
    }

    needs = bool(args.get("needs_confirm", True))
    # Auto-apply when user already stated a full create in this turn
    if needs and name and (email or share is not None):
        ut = (user_text or "").lower()
        if any(k in ut for k in ("add ", "create ", "new offtaker", "new subscriber")):
            needs = False

    if needs:
        cmd = {
            "id": uuid.uuid4().hex[:12],
            "type": "api_patch",
            "tool": "create_offtaker",
            # Flat args so confirm() re-runs _run_tool(tool, args) correctly
            "args": {
                "name": name,
                "email": email,
                "share_pct": (round(float(share) * 100, 4) if share is not None else None),
                "array_id": array_id,
                "delivery_mode": delivery_mode,
                "send_mode": send_mode,
                "net_rate_per_kwh": args.get("net_rate_per_kwh"),
                "needs_confirm": False,
            },
            "reason": f"Create offtaker {name}" + (f" ({email})" if email else ""),
        }
        return {
            "status": "pending_confirm",
            "pending": cmd,
            "message": (
                f"Ready to create offtaker '{name}'. "
                "If the owner already named them, re-call with needs_confirm=false."
            ),
            "preview": preview,
        }

    # Apply now
    try:
        client = None
        from sqlalchemy import select

        client = db.execute(
            select(Client).where(
                Client.tenant_id == tid,
                Client.name == name,
                Client.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if client is None:
            client = Client(
                tenant_id=tid,
                name=name,
                contact_email=email,
                active=True,
            )
            db.add(client)
            db.flush()

        from .billing.delivery import next_send_at

        sub = BillingReportSubscription(
            tenant_id=tid,
            client_id=client.id,
            customer_name=name,
            client_email=email,
            array_id=array_id,
            allocation_pct=share,
            array_share_pct=share,
            delivery_mode=delivery_mode,
            send_mode=send_mode,
            cadence="monthly",
            enabled=True,
            net_rate_per_kwh=args.get("net_rate_per_kwh"),
            next_send_at=next_send_at("monthly"),
            operator_email=getattr(tenant, "contact_email", None),
        )
        db.add(sub)
        db.flush()
        try:
            from .stripe_helpers import reconcile_offtaker_quantity

            reconcile_offtaker_quantity(tid)
        except Exception as e:
            log.debug("reconcile offtaker qty skipped: %s", e)
        db.commit()
        db.refresh(sub)
        return {
            "ok": True,
            "created": True,
            "subscription_id": sub.id,
            "name": sub.customer_name,
            "email": sub.client_email,
            "array_id": sub.array_id,
            "share_pct": (round(float(share) * 100, 4) if share is not None else None),
            "message": f"Created offtaker #{sub.id} {name}.",
            "ui_command": {
                "type": "navigate",
                "hash": "#reports",
                "hint": "Open Invoices to see the new offtaker",
            },
        }
    except Exception as e:
        log.exception("create_offtaker failed")
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)[:300]}


def _marketplace_vacancy(args, tenant, db) -> dict:
    try:
        from .market_vacancy import tenant_vacancy

        kw = {}
        if args.get("window_months"):
            try:
                kw["window_months"] = int(args["window_months"])
            except (TypeError, ValueError):
                pass
        out = tenant_vacancy(db, tenant.id, **kw)
        # Compact for agent context
        rows = out.get("arrays") or out.get("rows") or []
        if isinstance(rows, list) and len(rows) > 40:
            out = dict(out)
            out["arrays"] = rows[:40]
            out["arrays_truncated"] = True
            out["arrays_total"] = len(rows)
        return {"ok": True, **(out if isinstance(out, dict) else {"data": out})}
    except Exception as e:
        log.exception("marketplace_vacancy failed")
        return {"ok": False, "error": str(e)[:300]}


def _list_exchange_demand(tenant, db) -> dict:
    try:
        from sqlalchemy import select
        from .models import ExchangeDemand
        from .exchange_match import lead_dict

        rows = db.execute(
            select(ExchangeDemand)
            .where(ExchangeDemand.tenant_id == tenant.id)
            .order_by(ExchangeDemand.id.desc())
            .limit(200)
        ).scalars().all()
        items = [lead_dict(r) for r in rows]
        return {"ok": True, "leads": items, "count": len(items)}
    except Exception as e:
        log.exception("list_exchange_demand failed")
        return {"ok": False, "error": str(e)[:300], "leads": []}


def _create_exchange_demand(args, tenant, session, db, *, user_text: str = "") -> dict:
    if bool(getattr(tenant, "is_demo", False)):
        return {
            "ok": False,
            "error": "demo_blocked",
            "message": "Demo accounts cannot write waitlist leads. Use a real owner account.",
        }
    name = (args.get("contact_name") or args.get("name") or "").strip()
    email = (args.get("contact_email") or args.get("email") or "").strip()
    if not name and not email:
        return {
            "ok": False,
            "error": "Add at least a name or an email for the lead.",
        }

    needs = bool(args.get("needs_confirm", True))
    ut = (user_text or "").lower()
    if needs and (name or email) and any(
        k in ut for k in ("add ", "capture", "waitlist", "demand", "lead")
    ):
        needs = False

    preview = {
        "contact_name": name or None,
        "contact_email": email or None,
        "utility": (args.get("utility") or "").strip().lower() or None,
        "desired_band": (args.get("desired_band") or "").strip() or None,
        "monthly_bill_usd": args.get("monthly_bill_usd"),
        "notes": (args.get("notes") or "").strip() or None,
    }

    if needs:
        return {
            "status": "pending_confirm",
            "pending": {
                "id": uuid.uuid4().hex[:12],
                "type": "api_patch",
                "tool": "create_exchange_demand",
                "reason": f"Add demand lead {name or email}",
                "args": preview,
            },
            "message": "Ready to save this waitlist lead. Re-call with needs_confirm=false if already stated.",
            "preview": preview,
        }

    try:
        from .models import ExchangeDemand
        from .exchange_match import lead_dict

        row = ExchangeDemand(
            tenant_id=tenant.id,
            contact_name=name or None,
            contact_email=email or None,
            contact_phone=(args.get("contact_phone") or "").strip() or None,
            utility=((args.get("utility") or "").strip().lower() or None),
            desired_band=(args.get("desired_band") or "").strip() or None,
            monthly_bill_usd=args.get("monthly_bill_usd"),
            notes=(args.get("notes") or "").strip() or None,
            suggested_array_id=args.get("suggested_array_id"),
            source="operator_waitlist",
            status="new",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "ok": True,
            "created": True,
            "id": row.id,
            "lead": lead_dict(row),
            "message": f"Saved waitlist lead #{row.id}.",
        }
    except Exception as e:
        log.exception("create_exchange_demand failed")
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)[:300]}


def _payments_connect_status(tenant, db) -> dict:
    try:
        from .billing import payments as pay
        from .models import Tenant

        t = db.get(Tenant, tenant.id) or tenant
        status = {}
        try:
            status = pay.refresh_connect_status(db, t) or {}
        except Exception as e:
            log.debug("refresh_connect_status: %s", e)
        account_id = status.get("account_id") or getattr(
            t, "stripe_connect_account_id", None
        )
        charges = bool(
            status.get("charges_enabled")
            or getattr(t, "stripe_connect_charges_enabled", False)
        )
        return {
            "ok": True,
            "enabled": pay.payments_enabled(),
            "connected": bool(account_id),
            "account_id": account_id,
            "charges_enabled": charges,
            "details_submitted": bool(status.get("details_submitted")),
            "fee_bps": pay.fee_bps(),
            "fee_percent": pay.fee_bps() / 100.0,
            "ready": charges,
            "product": getattr(t, "product", None),
            "is_demo": bool(getattr(t, "is_demo", False)),
            "hint": (
                "When ready is true, offtaker invoice emails can include a pay link. "
                "Use start_payments_connect to open Stripe onboarding if not connected."
            ),
        }
    except Exception as e:
        log.exception("payments_connect_status failed")
        return {"ok": False, "error": str(e)[:300]}


def _start_payments_connect(args, tenant, session, db, *, user_text: str = "") -> dict:
    if bool(getattr(tenant, "is_demo", False)):
        return {
            "ok": False,
            "error": "demo_blocked",
            "message": "Demo cannot connect Stripe. Use a real Array Operator account.",
        }
    if (getattr(tenant, "product", None) or "nepool") != "array_operator":
        return {
            "ok": False,
            "error": "product_gate",
            "message": "Online offtaker payments are Array Operator only.",
        }

    needs = bool(args.get("needs_confirm", True))
    ut = (user_text or "").lower()
    if needs and any(
        k in ut for k in ("connect stripe", "enable online", "start stripe", "set up pay")
    ):
        needs = False

    if needs:
        return {
            "status": "pending_confirm",
            "pending": {
                "id": uuid.uuid4().hex[:12],
                "type": "api_patch",
                "tool": "start_payments_connect",
                "reason": "Start Stripe Connect onboarding for offtaker online pay",
            },
            "message": "Ready to open Stripe bank setup. Re-call with needs_confirm=false if the owner already asked.",
        }

    try:
        from .billing import payments as pay
        from .branding import app_url
        from .models import Tenant

        base = app_url("array_operator").rstrip("/")
        t = db.get(Tenant, tenant.id) or tenant
        created = pay.create_or_get_connect_account(db, t)
        if not created.get("ok"):
            return {
                "ok": False,
                "error": created.get("error") or "Couldn't start bank setup.",
                "error_code": created.get("error_code"),
            }
        db.refresh(t)
        if created.get("charges_enabled"):
            return {
                "ok": True,
                "already_ready": True,
                "message": "Stripe Connect already enabled — offtaker pay links can go live.",
            }
        link = pay.create_account_link(
            t,
            refresh_url=f"{base}/?connect=refresh#account",
            return_url=f"{base}/?connect=return#account",
        )
        if not link.get("ok") or not link.get("url"):
            return {
                "ok": False,
                "error": (link or {}).get("error") or "No Stripe onboarding URL returned",
            }
        url = link["url"]
        return {
            "ok": True,
            "url": url,
            "message": "Open this Stripe URL to finish bank setup for offtaker payouts.",
            "ui_command": {"type": "open_url", "url": url, "label": "Open Stripe Connect"},
        }
    except Exception as e:
        log.exception("start_payments_connect failed")
        return {"ok": False, "error": str(e)[:300]}
