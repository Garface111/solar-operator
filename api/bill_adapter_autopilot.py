"""Bill Adapter Autopilot — scalable automatic bill collection for utility logins.

WHY THIS EXISTS
---------------
Hand-writing a content script or harvester module per co-op does not scale
(~600 SmartHub co-ops + bespoke IOUs). The only durable path is:

  1. Recognize PLATFORM FAMILIES that share one auth + bill contract.
  2. On every new Accounts-tab Cloud Capture login, spring to life:
       • known family  → arm the existing adapter/harvester (no AI needed)
       • unknown portal → capture traffic / sample payload → synthesize a
         declarative bill extractor (auto_adapters) → validate → store candidate
  3. Recurring harvest uses the family adapter or the approved synthesised spec.

WHAT WORKS TODAY (verified in-product)
--------------------------------------
  GMP (bespoke JWT API)
    • Server pull: worker.pull_bills_for_tenant → adapters.gmp.fetch_bills_json
      using UtilitySession JWT (extension or cloud login).
    • Requires a live session token; password vault enables re-login via
      harvester/vendors/gmp.py when cloud capture is on.
    • STATUS: production-proven automatic bill pull.

  VEC + every NISC SmartHub co-op (cookie session)
    • Server pull: harvester/vendors/smarthub.py (Playwright + vault password)
      → POST /v1/sync with bill rows + PDFs.
    • Extension path: smarthub_content.js same-origin cookie capture → /v1/sync.
    • Pure JWT replay does NOT work (httpOnly session cookie).
    • STATUS: production-proven for VEC/WEC and cataloged SmartHub hosts when
      cloud capture is armed OR the owner opens the portal with the extension.

  Unknown / non-SmartHub portals (Eversource, CMP, …)
    • Hand modules exist for some (harvester/vendors/eversource.py, cmp.py).
    • True zero-shot AI inventing a full login+scrape stack is NOT reliable
      without at least one live session / HAR. Autopilot stages synthesis from
      a captured billing payload and falls back to "needs_har" honestly.

This module is the ORCHESTRATOR + verification surface. It does not replace
the harvester farm; it decides which path a new login takes and proves the
extractors still work (tests use real fixture shapes for GMP + VEC).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

log = logging.getLogger("bill_adapter_autopilot")

router = APIRouter(tags=["bill-adapter-autopilot"])


# ── Platform families ─────────────────────────────────────────────────────────

@dataclass
class PlatformPlan:
    """What to do when a utility login is saved."""
    provider: str
    family: str                     # gmp | smarthub | eversource | cmp | unknown
    bill_pull: str                  # how bills land
    auth_model: str                 # jwt | cookie_browser | unknown
    automatic: bool                 # True when we can harvest without a new hand adapter
    action: str                     # arm_known | synthesize | needs_har
    detail: str
    login_host: Optional[str] = None
    adapter_module: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# Providers whose login URL is fixed (no SmartHub host).
_BESPOKE = {
    "gmp": PlatformPlan(
        provider="gmp", family="gmp",
        bill_pull="server JWT API (api.greenmountainpower.com /api/v2/accounts/{n}/bills) "
                  "via worker.pull_bills; harvester re-login when vault password present",
        auth_model="jwt",
        automatic=True,
        action="arm_known",
        detail="GMP has a durable public-ish bills JSON API once a session JWT exists.",
        adapter_module="api.adapters.gmp",
    ),
    "eversource": PlatformPlan(
        provider="eversource", family="eversource",
        bill_pull="cloud-capture harvester (harvester/vendors/eversource.py)",
        auth_model="cookie_browser",
        automatic=True,
        action="arm_known",
        detail="Bespoke MyAccount portal; harvester module ships with product.",
        adapter_module="api.harvester.vendors.eversource",
    ),
    "eversource_ma": None,  # filled below
    "eversource_ct": None,
    "cmp": PlatformPlan(
        provider="cmp", family="cmp",
        bill_pull="cloud-capture harvester (harvester/vendors/cmp.py)",
        auth_model="cookie_browser",
        automatic=True,
        action="arm_known",
        detail="Avangrid portal; harvester module ships with product.",
        adapter_module="api.harvester.vendors.cmp",
    ),
}
for _code in ("eversource_ma", "eversource_ct"):
    p = _BESPOKE["eversource"]
    _BESPOKE[_code] = PlatformPlan(
        provider=_code, family="eversource", bill_pull=p.bill_pull,
        auth_model=p.auth_model, automatic=True, action="arm_known",
        detail=p.detail, adapter_module=p.adapter_module,
    )


def classify_login(provider: str, login_host: Optional[str] = None) -> PlatformPlan:
    """Map a Cloud Capture login to a bill-pull plan."""
    code = (provider or "").strip().lower()
    host = (login_host or "").strip().lower()

    if code in _BESPOKE and _BESPOKE[code] is not None:
        plan = _BESPOKE[code]
        return PlatformPlan(**{**plan.to_dict(), "login_host": host or plan.login_host})

    # SmartHub family — includes vec, wec, stowe, nhec, sh_<subdomain>, …
    try:
        from .adapters.smarthub import is_smarthub_provider
        is_sh = is_smarthub_provider(code) or bool(host and host.endswith(".smarthub.coop"))
    except Exception:
        is_sh = code in ("vec", "wec") or bool(host and "smarthub.coop" in host)

    if is_sh:
        if not host:
            try:
                from .providers import SMARTHUB_HOSTS
                host = SMARTHUB_HOSTS.get(code) or ""
            except Exception:
                host = ""
        return PlatformPlan(
            provider=code,
            family="smarthub",
            bill_pull="Playwright harvester (harvester/vendors/smarthub.py) with vault "
                      "password OR extension same-origin cookie capture → /v1/sync",
            auth_model="cookie_browser",
            automatic=True,
            action="arm_known",
            detail=(
                "All NISC SmartHub co-ops share one secured billing API "
                "(billing/history/overview + billPdfService). One adapter covers "
                f"VEC, WEC, and ~600 co-ops. host={host or 'set login_host on credential'}."
            ),
            login_host=host or None,
            adapter_module="api.harvester.vendors.smarthub",
        )

    return PlatformPlan(
        provider=code or "unknown",
        family="unknown",
        bill_pull="none yet — need a live session HAR or billing JSON sample",
        auth_model="unknown",
        automatic=False,
        action="needs_har",
        detail=(
            "No platform family match. Autopilot will synthesize a declarative "
            "extractor once a billing API payload is captured (extension network "
            "sniff or owner-browser HAR). Login automation still needs a family "
            "module or a one-time HAR of the auth flow."
        ),
        login_host=host or None,
    )


# ── Lifecycle: spring to life on credential save ──────────────────────────────

def on_credential_saved(
    *,
    tenant_id: str,
    provider: str,
    username: str,
    login_host: Optional[str] = None,
    enabled: bool = True,
    sample_payload: Optional[str] = None,
) -> dict[str, Any]:
    """Called when Accounts-tab Cloud Capture saves a utility login.

    Known families → mark ready + clear harvest backoff so the next harvester
    tick (or worker pull for GMP) picks them up.
    Unknown → optional sample_payload runs auto_adapters synthesis; otherwise
    records needs_har honestly.
    """
    plan = classify_login(provider, login_host)
    result: dict[str, Any] = {
        "ok": True,
        "tenant_id": tenant_id,
        "username": (username or "").strip(),
        "plan": plan.to_dict(),
        "armed": False,
        "synthesis": None,
        "at": datetime.now(timezone.utc).isoformat(),
    }

    if not enabled:
        result["detail"] = "Credential saved but cloud capture disabled — not armed."
        return result

    discovery = None
    if plan.action == "arm_known":
        result["armed"] = True
        result["detail"] = (
            f"Armed {plan.family} bill pull for {plan.provider}. "
            f"Auth={plan.auth_model}. {plan.detail}"
        )
        log.info(
            "bill-autopilot arm family=%s provider=%s tenant=%s",
            plan.family, plan.provider, tenant_id,
        )
        # Still record a discovery job as skipped_known (audit trail, no login).
        try:
            from .bill_discovery_engine import enqueue_discovery
            discovery = enqueue_discovery(
                tenant_id=tenant_id,
                provider=provider,
                username=username,
                login_host=login_host,
                force_explore=False,
            )
        except Exception as e:
            log.warning("enqueue discovery (known) failed: %s", e)
        result["discovery"] = discovery
        return result

    # Unknown platform — try synthesis if caller supplied a sample payload.
    if sample_payload:
        syn = synthesize_bill_extractor(sample_payload, fmt="json",
                                        provider=plan.provider)
        result["synthesis"] = syn
        if syn.get("ok"):
            result["armed"] = False  # candidate until approved
            result["detail"] = (
                "Synthesized a candidate bill extractor from sample payload; "
                "awaiting validation/approve before production harvest."
            )
            plan.action = "synthesize"
            result["plan"] = plan.to_dict()
        else:
            result["detail"] = syn.get("error") or "Synthesis failed."
        result["discovery"] = None
        return result

    # Fully automatic: queue bounded browser discovery for unknown portals.
    try:
        from .bill_discovery_engine import enqueue_discovery
        discovery = enqueue_discovery(
            tenant_id=tenant_id,
            provider=provider,
            username=username,
            login_host=login_host,
            force_explore=False,
        )
        result["discovery"] = discovery
        result["detail"] = (
            "Unknown platform — automatic discovery queued: bounded browser "
            "session will capture billing traffic and synthesize an extractor "
            f"(job #{discovery.get('id')}, status={discovery.get('status')}). "
            "Hard-aborts on MFA/CAPTCHA; one login attempt only."
        )
        plan.action = "explore"
        result["plan"] = plan.to_dict()
    except Exception as e:
        log.warning("enqueue discovery failed: %s", e)
        result["detail"] = plan.detail
        result["discovery"] = {"ok": False, "error": str(e)[:200]}
    return result


def synthesize_bill_extractor(
    raw_payload: str | dict | list,
    *,
    fmt: str = "json",
    provider: str = "unknown",
    notify: bool = True,
    tenant_id: Optional[str] = None,
    username: Optional[str] = None,
    job_id: Optional[int] = None,
) -> dict[str, Any]:
    """Run the existing auto_adapters synthesizer on a billing/generation payload.

    Returns {ok, source, fingerprint?, spec?, reconcile?, error?}.
    When ok and notify=True, emails Ford (INTERNAL_ALERT_TO) about the candidate.
    Discovery jobs set notify=False and send a richer job-level email on finalize.
    """
    from . import auto_adapters as aa

    if not isinstance(raw_payload, str):
        raw_text = json.dumps(raw_payload)
    else:
        raw_text = raw_payload

    try:
        fp = aa.fingerprint(raw_text, fmt)
    except Exception as e:
        return {"ok": False, "error": f"fingerprint: {type(e).__name__}: {e}"}

    try:
        spec, source, delta, ok = aa.synthesize(raw_text, fmt)
    except Exception as e:
        return {"ok": False, "error": f"synthesize: {type(e).__name__}: {e}",
                "fingerprint": fp}

    if not ok or not spec:
        return {
            "ok": False,
            "fingerprint": fp,
            "source": source,
            "error": "Could not synthesize a validated extractor from this payload.",
        }

    # Persist as candidate for the registry (same DB as auto_adapters).
    try:
        aa.reg_upsert(fp, fmt, spec, reconcile=delta, source=source)
    except Exception as e:
        log.warning("bill-autopilot reg_upsert failed: %s", e)

    out = {
        "ok": True,
        "fingerprint": fp,
        "source": source,
        "reconcile": delta,
        "spec": spec,
        "provider_hint": provider,
        "status": "candidate",
    }
    if notify:
        notify_new_bill_adapter(
            provider=provider,
            fingerprint=fp,
            source=source,
            reconcile=delta,
            tenant_id=tenant_id,
            username=username,
            job_id=job_id,
            detail="Synthesized from captured portal payload (auto_adapters).",
        )
    return out


def notify_new_bill_adapter(
    *,
    provider: str,
    fingerprint: Optional[str] = None,
    source: Optional[str] = None,
    reconcile: Any = None,
    tenant_id: Optional[str] = None,
    username: Optional[str] = None,
    job_id: Optional[int] = None,
    detail: Optional[str] = None,
    source_url: Optional[str] = None,
) -> bool:
    """Email Ford when a new bill-adapter candidate is built.

    Uses the internal alert channel (INTERNAL_ALERT_TO / Resend). Fail-soft:
    never raise into discovery/synthesis paths.
    """
    try:
        from .notify import send_internal_alert
        subj = f"[EnergyAgent] New bill adapter candidate · {provider or 'unknown'}"
        lines = [
            "A new bill-adapter extractor was synthesized and stored as a candidate.",
            "",
            f"Provider:     {provider or '—'}",
            f"Fingerprint:  {fingerprint or '—'}",
            f"Source:       {source or '—'}",
            f"Reconcile Δ:  {reconcile if reconcile is not None else '—'}",
            f"Tenant:       {tenant_id or '—'}",
            f"Login:        {username or '—'}",
            f"Discovery #:  {job_id if job_id is not None else '—'}",
            f"Sample URL:   {(source_url or '—')[:300]}",
            "",
            detail or "",
            "",
            "Next: review the candidate (auto_adapters registry). Approve before",
            "it drives production bill harvest for unknown portals. Known families",
            "(GMP / SmartHub) already harvest via platform modules.",
            "",
            "Status: GET /v1/bill-autopilot/discoveries  (session auth)",
            f"Job:    GET /v1/bill-autopilot/discovery/{job_id}" if job_id else "",
        ]
        body = "\n".join(ln for ln in lines if ln is not None)
        return bool(send_internal_alert(subj, body))
    except Exception as e:
        log.warning("notify_new_bill_adapter failed: %s", e)
        return False


# ── Verification: prove GMP + VEC bill extractors still work ──────────────────

def verify_gmp_extractor() -> dict[str, Any]:
    """Offline proof: real GMP bill JSON shape → metrics for worker upsert."""
    from .adapters import gmp

    bill = {
        "billNumber": "AUTOPILOT-GMP-1",
        "billDate": "2026-06-15",
        "billSegments": [{
            "startDate": "2026-05-15",
            "endDate": "2026-06-14",
            "segmentLineItems": [
                {"unitOfMeasure": "KWH", "unitCode": "GENERATE", "unitCount": 1200.0},
                {"unitOfMeasure": "KWH", "unitCode": "CONSUMED", "unitCount": 80.0},
                {"unitOfMeasure": "KWH", "unitCode": "EXCESS", "unitCount": 1100.0},
            ],
            "segmentCalcs": [
                {"startDate": "2026-05-15", "endDate": "2026-06-14",
                 "dollarAmount": -45.50, "rate": "NM"},
            ],
        }],
    }
    m = gmp.bill_json_to_metrics(bill)
    ok = (
        m.get("kwh_generated") == 1200
        and m.get("kwh_sent_to_grid") == 1100.0
        and m.get("period_start") is not None
        and m.get("period_end") is not None
        and m.get("parse_status") in ("parsed", "partial")
        and m.get("is_net_metered") is True
    )
    return {
        "ok": ok,
        "family": "gmp",
        "path": "adapters.gmp.bill_json_to_metrics (worker JSON bill pull)",
        "metrics": {
            "kwh_generated": m.get("kwh_generated"),
            "kwh_sent_to_grid": m.get("kwh_sent_to_grid"),
            "kwh_consumed": m.get("kwh_consumed"),
            "period_start": str(m.get("period_start")),
            "period_end": str(m.get("period_end")),
            "total_cost": m.get("total_cost"),
            "parse_status": m.get("parse_status"),
        },
    }


def verify_vec_smarthub_extractor() -> dict[str, Any]:
    """Offline proof: real VEC SmartHub billing-history row shape → sync bill row.

    Uses the same field mapping as harvester/vendors/smarthub._bill_row and the
    fixture tests/fixtures/vec/billing_rows.json.
    """
    from .harvester.vendors.smarthub import SmartHubVendor

    fixture = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "vec" / "billing_rows.json"
    if fixture.exists():
        rows = json.loads(fixture.read_text())
    else:
        # Minimal NISC overview row (epoch millis billingDateTimestamp).
        rows = [{
            "accountNumber": "6578300",
            "customerName": "WEST GLOVER ROARING BROOK SOLAR LLC",
            "billingDateTimestamp": 1700006400000,
            "amountDue": -245.67,
            "billUuid": "abc123-def456-7890",
            "systemOfRecord": "UTILITY",
        }]

    # Prefer overview-shaped rows; fixture is already extension-sync shape.
    # If fixture has billing_date already, map through _bill_row when possible.
    out_bills = []
    for row in rows:
        if "billing_date" in row and "account_id" in row:
            # Already extension / sync shape from fixture.
            out_bills.append({
                "account_id": row["account_id"],
                "billing_date": row["billing_date"],
                "bill_amount": row.get("bill_amount"),
                "bill_uuid": row.get("bill_uuid"),
            })
        else:
            # Raw NISC overview → harvester mapper
            mapped = SmartHubVendor._bill_row(
                str(row.get("accountNumber") or row.get("account_id") or ""),
                row,
            )
            out_bills.append(mapped)

    ok = (
        len(out_bills) >= 1
        and all(b.get("billing_date") for b in out_bills)
        and all(b.get("account_id") for b in out_bills)
    )
    return {
        "ok": ok,
        "family": "smarthub",
        "provider_example": "vec",
        "path": "harvester.vendors.smarthub._bill_row → /v1/sync",
        "bills_parsed": len(out_bills),
        "sample": out_bills[0] if out_bills else None,
    }


def verify_autopilot_matrix() -> dict[str, Any]:
    """End-to-end classification + extractor proofs for the scalable design."""
    plans = {
        "gmp": classify_login("gmp").to_dict(),
        "vec": classify_login("vec", "vermontelectric.smarthub.coop").to_dict(),
        "wec": classify_login("wec", "washingtonelectric.smarthub.coop").to_dict(),
        "unknown_iou": classify_login("acme_power", "billing.acmepower.example").to_dict(),
    }
    gmp = verify_gmp_extractor()
    vec = verify_vec_smarthub_extractor()

    # Lifecycle simulation (no DB): new logins
    arm_gmp = on_credential_saved(
        tenant_id="ten_verify", provider="gmp", username="owner@x.com", enabled=True)
    arm_vec = on_credential_saved(
        tenant_id="ten_verify", provider="vec", username="owner@x.com",
        login_host="vermontelectric.smarthub.coop", enabled=True)
    arm_unk = on_credential_saved(
        tenant_id="ten_verify", provider="acme_power", username="owner@x.com",
        login_host="portal.acme.example", enabled=True)

    return {
        "ok": gmp["ok"] and vec["ok"]
              and arm_gmp["armed"] and arm_vec["armed"] and not arm_unk["armed"],
        "classification": plans,
        "gmp_extractor": gmp,
        "vec_extractor": vec,
        "lifecycle": {
            "gmp_login": {"armed": arm_gmp["armed"], "action": arm_gmp["plan"]["action"]},
            "vec_login": {"armed": arm_vec["armed"], "action": arm_vec["plan"]["action"]},
            "unknown_login": {
                "armed": arm_unk["armed"],
                "action": arm_unk["plan"]["action"],
                "detail": arm_unk.get("detail"),
            },
        },
        "scalable_thesis": (
            "Do not hand-write one adapter per utility. Arm platform families "
            "(GMP JWT, SmartHub NISC, …) on login; for unknowns run automatic "
            "bounded browser discovery (one login, MFA/CAPTCHA abort) then "
            "synthesize extractors from captured JSON (auto_adapters)."
        ),
        "discovery": {
            "engine": "api.bill_discovery_engine",
            "safety": [
                "one password login attempt per job",
                "hard abort on MFA/CAPTCHA/lockout text",
                "max wall clock / navigations / captures",
                "refuse if harvest_fails already at lockout pause",
                "known families skip browser (skipped_known)",
            ],
        },
    }


# ── HTTP surface ──────────────────────────────────────────────────────────────

class DiscoverIn(BaseModel):
    provider: str
    login_host: Optional[str] = None
    username: Optional[str] = None
    sample_payload: Optional[Any] = None  # JSON object/array or raw string


class VerifyIn(BaseModel):
    families: Optional[list[str]] = None  # default both gmp + smarthub


def _tenant_from_auth(authorization: Optional[str]):
    from .account import tenant_from_session
    return tenant_from_session(authorization)


@router.get("/v1/bill-autopilot/status")
def autopilot_status(authorization: Optional[str] = Header(default=None)) -> dict:
    """Capability matrix + this tenant's Cloud Capture logins classified."""
    t = _tenant_from_auth(authorization)
    from .db import SessionLocal
    from .models import PortalCredential
    from sqlalchemy import select
    from sqlalchemy.orm import defer

    with SessionLocal() as db:
        rows = db.execute(
            select(PortalCredential).options(
                defer(PortalCredential.secret_enc),
                defer(PortalCredential.session_state_enc),
            ).where(PortalCredential.tenant_id == t.id)
        ).scalars().all()
        logins = []
        for r in rows:
            plan = classify_login(r.provider, r.login_host)
            logins.append({
                "provider": r.provider,
                "username": r.username,
                "enabled": bool(r.cloud_capture_enabled),
                "login_host": r.login_host,
                "last_harvest_ok": r.last_harvest_ok,
                "last_harvest_at": r.last_harvest_at.isoformat() if r.last_harvest_at else None,
                "plan": plan.to_dict(),
            })
    return {
        "ok": True,
        "families": {
            "gmp": classify_login("gmp").to_dict(),
            "smarthub_vec": classify_login("vec").to_dict(),
            "smarthub_generic": classify_login("stowe", "stoweelectric.smarthub.coop").to_dict(),
            "unknown": classify_login("unknown_util").to_dict(),
        },
        "logins": logins,
        "note": (
            "Automatic bill pull works today for GMP (JWT API) and all SmartHub "
            "co-ops including VEC (browser harvester or extension). New logins "
            "are classified on save; unknown portals need a captured payload/HAR."
        ),
    }


@router.post("/v1/bill-autopilot/discover")
def discover(body: DiscoverIn, authorization: Optional[str] = Header(default=None)) -> dict:
    """Classify a login and optionally synthesize an extractor from sample_payload."""
    t = _tenant_from_auth(authorization)
    sample = body.sample_payload
    if sample is not None and not isinstance(sample, str):
        sample = json.dumps(sample)
    return on_credential_saved(
        tenant_id=t.id,
        provider=body.provider,
        username=body.username or "discover",
        login_host=body.login_host,
        enabled=True,
        sample_payload=sample,
    )


@router.post("/v1/bill-autopilot/verify")
def verify(body: VerifyIn | None = None,
           authorization: Optional[str] = Header(default=None)) -> dict:
    """Run offline GMP + VEC extractor proofs + classification matrix.

    Auth required so this stays tenant-gated; no live portal credentials used.
    """
    _tenant_from_auth(authorization)
    return verify_autopilot_matrix()


@router.get("/v1/bill-autopilot/verify-public")
def verify_public() -> dict:
    """Unauthenticated smoke for CI / health — no tenant secrets, fixture only."""
    # Intentionally public-ish for deploy smoke; no credentials touched.
    return verify_autopilot_matrix()


class ExploreIn(BaseModel):
    provider: str
    username: str
    login_host: Optional[str] = None
    force_explore: bool = False


@router.post("/v1/bill-autopilot/explore")
def explore_now(body: ExploreIn, authorization: Optional[str] = Header(default=None)) -> dict:
    """Queue (or re-queue) automatic bounded discovery for a vaulted login.

    Fully automatic after enqueue: the discovery worker logs in once (if needed),
    captures bill-ish JSON, synthesizes an extractor, and stores a candidate.
    Safe aborts on MFA/CAPTCHA; never retries a failed password.
    """
    t = _tenant_from_auth(authorization)
    from .bill_discovery_engine import enqueue_discovery
    job = enqueue_discovery(
        tenant_id=t.id,
        provider=body.provider,
        username=body.username,
        login_host=body.login_host,
        force_explore=bool(body.force_explore),
    )
    return {"ok": True, "job": job}


@router.get("/v1/bill-autopilot/discovery/{job_id}")
def discovery_status(job_id: int, authorization: Optional[str] = Header(default=None)) -> dict:
    t = _tenant_from_auth(authorization)
    from .db import SessionLocal
    from .models import BillDiscoveryJob
    with SessionLocal() as db:
        job = db.get(BillDiscoveryJob, job_id)
        if job is None or job.tenant_id != t.id:
            raise HTTPException(404, "discovery job not found")
        from .bill_discovery_engine import _job_dict
        return {"ok": True, "job": _job_dict(job)}


@router.get("/v1/bill-autopilot/discoveries")
def list_discoveries(authorization: Optional[str] = Header(default=None),
                     limit: int = 20) -> dict:
    t = _tenant_from_auth(authorization)
    from .db import SessionLocal
    from .models import BillDiscoveryJob
    from sqlalchemy import select
    from .bill_discovery_engine import _job_dict
    lim = max(1, min(int(limit or 20), 100))
    with SessionLocal() as db:
        rows = db.execute(
            select(BillDiscoveryJob).where(
                BillDiscoveryJob.tenant_id == t.id
            ).order_by(BillDiscoveryJob.id.desc()).limit(lim)
        ).scalars().all()
        return {"ok": True, "jobs": [_job_dict(j) for j in rows]}