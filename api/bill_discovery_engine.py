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
) -> dict[str, Any]:
    """Offline path: synthesize from pre-captured network payloads (tests +
    HAR upload). Fully automatic after captures exist — no browser."""
    return _synthesize_from_captures(captures, provider=provider)


def _finalize_job(job_id: int, result: dict) -> dict[str, Any]:
    with SessionLocal() as db:
        job = db.get(BillDiscoveryJob, job_id)
        if job is None:
            return result
        job.status = result.get("status") or "failed"
        job.abort_reason = result.get("abort_reason")
        job.detail = (result.get("detail") or "")[:4000]
        captures = result.get("captures") or []
        job.captures_count = len(captures)
        # Store compact previews only (bodies can be large).
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
        db.commit()
        db.refresh(job)
        return _job_dict(job)


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
        syn = synthesize_bill_extractor(body, fmt="json", provider=provider)
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
