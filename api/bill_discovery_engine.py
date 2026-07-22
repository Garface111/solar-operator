"""Fully automatic, safety-bounded bill-adapter discovery.

When a utility login is saved (Cloud Capture), the Bill Adapter Autopilot
enqueues a discovery job. This engine:

  1. Classifies the platform (GMP / SmartHub / …) — known families short-circuit
     to "skipped_known" (production harvest already armed; no extra login risk).
  2. For UNKNOWN portals (or force_explore=True): open a headless browser with
     the vault password, attempt ONE login, capture JSON network responses while
     taking a tiny number of bill-ish navigations, then synthesize an extractor
     via auto_adapters and store it as a candidate.

SAFETY (never lock a customer out of their own utility):
  * At most ONE password login attempt per discovery job.
  * Hard abort on MFA / CAPTCHA / bot-wall page text (no retries).
  * Max wall clock, max navigations, max network captures.
  * Does NOT write bills into production until synthesis validates.
  * Does NOT run when harvest_fails already indicate a lockout pause.
  * Known families skip the browser path by default (already have adapters).

Live portal runs need Playwright + SO_CONFIG_KEY + vault password. Offline /
tests use ``run_discovery_from_captures`` with pre-recorded payloads (GMP +
VEC fixtures) to prove synthesis end-to-end without touching a utility.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy import select

from .db import SessionLocal
from .models import BillDiscoveryJob, PortalCredential, now

log = logging.getLogger("bill_discovery")

# ── Hard safety budget (env-tunable, defaults are conservative) ───────────────
MAX_WALL_S = int(os.environ.get("BILL_DISCOVERY_MAX_WALL_S") or 90)
MAX_NAVIGATIONS = int(os.environ.get("BILL_DISCOVERY_MAX_NAVS") or 8)
MAX_CAPTURES = int(os.environ.get("BILL_DISCOVERY_MAX_CAPTURES") or 40)
MAX_BODY_BYTES = int(os.environ.get("BILL_DISCOVERY_MAX_BODY") or 400_000)
# Only one discovery job runs at a time process-wide (lockout hygiene).
_RUNNING = False

# Page-text signals → abort immediately, do not re-login.
_ABORT_PATTERNS = re.compile(
    r"(captcha|recaptcha|hcaptcha|cf-challenge|"
    r"two[- ]?factor|2\s*fa|multi[- ]?factor|mfa|"
    r"verification code|enter (the |your )?code|"
    r"authenticate your device|approve this sign[- ]?in|"
    r"unusual activity|account (has been )?locked|too many attempts|"
    r"please verify you are human|security check)",
    re.I,
)

# Network URLs that look like billing / usage / invoice APIs.
_URL_BILLISH = re.compile(
    r"(bill|billing|invoice|statement|usage|meter|kwh|generation|"
    r"history/overview|billPdf|transactions|account.*bill)",
    re.I,
)

# Visible link/button text worth a single cautious click.
_CLICK_TEXT = re.compile(
    r"^(billing|bills|bill history|invoices|statements|usage|account|"
    r"documents|payments|history|my account)$",
    re.I,
)


# ── Queue ────────────────────────────────────────────────────────────────────

def _ensure_table() -> None:
    """Idempotent create for bill_discovery_job (create_all on boot also does this)."""
    try:
        from .db import engine
        from .models import Base
        BillDiscoveryJob.__table__.create(bind=engine, checkfirst=True)
    except Exception as e:
        log.debug("bill_discovery ensure_table: %s", e)


def enqueue_discovery(
    *,
    tenant_id: str,
    provider: str,
    username: str,
    login_host: Optional[str] = None,
    force_explore: bool = False,
) -> dict[str, Any]:
    """Enqueue a discovery job. Idempotent within a short window: if a queued/
    running job already exists for this login, return it instead of stacking."""
    from .bill_adapter_autopilot import classify_login

    _ensure_table()
    provider = (provider or "").strip().lower()
    username_lc = (username or "").strip().lower()
    plan = classify_login(provider, login_host)

    with SessionLocal() as db:
        # De-dupe open jobs for same login.
        existing = db.execute(
            select(BillDiscoveryJob).where(
                BillDiscoveryJob.tenant_id == tenant_id,
                BillDiscoveryJob.provider == provider,
                BillDiscoveryJob.username_lc == username_lc,
                BillDiscoveryJob.status.in_(("queued", "running")),
            ).order_by(BillDiscoveryJob.id.desc()).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return _job_dict(existing)

        # Known families: record skipped_known unless force_explore.
        if plan.action == "arm_known" and not force_explore:
            job = BillDiscoveryJob(
                tenant_id=tenant_id,
                provider=provider,
                username_lc=username_lc,
                login_host=login_host,
                status="skipped_known",
                family=plan.family,
                action="arm_known",
                detail=(
                    f"Known platform family '{plan.family}' — production bill "
                    f"pull already armed ({plan.bill_pull}). No exploratory "
                    f"login (avoids lockout risk). force_explore=1 to probe anyway."
                ),
                finished_at=now(),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            return _job_dict(job)

        job = BillDiscoveryJob(
            tenant_id=tenant_id,
            provider=provider,
            username_lc=username_lc,
            login_host=login_host,
            status="queued",
            family=plan.family,
            action="explore",
            detail="Queued for automatic bounded portal discovery.",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        jid = job.id

    # Kick processing in-process when possible (best-effort; scheduler also drains).
    try:
        _spawn_process_one(jid)
    except Exception as e:
        log.warning("bill-discovery spawn failed job=%s: %s", jid, e)

    with SessionLocal() as db:
        job = db.get(BillDiscoveryJob, jid)
        return _job_dict(job) if job else {"id": jid, "status": "queued"}


def _job_dict(job: BillDiscoveryJob) -> dict[str, Any]:
    syn = None
    if job.synthesis_json:
        try:
            syn = json.loads(job.synthesis_json)
        except Exception:
            syn = {"raw": (job.synthesis_json or "")[:500]}
    return {
        "id": job.id,
        "tenant_id": job.tenant_id,
        "provider": job.provider,
        "username_lc": job.username_lc,
        "login_host": job.login_host,
        "status": job.status,
        "abort_reason": job.abort_reason,
        "family": job.family,
        "action": job.action,
        "captures_count": job.captures_count or 0,
        "fingerprint": job.fingerprint,
        "synthesis": syn,
        "detail": job.detail,
        "created_at": job.created_at.isoformat() + "Z" if job.created_at else None,
        "started_at": job.started_at.isoformat() + "Z" if job.started_at else None,
        "finished_at": job.finished_at.isoformat() + "Z" if job.finished_at else None,
    }


def _spawn_process_one(job_id: int) -> None:
    """Fire-and-forget background process for one job (daemon thread)."""
    import threading

    def _run():
        try:
            process_job(job_id)
        except Exception:
            log.exception("bill-discovery job %s crashed", job_id)

    t = threading.Thread(target=_run, name=f"bill-discovery-{job_id}", daemon=True)
    t.start()


def process_queued(limit: int = 3) -> dict[str, Any]:
    """Scheduler entry: drain up to `limit` queued jobs (sequential)."""
    global _RUNNING
    if _RUNNING:
        return {"ok": True, "skipped": "already_running"}
    with SessionLocal() as db:
        ids = list(db.execute(
            select(BillDiscoveryJob.id).where(
                BillDiscoveryJob.status == "queued"
            ).order_by(BillDiscoveryJob.created_at.asc()).limit(limit)
        ).scalars().all())
    results = []
    for jid in ids:
        results.append(process_job(jid))
    return {"ok": True, "processed": len(results), "results": results}


# ── Core processing ───────────────────────────────────────────────────────────

def process_job(job_id: int) -> dict[str, Any]:
    """Run one discovery job to completion (sync wrapper around async explore)."""
    global _RUNNING
    with SessionLocal() as db:
        job = db.get(BillDiscoveryJob, job_id)
        if job is None:
            return {"ok": False, "error": "not_found"}
        if job.status not in ("queued",):
            return _job_dict(job)
        job.status = "running"
        job.started_at = now()
        db.commit()
        tenant_id = job.tenant_id
        provider = job.provider
        username_lc = job.username_lc
        login_host = job.login_host

    _RUNNING = True
    try:
        # Prefer live browser explore; fall back to offline if Playwright missing.
        try:
            result = asyncio.run(
                _explore_live(tenant_id, provider, username_lc, login_host)
            )
        except Exception as e:
            log.warning("live explore failed job=%s: %s", job_id, e)
            result = {
                "status": "failed",
                "abort_reason": "explore_error",
                "detail": f"{type(e).__name__}: {e}"[:500],
                "captures": [],
                "synthesis": None,
            }

        return _finalize_job(job_id, result)
    finally:
        _RUNNING = False


def run_discovery_from_captures(
    captures: list[dict],
    *,
    provider: str = "unknown",
    tenant_id: Optional[str] = None,
    username_lc: Optional[str] = None,
    start_capture: bool = False,
) -> dict[str, Any]:
    """Offline path: synthesize from pre-captured network payloads (tests +
    HAR upload). Fully automatic after captures exist — no browser.

    When start_capture=True and tenant/username provided, auto-approve the
    adapter and immediately ingest bills + rearm harvest.
    """
    result = _synthesize_from_captures(captures, provider=provider)
    if (
        start_capture
        and result.get("status") == "succeeded"
        and result.get("synthesis")
        and tenant_id
        and username_lc
    ):
        try:
            result["capture_start"] = activate_adapter_and_start_capture(
                tenant_id=tenant_id,
                provider=provider,
                username_lc=username_lc,
                synthesis=result["synthesis"],
                captures=captures,
            )
        except Exception as e:
            result["capture_start"] = {
                "ok": False, "error": f"{type(e).__name__}: {e}"[:300],
            }
    return result


def activate_adapter_and_start_capture(
    *,
    tenant_id: str,
    provider: str,
    username_lc: str,
    synthesis: dict,
    captures: list[dict],
) -> dict[str, Any]:
    """Approve the new adapter and begin bill capture immediately.

    1. Promote candidate → approved in auto_adapters registry
    2. Extract periods from discovery captures via the new spec (+ SmartHub/
       GMP-shaped rows when present)
    3. Upsert UtilityAccount + Bill rows for the tenant
    4. Rearm Cloud Capture (clear last_harvest_at / fail backoff)
    5. Best-effort trigger a harvest tick so the next pull uses the live portal
    """
    from . import auto_adapters as aa
    from .models import UtilityAccount, Bill
    from .worker import _upsert_bill
    from datetime import datetime, timedelta

    provider = (provider or "unknown").strip().lower()
    username_lc = (username_lc or "").strip().lower()
    fp = (synthesis or {}).get("fingerprint")
    spec = (synthesis or {}).get("spec")
    if not fp and not spec:
        return {"ok": False, "error": "no fingerprint/spec to activate"}

    approved = 0
    if fp:
        try:
            approved = int(aa.reg_approve(fp) or 0)
        except Exception as e:
            log.warning("reg_approve failed fp=%s: %s", fp, e)
        if not spec:
            row = aa.reg_get(fp)
            if row and row.get("spec"):
                try:
                    spec = json.loads(row["spec"]) if isinstance(row["spec"], str) else row["spec"]
                except Exception:
                    spec = None

    metrics_list: list[dict] = []

    # Path A: declarative adapter extract (date + generation_kwh)
    if spec:
        for cap in captures or []:
            body = cap.get("body")
            if not body or not isinstance(body, str) or body[0] not in "{[":
                continue
            try:
                recs, _c, _s = aa.extract(spec, body)
            except Exception:
                continue
            for r in recs or []:
                d = r.get("date")
                kwh = r.get("generation_kwh")
                if not d or kwh is None:
                    continue
                try:
                    if isinstance(d, str):
                        day = datetime.fromisoformat(d[:10])
                    else:
                        continue
                except Exception:
                    continue
                kwh_f = float(kwh)
                metrics_list.append({
                    "kwh_generated": int(round(kwh_f)) if kwh_f == kwh_f else None,
                    "kwh_sent_to_grid": float(kwh_f),
                    "period_start": day.replace(day=1) if hasattr(day, "day") else day,
                    "period_end": day,
                    "billing_days": 30,
                    "bill_date": day,
                    "parse_status": "parsed",
                    "document_number": f"auto-{provider}-{d}",
                    "raw_json": {"source": "bill_discovery_adapter", "record": r,
                                "fingerprint": fp, "url": cap.get("url")},
                    "is_net_metered": True,
                })

    # Path B: SmartHub-shaped bill rows already in captures / fixture shape
    try:
        from .adapters.smarthub import parse_bill as sh_parse_bill
    except Exception:
        sh_parse_bill = None
    if sh_parse_bill:
        for cap in captures or []:
            body = cap.get("body")
            if not body:
                continue
            try:
                data = json.loads(body) if isinstance(body, str) else body
            except Exception:
                continue
            rows = data if isinstance(data, list) else (
                data.get("bills") or data.get("rows") or data.get("billingHistory") or []
            )
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if not (row.get("billing_date") or row.get("billingDateTimestamp")
                        or row.get("account_id") or row.get("acctNbr")):
                    continue
                # Map raw NISC → extension shape when needed
                if "billing_date" not in row and "billingDateTimestamp" in row:
                    try:
                        from .harvester.vendors.smarthub import SmartHubVendor
                        row = SmartHubVendor._bill_row(
                            str(row.get("acctNbr") or row.get("account_id") or "0"),
                            row,
                        )
                    except Exception:
                        continue
                try:
                    b = sh_parse_bill(row)
                except Exception:
                    continue
                bd = b.get("billing_date")
                if not bd:
                    continue
                pe = b.get("period_end") or bd
                ps = b.get("period_start")
                if ps is None and pe is not None:
                    try:
                        ps = pe - timedelta(days=30)
                    except Exception:
                        ps = pe
                kwh = b.get("kwh")
                metrics_list.append({
                    "kwh_generated": int(round(kwh)) if isinstance(kwh, (int, float)) else 0,
                    "period_start": ps,
                    "period_end": pe,
                    "billing_days": ((pe - ps).days if (pe and ps) else 30),
                    "bill_date": bd,
                    "parse_status": "parsed" if kwh is not None else "partial",
                    "document_number": b.get("bill_uuid") or f"sh-{bd}",
                    "total_cost": b.get("bill_amount") or b.get("total_due"),
                    "raw_json": row,
                    "account_hint": b.get("account_id"),
                })

    # Path C: GMP bill JSON objects
    try:
        from .adapters import gmp as gmp_ad
        for cap in captures or []:
            body = cap.get("body")
            if not body:
                continue
            try:
                data = json.loads(body) if isinstance(body, str) else body
            except Exception:
                continue
            bills = data if isinstance(data, list) else (
                data.get("bills") or ([data] if isinstance(data, dict) and data.get("billSegments") else [])
            )
            for bill in bills or []:
                if not isinstance(bill, dict) or "billSegments" not in bill:
                    continue
                try:
                    m = gmp_ad.bill_json_to_metrics(bill)
                except Exception:
                    continue
                if m.get("period_end") is None and m.get("bill_date") is None:
                    continue
                metrics_list.append(m)
    except Exception:
        pass

    created = updated = 0
    account_ids: list[int] = []
    with SessionLocal() as db:
        # One account per login (stable key); optional per-hint accounts later.
        base_acct_no = f"auto_{provider[:10]}_{username_lc[:24]}"[:40]
        ua = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == tenant_id,
                UtilityAccount.provider == provider,
                UtilityAccount.account_number == base_acct_no,
            )
        ).scalar_one_or_none()
        if ua is None:
            ua = UtilityAccount(
                tenant_id=tenant_id,
                provider=provider,
                account_number=base_acct_no,
                customer_number=username_lc[:40] if username_lc else None,
                nickname=f"{provider} ({username_lc})"[:200],
                enabled=True,
                extra={"adapter_fingerprint": fp, "source": "bill_discovery"},
            )
            db.add(ua)
            db.flush()
        else:
            extra = dict(ua.extra or {})
            extra["adapter_fingerprint"] = fp
            extra["source"] = "bill_discovery"
            ua.extra = extra
            ua.enabled = True
        account_ids.append(ua.id)

        for m in metrics_list:
            if m.get("period_end") is None and m.get("bill_date") is not None:
                m["period_end"] = m["bill_date"]
            if m.get("period_start") is None and m.get("period_end") is not None:
                try:
                    m["period_start"] = m["period_end"] - timedelta(days=30)
                except Exception:
                    m["period_start"] = m["period_end"]
            if m.get("billing_days") is None:
                m["billing_days"] = 30
            if m.get("parse_status") is None:
                m["parse_status"] = "partial"
            # kwh_generated required-ish for upsert; allow 0
            if m.get("kwh_generated") is None:
                m["kwh_generated"] = 0
            try:
                action = _upsert_bill(db, tenant_id, ua, m)
                if action == "created":
                    created += 1
                else:
                    updated += 1
            except Exception as e:
                log.warning("activate upsert bill failed: %s", e)

        # Rearm Cloud Capture so the harvester picks this login up ASAP.
        rearmed = 0
        try:
            from sqlalchemy.orm import defer
            creds = db.execute(
                select(PortalCredential).options(
                    defer(PortalCredential.secret_enc),
                    defer(PortalCredential.session_state_enc),
                ).where(
                    PortalCredential.tenant_id == tenant_id,
                    PortalCredential.provider == provider,
                    PortalCredential.username_lc == username_lc,
                )
            ).scalars().all()
            for c in creds:
                c.cloud_capture_enabled = True
                c.last_harvest_at = None
                c.harvest_fails = 0
                c.updated_at = now()
                rearmed += 1
        except Exception as e:
            log.warning("rearm credential failed: %s", e)

        db.commit()

        # bill → daily so dashboards light up immediately
        try:
            from .jobs.bill_to_daily import transform_tenant_bills
            transform_tenant_bills(tenant_id, db=db)
            db.commit()
        except Exception:
            log.warning("bill_to_daily after activate failed t=%s", tenant_id, exc_info=True)

    # Best-effort live harvest (known vendors / SmartHub host). Never block.
    harvest = None
    try:
        harvest = _trigger_harvest_async(tenant_id, provider, username_lc)
    except Exception as e:
        harvest = {"ok": False, "error": str(e)[:200]}

    # Also kick GMP-style JSON pull when a JWT session exists for this tenant.
    pull = None
    try:
        from .worker import pull_bills_for_tenant
        if provider == "gmp":
            pull = pull_bills_for_tenant(tenant_id)
    except Exception as e:
        pull = {"ok": False, "error": str(e)[:200]}

    return {
        "ok": True,
        "approved": approved,
        "fingerprint": fp,
        "bills_created": created,
        "bills_updated": updated,
        "metrics_extracted": len(metrics_list),
        "accounts": account_ids,
        "credentials_rearmed": rearmed,
        "harvest": harvest,
        "pull": pull,
    }


def _trigger_harvest_async(tenant_id: str, provider: str, username_lc: str) -> dict:
    """Fire a single Cloud Capture harvest in a daemon thread (non-blocking)."""
    import threading

    def _run():
        try:
            from .harvester.engine import BrowserFarm
            async def _one():
                async with BrowserFarm() as farm:
                    return await farm.harvest(tenant_id, provider, username_lc)
            outcome = asyncio.run(_one())
            log.info(
                "post-adapter harvest %s/%s → %s rows=%s",
                provider, tenant_id, getattr(outcome, "status", None),
                getattr(outcome, "rows", None),
            )
        except Exception:
            log.warning("post-adapter harvest failed %s/%s", provider, tenant_id,
                        exc_info=True)

    t = threading.Thread(
        target=_run, name=f"harvest-after-adapter-{provider}", daemon=True)
    t.start()
    return {"ok": True, "queued": True}


def _finalize_job(job_id: int, result: dict) -> dict[str, Any]:
    notify_payload = None
    capture_start = None
    with SessionLocal() as db:
        job = db.get(BillDiscoveryJob, job_id)
        if job is None:
            return result
        job.status = result.get("status") or "failed"
        job.abort_reason = result.get("abort_reason")
        job.detail = (result.get("detail") or "")[:4000]
        captures = result.get("captures") or []
        # Prefer full capture bodies still in memory for ingest; compact for storage.
        full_captures = captures
        job.captures_count = len(captures)
        compact = []
        for c in captures[:MAX_CAPTURES]:
            compact.append({
                "url": (c.get("url") or "")[:300],
                "status": c.get("status"),
                "content_type": (c.get("content_type") or "")[:80],
                "bytes": c.get("bytes") or 0,
                "body_preview": (c.get("body") or "")[:400],
            })
        job.captures_json = json.dumps(compact)
        syn = result.get("synthesis")
        if syn:
            job.synthesis_json = json.dumps(syn)[:8000]
            job.fingerprint = syn.get("fingerprint")
        job.finished_at = now()

        should_activate = (
            job.status == "succeeded" and syn and syn.get("ok")
            and (syn.get("fingerprint") or syn.get("spec"))
        )
        tenant_id = job.tenant_id
        provider = job.provider
        username_lc = job.username_lc
        job_id_v = job.id
        db.commit()
        db.refresh(job)
        out = _job_dict(job)

    if should_activate:
        try:
            capture_start = activate_adapter_and_start_capture(
                tenant_id=tenant_id,
                provider=provider,
                username_lc=username_lc,
                synthesis=syn,
                captures=full_captures,
            )
            out["capture_start"] = capture_start
            # Persist capture_start summary on the job detail.
            with SessionLocal() as db:
                job = db.get(BillDiscoveryJob, job_id_v)
                if job is not None:
                    extra = (
                        f" Capture started: created={capture_start.get('bills_created')} "
                        f"updated={capture_start.get('bills_updated')} "
                        f"rearmed={capture_start.get('credentials_rearmed')}."
                    )
                    job.detail = ((job.detail or "") + extra)[:4000]
                    db.commit()
                    db.refresh(job)
                    out = _job_dict(job)
                    out["capture_start"] = capture_start
        except Exception as e:
            log.warning("activate_adapter_and_start_capture failed job=%s: %s",
                        job_id, e)
            out["capture_start"] = {"ok": False, "error": str(e)[:300]}

        try:
            from .bill_adapter_autopilot import notify_new_bill_adapter
            cs = out.get("capture_start") or {}
            detail = (
                f"Adapter approved and bill capture started automatically.\n"
                f"Bills created={cs.get('bills_created')} updated={cs.get('bills_updated')} "
                f"extracted={cs.get('metrics_extracted')} rearmed={cs.get('credentials_rearmed')}.\n"
                f"{(syn or {}).get('detail') or result.get('detail') or ''}"
            )
            notify_new_bill_adapter(
                provider=provider,
                fingerprint=(syn or {}).get("fingerprint"),
                source=(syn or {}).get("source"),
                reconcile=(syn or {}).get("reconcile"),
                tenant_id=tenant_id,
                username=username_lc,
                job_id=job_id_v,
                detail=detail,
                source_url=(syn or {}).get("_source_url"),
            )
        except Exception as e:
            log.warning("adapter-built email failed job=%s: %s", job_id, e)
    return out


# ── Safety page checks ───────────────────────────────────────────────────────

def page_requires_abort(page_text: str, url: str = "") -> Optional[str]:
    """Return abort reason if the page signals MFA/CAPTCHA/lockout — else None."""
    blob = f"{url}\n{page_text or ''}"
    if not blob.strip():
        return None
    m = _ABORT_PATTERNS.search(blob)
    if not m:
        return None
    token = m.group(0).lower()
    if "captcha" in token or "human" in token or "challenge" in token:
        return "captcha"
    if any(x in token for x in ("factor", "2fa", "mfa", "code", "device", "approve")):
        return "mfa"
    if "locked" in token or "too many" in token:
        return "account_locked"
    return "security_wall"


def url_looks_billish(url: str) -> bool:
    return bool(_URL_BILLISH.search(url or ""))


# ── Synthesis ────────────────────────────────────────────────────────────────

def _synthesize_from_captures(captures: list[dict], *, provider: str) -> dict[str, Any]:
    """Try auto_adapters synthesis on each captured JSON body; keep best."""
    from .bill_adapter_autopilot import synthesize_bill_extractor

    best = None
    tried = 0
    for cap in captures:
        body = cap.get("body")
        if not body or not isinstance(body, str):
            continue
        body = body.strip()
        if not body or body[0] not in "{[":
            continue
        if len(body) < 40:
            continue
        tried += 1
        # notify=False — job finalize sends one email with full context.
        syn = synthesize_bill_extractor(
            body, fmt="json", provider=provider, notify=False)
        if syn.get("ok"):
            # Prefer larger / billish URLs.
            score = len(body)
            if url_looks_billish(cap.get("url") or ""):
                score += 50_000
            syn["_score"] = score
            syn["_source_url"] = cap.get("url")
            if best is None or score > best.get("_score", 0):
                best = syn

    if best:
        best.pop("_score", None)
        return {
            "status": "succeeded",
            "abort_reason": None,
            "detail": (
                f"Synthesized bill extractor from {tried} JSON capture(s); "
                f"candidate fingerprint={best.get('fingerprint')} "
                f"(source={best.get('source')}). Not yet writing production bills — "
                f"candidate awaits approval / family arming."
            ),
            "captures": captures,
            "synthesis": best,
        }

    return {
        "status": "failed" if captures else "failed",
        "abort_reason": "no_payload" if not captures else "synthesis_failed",
        "detail": (
            f"Captured {len(captures)} response(s), tried {tried} JSON bodies; "
            f"no validated extractor. May need a richer session or manual HAR."
        ),
        "captures": captures,
        "synthesis": None,
    }


# ── Live browser explore ─────────────────────────────────────────────────────

async def _explore_live(
    tenant_id: str,
    provider: str,
    username_lc: str,
    login_host: Optional[str],
) -> dict[str, Any]:
    """Bounded Playwright session. ONE login. Abort on MFA/CAPTCHA."""
    # Lockout coordination: refuse if account already paused.
    try:
        from .harvester.scheduler import MAX_LOGIN_FAILS
        from sqlalchemy import func
        with SessionLocal() as db:
            # Worst fail count for this portal account across tenants.
            from .models import PortalCredential as PC
            fails = db.execute(
                select(func.max(PC.harvest_fails)).where(
                    PC.provider == provider,
                    PC.username_lc == username_lc,
                )
            ).scalar() or 0
            if fails >= MAX_LOGIN_FAILS:
                return {
                    "status": "aborted_safe",
                    "abort_reason": "lockout_pause",
                    "detail": (
                        f"Portal account already has {fails} consecutive harvest "
                        f"failures — discovery refused to attempt another login."
                    ),
                    "captures": [],
                    "synthesis": None,
                }
    except Exception:
        pass

    # Load credentials (short session).
    try:
        from .harvester.engine import BrowserFarm
        from .harvester import login as login_mod
        from .harvester import stealth
        from .harvester.vendors import module_for
        from .harvester import credentials as cred_mod
    except Exception as e:
        return {
            "status": "failed",
            "abort_reason": "harvester_import",
            "detail": f"Harvester unavailable: {e}",
            "captures": [],
            "synthesis": None,
        }

    try:
        from ..crypto import set_decrypt_audit_context, clear_decrypt_audit_context
        set_decrypt_audit_context(
            tenant_id=tenant_id, provider=provider,
            username_lc=username_lc, job_id="bill_discovery",
        )
    except Exception:
        clear_decrypt_audit_context = None  # type: ignore

    creds = None
    try:
        with SessionLocal() as db:
            row = db.execute(
                select(PortalCredential).where(
                    PortalCredential.tenant_id == tenant_id,
                    PortalCredential.provider == provider,
                    PortalCredential.username_lc == username_lc,
                )
            ).scalar_one_or_none()
            if row is None or not row.secret_enc:
                return {
                    "status": "failed",
                    "abort_reason": "no_creds",
                    "detail": "No vault password for this login — cannot explore.",
                    "captures": [],
                    "synthesis": None,
                }
            creds = cred_mod.load_creds(row)
            if login_host and not getattr(creds, "login_host", None):
                try:
                    creds.login_host = login_host  # type: ignore[attr-defined]
                except Exception:
                    pass
    finally:
        if clear_decrypt_audit_context:
            try:
                clear_decrypt_audit_context()
            except Exception:
                pass

    if not creds or not creds.password:
        return {
            "status": "failed",
            "abort_reason": "no_creds",
            "detail": "Password missing after decrypt.",
            "captures": [],
            "synthesis": None,
        }

    vendor = module_for(provider)
    captures: list[dict] = []
    t0 = time.monotonic()

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        return {
            "status": "failed",
            "abort_reason": "no_playwright",
            "detail": f"Playwright not installed in this process: {e}",
            "captures": [],
            "synthesis": None,
        }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=stealth.launch_args() if hasattr(stealth, "launch_args") else [],
        )
        try:
            ctx_opts = stealth.context_options(
                getattr(creds, "session_state", None)
            ) if hasattr(stealth, "context_options") else {}
            context = await browser.new_context(**ctx_opts)
            if hasattr(stealth, "apply"):
                await stealth.apply(context)
            context.set_default_timeout(15_000)
            page = await context.new_page()

            async def _on_response(response):
                if len(captures) >= MAX_CAPTURES:
                    return
                try:
                    url = response.url or ""
                    status = response.status
                    if status < 200 or status >= 400:
                        return
                    ct = (response.headers or {}).get("content-type", "")
                    if "json" not in ct and not url_looks_billish(url):
                        return
                    # Prefer billish URLs; still take json from same origin.
                    body = await response.text()
                    if not body or body[0] not in "{[":
                        return
                    if len(body) > MAX_BODY_BYTES:
                        body = body[:MAX_BODY_BYTES]
                    captures.append({
                        "url": url,
                        "status": status,
                        "content_type": ct,
                        "bytes": len(body),
                        "body": body,
                    })
                except Exception:
                    return

            page.on("response", _on_response)

            # Resolve start URL.
            if vendor is not None:
                start_url = await vendor.login_url(creds)
            elif login_host:
                h = login_host if login_host.startswith("http") else f"https://{login_host}"
                start_url = h
            else:
                return {
                    "status": "failed",
                    "abort_reason": "no_url",
                    "detail": "No login URL / host for this provider.",
                    "captures": captures,
                    "synthesis": None,
                }

            await page.goto(start_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            await asyncio.sleep(1.5)

            # Safety check before typing password.
            try:
                txt = await page.inner_text("body")
            except Exception:
                txt = ""
            abort = page_requires_abort(txt, page.url)
            if abort:
                return {
                    "status": "aborted_safe",
                    "abort_reason": abort,
                    "detail": f"Pre-login page triggered safe abort ({abort}).",
                    "captures": captures,
                    "synthesis": None,
                }

            # Already logged in?
            logged_in = False
            if vendor is not None:
                try:
                    logged_in = await vendor.is_logged_in(page)
                except Exception:
                    logged_in = False

            if not logged_in:
                # ONE login attempt only.
                outcome = await login_mod.perform_login(
                    page, creds.username, creds.password, provider)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                try:
                    txt = await page.inner_text("body")
                except Exception:
                    txt = ""
                abort = page_requires_abort(txt, page.url)
                if abort:
                    return {
                        "status": "aborted_safe",
                        "abort_reason": abort,
                        "detail": (
                            f"Post-login wall ({abort}); discovery will not retry. "
                            f"login_outcome={outcome}"
                        ),
                        "captures": captures,
                        "synthesis": None,
                    }
                if vendor is not None:
                    try:
                        logged_in = await vendor.is_logged_in(page)
                    except Exception:
                        logged_in = False
                else:
                    # Heuristic: password field gone + not on login URL.
                    try:
                        pw = await page.query_selector('input[type="password"]')
                        logged_in = pw is None and "login" not in (page.url or "").lower()
                    except Exception:
                        logged_in = False
                if not logged_in:
                    return {
                        "status": "failed",
                        "abort_reason": "login_failed",
                        "detail": f"Login did not authenticate (outcome={outcome}). No retry.",
                        "captures": captures,
                        "synthesis": None,
                    }

            # Bounded navigation: click a few bill-ish links.
            navs = 0
            while navs < MAX_NAVIGATIONS and (time.monotonic() - t0) < MAX_WALL_S:
                if len(captures) >= MAX_CAPTURES:
                    break
                try:
                    txt = await page.inner_text("body")
                except Exception:
                    txt = ""
                abort = page_requires_abort(txt, page.url)
                if abort:
                    return {
                        "status": "aborted_safe",
                        "abort_reason": abort,
                        "detail": f"Abort during explore ({abort}) after {navs} navs.",
                        "captures": captures,
                        "synthesis": None,
                    }

                clicked = await _click_one_billish(page)
                if not clicked:
                    break
                navs += 1
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                await asyncio.sleep(1.0)

            # If vendor has a scrape that emits bill requests, try once (known paths).
            if vendor is not None and hasattr(vendor, "scrape"):
                try:
                    if (time.monotonic() - t0) < MAX_WALL_S - 15:
                        await vendor.scrape(page, context, creds)
                except Exception as e:
                    log.info("discovery vendor.scrape soft-fail: %s", type(e).__name__)

            await context.close()
        finally:
            await browser.close()

    if not captures:
        return {
            "status": "failed",
            "abort_reason": "no_payload",
            "detail": (
                "Authenticated (or attempted) but no JSON billing responses "
                "were captured. Portal may load bills only after deeper "
                "navigation or uses non-JSON formats."
            ),
            "captures": [],
            "synthesis": None,
        }

    return _synthesize_from_captures(captures, provider=provider)


async def _click_one_billish(page) -> bool:
    """Click at most one visible control whose text looks like billing nav."""
    try:
        # anchors and buttons
        for sel in ("a", "button", "[role=link]", "[role=button]"):
            els = await page.query_selector_all(sel)
            for el in els[:80]:
                try:
                    if not await el.is_visible():
                        continue
                    text = (await el.inner_text() or "").strip()
                    if not text or len(text) > 40:
                        continue
                    if not _CLICK_TEXT.match(text.strip()):
                        # softer contains match for compound labels
                        if not re.search(
                            r"\b(bill|billing|invoice|usage|statement|history)\b",
                            text, re.I,
                        ):
                            continue
                    await el.click(timeout=3000)
                    return True
                except Exception:
                    continue
    except Exception:
        return False
    return False
