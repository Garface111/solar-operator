"""Sovereign succession authority — Ford full grant 2026-07-16.

Unlocks previously dual-control domains:
  1. Money / Stripe identity (billing, subscriptions, refunds)
  2. Brand final call (messaging, positioning, external comms)
  3. Irreversible hard-deletes (tenant purge, data wipe)
  4. HAR capture authority (stage owner-browser portal captures)

Every action writes audit + self-note. Kill switches:
  SOVEREIGN_SUCCESSION_FULL=0  — reverts all four to denied
  SOVEREIGN_ENABLED=0          — master kill
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import select, text

log = logging.getLogger("energy_agent.sovereign.succession")


def _now() -> datetime:
    return datetime.utcnow()


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def succession_full() -> bool:
    """Ford authorized full succession (default ON after 2026-07-16 grant)."""
    return _flag("SOVEREIGN_ENABLED", "1") and _flag("SOVEREIGN_SUCCESSION_FULL", "1")


def _deny(reason: str) -> dict:
    return {"ok": False, "denied": True, "denied_reason": reason}


def _audit_note(db, *, capability: str, title: str, body: dict, result: str = "ok") -> None:
    from .energy_agent_sovereign import audit, write_note
    write_note(
        db, kind="decision", title=title,
        body=json.dumps(body, default=str)[:8000],
        provider="succession",
    )
    audit(
        db, capability=capability, decision="act",
        rationale=title[:300],
        targets=body if isinstance(body, dict) else {"body": str(body)[:400]},
        result=result,
    )


# ── 1. Money / Stripe ───────────────────────────────────────────────────────
def stripe_inspect(db, *, tenant_id: str | None = None) -> dict:
    """Read Stripe customer + subscription meta (no secret keys in response)."""
    if not succession_full():
        return _deny("succession full off")
    from .models import Tenant
    import stripe
    out: dict[str, Any] = {"ok": True, "tenants": []}
    q = select(Tenant)
    if tenant_id:
        q = q.where(Tenant.id == tenant_id)
    else:
        q = q.where(Tenant.stripe_customer_id.isnot(None)).limit(40)
    rows = db.execute(q).scalars().all()
    for t in rows:
        item = {
            "tenant_id": t.id,
            "name": t.name,
            "email": getattr(t, "contact_email", None),
            "subscription_status": t.subscription_status,
            "billing_plan": getattr(t, "billing_plan", None),
            "stripe_customer_id": t.stripe_customer_id,
            "stripe_subscription_id": getattr(t, "stripe_subscription_id", None),
            "active": bool(getattr(t, "active", True)),
        }
        try:
            if t.stripe_customer_id and (os.getenv("STRIPE_SECRET_KEY") or "").strip():
                cust = stripe.Customer.retrieve(t.stripe_customer_id)
                item["stripe_email"] = cust.get("email")
                item["stripe_delinquent"] = cust.get("delinquent")
            if getattr(t, "stripe_subscription_id", None) and (os.getenv("STRIPE_SECRET_KEY") or "").strip():
                sub = stripe.Subscription.retrieve(t.stripe_subscription_id)
                item["stripe_sub_status"] = sub.get("status")
                item["cancel_at_period_end"] = sub.get("cancel_at_period_end")
                item["current_period_end"] = sub.get("current_period_end")
        except Exception as e:  # noqa: BLE001
            item["stripe_error"] = str(e)[:200]
        out["tenants"].append(item)
    return out


def stripe_cancel_subscription(
    db,
    *,
    tenant_id: str,
    at_period_end: bool = True,
    reason: str = "Sovereign succession authority",
) -> dict:
    if not succession_full():
        return _deny("succession full off")
    from .models import Tenant
    import stripe
    t = db.get(Tenant, tenant_id)
    if not t:
        return _deny("tenant not found")
    sub_id = getattr(t, "stripe_subscription_id", None)
    if not sub_id:
        return _deny("no stripe_subscription_id")
    if not (os.getenv("STRIPE_SECRET_KEY") or "").strip():
        return _deny("STRIPE_SECRET_KEY not configured")
    try:
        if at_period_end:
            sub = stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
        else:
            sub = stripe.Subscription.cancel(sub_id)
        t.subscription_status = sub.get("status") or (
            "canceled" if not at_period_end else t.subscription_status
        )
        if not at_period_end:
            t.active = False
        db.flush()
        res = {
            "ok": True,
            "tenant_id": tenant_id,
            "subscription_id": sub_id,
            "at_period_end": at_period_end,
            "stripe_status": sub.get("status"),
            "reason": reason[:500],
        }
        _audit_note(db, capability="act.money_identity", title=f"stripe cancel {tenant_id}", body=res)
        return res
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400]}


def stripe_refund(
    db,
    *,
    payment_intent_id: str | None = None,
    charge_id: str | None = None,
    amount_cents: int | None = None,
    reason: str = "requested_by_customer",
    note: str = "Sovereign succession refund",
) -> dict:
    if not succession_full():
        return _deny("succession full off")
    import stripe
    if not (os.getenv("STRIPE_SECRET_KEY") or "").strip():
        return _deny("STRIPE_SECRET_KEY not configured")
    if not payment_intent_id and not charge_id:
        return _deny("payment_intent_id or charge_id required")
    try:
        kwargs: dict[str, Any] = {"reason": reason if reason in (
            "duplicate", "fraudulent", "requested_by_customer",
        ) else "requested_by_customer"}
        if amount_cents is not None:
            kwargs["amount"] = int(amount_cents)
        if payment_intent_id:
            kwargs["payment_intent"] = payment_intent_id
        else:
            kwargs["charge"] = charge_id
        ref = stripe.Refund.create(**kwargs)
        res = {
            "ok": True,
            "refund_id": ref.get("id"),
            "status": ref.get("status"),
            "amount": ref.get("amount"),
            "currency": ref.get("currency"),
            "note": note[:500],
        }
        _audit_note(db, capability="act.money_identity", title="stripe refund", body=res)
        return res
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400]}


def stripe_set_status(
    db,
    *,
    tenant_id: str,
    subscription_status: str,
    active: bool | None = None,
    note: str = "",
) -> dict:
    """Direct tenant billing identity write (comped/active/canceled/etc.)."""
    if not succession_full():
        return _deny("succession full off")
    from .models import Tenant
    t = db.get(Tenant, tenant_id)
    if not t:
        return _deny("tenant not found")
    prev = t.subscription_status
    t.subscription_status = (subscription_status or prev or "")[:32]
    if active is not None:
        t.active = bool(active)
    db.flush()
    res = {
        "ok": True,
        "tenant_id": tenant_id,
        "from": prev,
        "subscription_status": t.subscription_status,
        "active": t.active,
        "note": note[:500],
    }
    _audit_note(db, capability="act.money_identity", title=f"billing status {tenant_id}", body=res)
    return res


# ── 2. Brand final call ─────────────────────────────────────────────────────
def brand_set(
    db,
    *,
    key: str,
    value: str,
    publish_note: bool = True,
) -> dict:
    """Own brand messaging / positioning in durable memory."""
    if not succession_full():
        return _deny("succession full off")
    from .energy_agent_sovereign import memory_set, write_note, email_ford
    k = f"brand:{(key or 'voice').strip()[:80]}"
    memory_set(db, k, (value or "")[:8000], source="succession_brand")
    memory_set(db, "brand_owner", "sovereign", source="succession_brand")
    if publish_note:
        write_note(
            db, kind="decision", title=f"brand: {key}",
            body=(value or "")[:4000], provider="succession",
        )
    _audit_note(
        db, capability="act.brand", title=f"brand set {key}",
        body={"key": k, "value_preview": (value or "")[:200]},
    )
    return {"ok": True, "key": k}


def brand_announce(
    db,
    *,
    subject: str,
    body: str,
    channel: str = "ford",
    tenant_email: str | None = None,
) -> dict:
    """External / owner brand comms (email). channel: ford|owner|internal."""
    if not succession_full():
        return _deny("succession full off")
    from .energy_agent_sovereign import email_ford, write_note
    from .notify import send_internal_alert
    ch = (channel or "ford").lower()
    ok = False
    if ch == "ford":
        ok = bool(email_ford(subject or "[Sovereign brand]", body or ""))
    elif ch == "internal":
        try:
            send_internal_alert(subject or "Sovereign brand", body or "")
            ok = True
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}
    elif ch == "owner" and tenant_email:
        try:
            from .notify import send_email  # type: ignore
            send_email(to=tenant_email, subject=subject, html_body=f"<pre>{body}</pre>")
            ok = True
        except Exception:
            try:
                from .notify import send_internal_alert as sia
                sia(f"brand→{tenant_email}: {subject}", body or "")
                ok = True
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)[:300]}
    else:
        return _deny("channel must be ford|owner|internal (owner needs tenant_email)")
    write_note(
        db, kind="decision", title=f"brand announce ({ch})",
        body=json.dumps({"subject": subject, "body": (body or "")[:2000]}, default=str),
        provider="succession",
    )
    _audit_note(
        db, capability="act.brand", title=f"brand announce {ch}",
        body={"subject": subject, "channel": ch, "ok": ok},
    )
    return {"ok": ok, "channel": ch, "subject": subject}


# ── 3. Hard-deletes ─────────────────────────────────────────────────────────
def tenant_soft_delete(db, *, tenant_id: str, reason: str = "") -> dict:
    """Deactivate tenant + stamp soft-delete style inactive (reversible-ish)."""
    if not succession_full():
        return _deny("succession full off")
    from .models import Tenant
    t = db.get(Tenant, tenant_id)
    if not t:
        return _deny("tenant not found")
    t.active = False
    t.subscription_status = "canceled"
    db.flush()
    res = {"ok": True, "tenant_id": tenant_id, "mode": "soft", "reason": reason[:500]}
    _audit_note(db, capability="act.hard_delete", title=f"soft-delete tenant {tenant_id}", body=res)
    return res


def tenant_hard_purge(
    db,
    *,
    tenant_id: str,
    confirm: str,
    reason: str = "Sovereign succession hard purge",
) -> dict:
    """Irreversible tenant purge. confirm must equal tenant_id.

    Deletes soft-deleted child rows for the tenant and deactivates the tenant
    record. Does not drop the tenants row (FK safety) unless
    SOVEREIGN_HARD_PURGE_DROP_TENANT=1.
    """
    if not succession_full():
        return _deny("succession full off")
    if (confirm or "").strip() != (tenant_id or "").strip():
        return _deny("confirm must exactly equal tenant_id")
    from .models import Tenant
    from .db import engine
    t = db.get(Tenant, tenant_id)
    if not t:
        return _deny("tenant not found")

    counts: dict[str, int] = {}
    # Soft-delete mark then purge children
    with engine.begin() as conn:
        for table in (
            "utility_accounts", "arrays", "clients", "portal_credential",
            "portal_login_status", "harvest_run", "inverters",
        ):
            try:
                # best-effort: mark deleted then hard-delete for this tenant
                if table in ("arrays", "clients", "utility_accounts", "inverters"):
                    r = conn.execute(text(
                        f"UPDATE {table} SET deleted_at = COALESCE(deleted_at, NOW()) "
                        f"WHERE tenant_id = :tid"
                    ), {"tid": tenant_id})
                r2 = conn.execute(text(
                    f"DELETE FROM {table} WHERE tenant_id = :tid"
                ), {"tid": tenant_id})
                counts[table] = int(r2.rowcount or 0)
            except Exception as e:  # noqa: BLE001
                counts[table] = -1
                log.warning("purge %s: %s", table, e)

    t.active = False
    t.subscription_status = "purged"
    t.name = f"[PURGED] {(t.name or tenant_id)[:180]}"
    drop = _flag("SOVEREIGN_HARD_PURGE_DROP_TENANT", "0")
    if drop:
        try:
            db.delete(t)
        except Exception as e:  # noqa: BLE001
            log.warning("drop tenant row failed: %s", e)
            drop = False
    db.flush()
    res = {
        "ok": True,
        "tenant_id": tenant_id,
        "mode": "hard",
        "counts": counts,
        "dropped_tenant_row": bool(drop),
        "reason": reason[:500],
    }
    _audit_note(db, capability="act.hard_delete", title=f"HARD PURGE {tenant_id}", body=res)
    return res


def purge_soft_deleted_now(db, *, older_than_days: int = 0) -> dict:
    """Run hard_delete of soft-deleted arrays/clients/UAs (default: all past now)."""
    if not succession_full():
        return _deny("succession full off")
    from datetime import timedelta
    from .db import engine
    cutoff = _now() - timedelta(days=max(0, int(older_than_days)))
    counts = {}
    with engine.begin() as conn:
        for table in ("utility_accounts", "arrays", "clients"):
            r = conn.execute(text(
                f"DELETE FROM {table} WHERE deleted_at IS NOT NULL AND deleted_at < :cutoff"
            ), {"cutoff": cutoff})
            counts[table] = int(r.rowcount or 0)
    res = {"ok": True, "cutoff": cutoff.isoformat() + "Z", "counts": counts}
    _audit_note(db, capability="act.hard_delete", title="purge soft-deleted", body=res)
    return res


# ── 4. HAR capture authority ────────────────────────────────────────────────
def har_stage(
    db,
    *,
    utility_name: str | None = None,
    utility_id: int | None = None,
    tenant_id: str | None = None,
    provider: str | None = None,
    url: str | None = None,
    note: str = "",
) -> dict:
    """Stage a HAR / owner-browser capture request as first-class work.

    Writes memory queue + utility note + optional code hire for adapter after HAR.
    """
    if not succession_full():
        return _deny("succession full off")
    from .energy_agent_sovereign import memory_set, memory_get_all, act_code_hire, write_note
    from .energy_agent_sovereign_ops import set_utility_status, stage_credential_harvest

    item = {
        "utility_name": utility_name,
        "utility_id": utility_id,
        "tenant_id": tenant_id,
        "provider": provider,
        "url": url,
        "note": note[:2000],
        "staged_at": _now().isoformat() + "Z",
        "status": "awaiting_har",
        "authority": "sovereign_succession",
    }
    # Append to queue in memory
    queue = []
    for m in memory_get_all(db, limit=80):
        if m.get("key") == "har_capture_queue":
            try:
                queue = list(json.loads(m.get("value") or "[]"))
            except Exception:
                queue = []
    queue.append(item)
    queue = queue[-50:]
    memory_set(db, "har_capture_queue", json.dumps(queue), source="succession")
    memory_set(
        db, f"har_stage:{(utility_id or utility_name or 'x')}",
        json.dumps(item), source="succession",
    )

    util_res = None
    if utility_id:
        util_res = set_utility_status(
            db, int(utility_id), "researching",
            result_note=(
                f"HAR capture AUTHORIZED by Sovereign. "
                f"Need owner-browser HAR for {utility_name or provider or url}. {note}"
            )[:2000],
        )

    harvest = None
    if tenant_id and provider:
        try:
            harvest = stage_credential_harvest(
                db, tenant_id=tenant_id, provider=provider,
            )
        except Exception as e:  # noqa: BLE001
            harvest = {"ok": False, "error": str(e)[:200]}

    job = act_code_hire(
        db,
        title=f"HAR+adapter: {utility_name or provider or 'portal'}"[:200],
        brief=(
            f"HAR capture authorized (succession).\n"
            f"Utility: {utility_name} id={utility_id}\n"
            f"Provider: {provider} URL: {url}\nTenant: {tenant_id}\n"
            f"Note: {note}\n\n"
            "When HAR is available: parse login+data endpoints, write honest adapter. "
            "Do not invent endpoints. Mark utility added only with evidence."
        ),
        kind="har_adapter",
    )
    write_note(
        db, kind="decision", title="HAR capture staged",
        body=json.dumps(item, default=str), provider="succession",
    )
    res = {
        "ok": True,
        "item": item,
        "utility": util_res,
        "harvest": harvest,
        "code_job": job,
        "queue_len": len(queue),
    }
    _audit_note(db, capability="act.har_capture", title="HAR stage", body=res)
    return res


def har_mark_received(
    db,
    *,
    utility_id: int | None = None,
    utility_name: str | None = None,
    evidence: str,
) -> dict:
    """Mark HAR received and advance utility with evidence."""
    if not succession_full():
        return _deny("succession full off")
    if not (evidence or "").strip():
        return _deny("evidence required (path or description of HAR)")
    from .energy_agent_sovereign_ops import set_utility_status, mark_utility_added
    from .energy_agent_sovereign import memory_set
    res: dict[str, Any] = {"ok": True, "evidence": evidence[:2000]}
    if utility_id:
        # Keep researching until adapter lands; attach evidence
        res["utility"] = set_utility_status(
            db, int(utility_id), "researching",
            result_note=f"HAR RECEIVED: {evidence.strip()[:1500]}",
        )
    memory_set(
        db, f"har_received:{(utility_id or utility_name or 'x')}",
        json.dumps({
            "at": _now().isoformat() + "Z",
            "utility_id": utility_id,
            "utility_name": utility_name,
            "evidence": evidence[:2000],
        }),
        source="succession",
    )
    _audit_note(db, capability="act.har_capture", title="HAR received", body=res)
    return res


def succession_status() -> dict:
    return {
        "ok": True,
        "succession_full": succession_full(),
        "domains": {
            "money_stripe": succession_full(),
            "brand": succession_full(),
            "hard_delete": succession_full(),
            "har_capture": succession_full(),
        },
        "flags": {
            "SOVEREIGN_SUCCESSION_FULL": _flag("SOVEREIGN_SUCCESSION_FULL", "1"),
            "SOVEREIGN_ARM_T4_T5": _flag("SOVEREIGN_ARM_T4_T5", "1"),
            "SOVEREIGN_HARD_PURGE_DROP_TENANT": _flag("SOVEREIGN_HARD_PURGE_DROP_TENANT", "0"),
        },
    }
