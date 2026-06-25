"""Automatic warranty claims — the server-authoritative paperwork engine behind
Array Operator's Claims tab.

The promise made literal: "not a dashboard — an agent that watches your panels
and brings the verdict AND the paperwork." This module is the paperwork arm.
It watches each owner's fleet (via inverter_fleet.build_fleet_tree) and the
MOMENT an inverter goes DEAD or throws a hardware FAULT — the two warrantable
failures — it opens a WarrantyClaim: drafts the manufacturer email, snapshots
the peer-measured evidence (so weather can't be blamed), and runs it through a
lifecycle the owner controls.

SEND POLICY — full control from the owner's end (tenant.claim_send_mode, with a
per-claim override on WarrantyClaim.send_mode):
    manual → agent drafts, owner approves each send (default — nothing leaves on
             its own)
    auto   → agent files the instant a failure is confirmed
    delay  → agent queues and files after `claim_grace_hours` unless the owner
             cancels (the safety net with leverage)

SENDING — the genuinely outward-facing, irreversible step, so it is deliberately
conservative by default:
    • By default a "send" emails the FULLY-DRAFTED claim to the OWNER
      (tenant.contact_email) as a forward-ready packet, with the manufacturer
      address in the body. The owner forwards it — we never cold-email a guessed
      manufacturer support address on their behalf.
    • Set env WARRANTY_SEND_DIRECT=1 (or pass direct=True per call) to send
      straight to the manufacturer with Reply-To set to the owner.
Either way Reply-To is the owner, so replies reach them, not us.

Endpoints (mounted under /v1/array-owners/claims) are at the bottom; the engine
functions above are pure enough to unit-test by injecting a fake fleet tree.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from . import notify
from .db import SessionLocal
from .inverters import peer_analysis
from .models import Tenant, WarrantyClaim, now
from .rates import REC_PRICE_USD_PER_MWH, get_energy_rate

log = logging.getLogger(__name__)
router = APIRouter()

# Annualisation window — matches peer_analysis.WINDOW_DAYS so the lost-value math
# lines up with the evidence the cohort analysis produced.
WINDOW_DAYS = peer_analysis.WINDOW_DAYS

# The two warrantable hardware failures. Underperformance / comms gaps are
# DIAGNOSES (handled in the Arrays triage queue), never auto-claims.
WARRANTABLE = ("dead", "fault")

ACTIVE_STAGES = ("ready", "queued", "sent")
VALID_MODES = ("manual", "auto", "delay")

# Best-guess manufacturer support addresses. These are EDITABLE per-claim by the
# owner (draft.to) before anything is filed — we never assume they're right.
_VENDOR_SUPPORT = {
    "solaredge": "support@solaredge.com",
    "enphase": "support@enphase.com",
    "fronius": "pv-support-usa@fronius.com",
    "sma": "service@sma-america.com",
    "locus": "support@locusenergy.com",
}


def _send_direct_default() -> bool:
    return os.getenv("WARRANTY_SEND_DIRECT", "").strip().lower() in ("1", "true", "yes")


# ── value model ───────────────────────────────────────────────────────────────

def _val(kwh: float, rate: float) -> float:
    """Dollar value of kWh = energy offset + REC value. Mirrors array_owners
    ._value_model for the per-window (estimated, un-floored) case."""
    return kwh * rate + (kwh / 1000.0) * REC_PRICE_USD_PER_MWH


def _lost_kwh(inv: dict, fleet_window_kwh: float, total_nameplate: float) -> float:
    """How much this inverter SHOULD have made minus what it did — its fair share
    of the cohort's window harvest, by nameplate. For dead/fault that's almost the
    whole fair share."""
    if total_nameplate <= 0:
        return 0.0
    fair = (inv.get("nameplate_kw") or 0.0) / total_nameplate * fleet_window_kwh
    return max(0.0, fair - (inv.get("window_kwh") or 0.0))


def build_evidence(inv: dict, column: dict, rate: float) -> dict:
    """Immutable, peer-measured evidence snapshot captured at detection time.
    camelCase keys so the Array Operator front end can consume it verbatim."""
    invs = column.get("inverters", [])
    total_np = sum((i.get("nameplate_kw") or 0.0) for i in invs) or 1.0
    fleet_win = sum((i.get("window_kwh") or 0.0) for i in invs)
    lk = _lost_kwh(inv, fleet_win, total_np)
    lost_mo = _val(lk, rate) / WINDOW_DAYS * 30
    lost_yr = _val(lk, rate) / WINDOW_DAYS * 365
    stale = inv.get("stale_hours")
    days_down = round(stale / 24) if stale is not None else None
    return {
        "peerIndex": round(inv["peer_index"], 2) if inv.get("peer_index") is not None else None,
        "lostKwh": round(lk),
        "lostMo": round(lost_mo, 2),
        "lostYr": round(lost_yr, 2),
        "daysDown": days_down,
        "peers": max(0, len(invs) - 1),
        "windowDays": WINDOW_DAYS,
    }


# ── draft ─────────────────────────────────────────────────────────────────────

def _vendor_support_email(vendor: str | None) -> str:
    v = (vendor or "").strip().lower()
    if v in _VENDOR_SUPPORT:
        return _VENDOR_SUPPORT[v]
    slug = "".join(ch for ch in v if ch.isalnum()) or "installer"
    return f"support@{slug}.com"


def build_draft(claim: WarrantyClaim, tenant: Tenant) -> dict:
    """Compose the manufacturer email {to, subject, body} from the claim's
    snapshot. Peer-measured evidence is baked in so the manufacturer can't
    hand-wave it as a cloudy spell."""
    is_fault = claim.fail_type == "fault"
    vendor_title = (claim.vendor or "manufacturer").replace("_", " ").title()
    to = _vendor_support_email(claim.vendor)
    model = claim.model or "inverter"
    subject = (
        f"{'Service request' if is_fault else 'Warranty claim'} — "
        f"{model} ({claim.site_name} · {claim.inv_name})"
    )
    e = claim.evidence or {}
    pi = e.get("peerIndex")
    peers = e.get("peers", 0)
    days_down = e.get("daysDown")
    down_line = (
        f"It has produced no usable output for approximately {days_down} day(s)."
        if days_down is not None else "It has stopped producing."
    )
    rate_per_kwh = _val(1.0, get_energy_rate(None))
    owner = tenant.operator_name or tenant.company_name or tenant.name or "[Your name]"
    pi_txt = f"{pi:.2f}" if pi is not None else "—"
    body = (
        "To whom it may concern,\n\n"
        f"I am {'requesting service for' if is_fault else 'filing a warranty claim on'} "
        f'the following inverter on my solar array "{claim.site_name}":\n\n'
        f"  • Inverter:        {claim.inv_name}\n"
        f"  • Serial:          {claim.serial or '—'}\n"
        f"  • Model:           {model}"
        f"{f' ({claim.nameplate_kw} kW nameplate)' if claim.nameplate_kw else ''}\n"
        f"  • Manufacturer:    {vendor_title}\n"
        f"  • Reported status: {'HARDWARE FAULT' if is_fault else 'DEAD / not reporting'}\n\n"
        "Issue:\n"
        f"{down_line}\n\n"
        f"Evidence (independently measured against {peers} peer inverter"
        f"{'' if peers == 1 else 's'} on the same array, under identical weather):\n"
        f"  • Peer index:            {pi_txt} (1.00 = fair share; this unit is far below par)\n"
        f"  • Estimated lost output: {e.get('lostKwh', 0):,} kWh over the last {WINDOW_DAYS} days\n"
        f"  • Estimated lost value:  ${e.get('lostMo', 0):,.0f} so far this month "
        f"(at ${rate_per_kwh:.2f}/kWh offset incl. RECs)\n\n"
        "The neighboring inverters produced normally over the same period, which "
        "rules out weather or shading as the cause. Please advise on next steps for "
        "repair or replacement under warranty.\n\n"
        "Thank you,\n"
        f"{owner}\n"
        f"{tenant.company_name or ''}".rstrip() + "\n"
    )
    return {"to": to, "subject": subject, "body": body}


# ── policy ────────────────────────────────────────────────────────────────────

def effective_mode(tenant: Tenant, claim: WarrantyClaim) -> str:
    """The send rule actually governing this claim: its per-claim override, else
    the tenant default."""
    if claim.send_mode in VALID_MODES:
        return claim.send_mode
    mode = tenant.claim_send_mode
    return mode if mode in VALID_MODES else "manual"


def _apply_policy(db, tenant: Tenant, claim: WarrantyClaim) -> None:
    """Place a freshly-opened (or re-activated) claim per its effective policy.
    Does NOT commit — the caller batches commits."""
    mode = effective_mode(tenant, claim)
    if mode == "auto":
        file_claim(db, tenant, claim, via="auto", commit=False)
    elif mode == "delay":
        claim.stage = "queued"
        claim.send_at = now() + timedelta(hours=max(1, int(tenant.claim_grace_hours or 24)))
    else:
        claim.stage = "ready"
        claim.send_at = None


# ── reconcile (the watcher) ───────────────────────────────────────────────────

def reconcile(db, tenant: Tenant, tree: Optional[dict] = None) -> dict:
    """Bring the claim ledger in line with the live fleet.

      • dead/fault inverter with no active claim → OPEN one (draft + policy).
      • active claim whose inverter recovered/vanished → CLOSE it
        (resolved if we'd already filed; otherwise cleared — caught a blip).

    Returns a small {opened, closed} tally. Pass `tree` to avoid a live fetch
    (the GET endpoint passes the tree it already built; tests inject a fake one).
    """
    from . import inverter_fleet

    if tree is None:
        # stable_verdicts: a warranty claim emails a vendor, so it must never fire
        # on a dawn / capture-gap false "dead". Use the same complete-day verdicts
        # the dashboard + alerts use. (dead/fault on settled data still opens one.)
        tree = inverter_fleet.build_fleet_tree(db, tenant, stable_verdicts=True)

    rate = get_energy_rate(None)

    # live failures, keyed by persisted inverter_id
    live: dict[int, tuple[dict, dict]] = {}
    for col in tree.get("columns", []):
        for inv in col.get("inverters", []):
            if inv.get("status") in WARRANTABLE and inv.get("inverter_id") is not None:
                live[int(inv["inverter_id"])] = (inv, col)

    active = db.execute(
        select(WarrantyClaim).where(
            WarrantyClaim.tenant_id == tenant.id,
            WarrantyClaim.stage.in_(ACTIVE_STAGES),
        )
    ).scalars().all()
    active_by_inv = {c.inverter_id: c for c in active if c.inverter_id is not None}

    opened = closed = 0

    # OPEN — new failures
    for inv_id, (inv, col) in live.items():
        if inv_id in active_by_inv:
            continue
        claim = WarrantyClaim(
            tenant_id=tenant.id,
            array_id=col.get("array_id"),
            inverter_id=inv_id,
            serial=inv.get("sn"),
            inv_name=inv.get("name") or inv.get("sn"),
            model=inv.get("model"),
            vendor=inv.get("vendor"),
            nameplate_kw=inv.get("nameplate_kw"),
            site_name=col.get("array_name"),
            fail_type=inv.get("status"),
            evidence=build_evidence(inv, col, rate),
            stage="ready",
        )
        claim.draft = build_draft(claim, tenant)
        _apply_policy(db, tenant, claim)
        db.add(claim)
        opened += 1

    # CLOSE — recovered / vanished
    for inv_id, claim in active_by_inv.items():
        if inv_id in live:
            continue
        if claim.stage == "sent":
            claim.stage = "resolved"
            claim.resolved_at = now()
            claim.recovered_usd = round((claim.evidence or {}).get("lostYr", 0) or 0)
            claim.auto_resolved = True
        else:
            claim.stage = "cleared"
            claim.cleared_at = now()
        closed += 1

    db.commit()
    return {"opened": opened, "closed": closed}


def process_due(db, tenant: Tenant) -> int:
    """Fire any queued claims whose grace timer has elapsed. Returns count sent."""
    due = db.execute(
        select(WarrantyClaim).where(
            WarrantyClaim.tenant_id == tenant.id,
            WarrantyClaim.stage == "queued",
            WarrantyClaim.send_at.isnot(None),
            WarrantyClaim.send_at <= now(),
        )
    ).scalars().all()
    n = 0
    for claim in due:
        if file_claim(db, tenant, claim, via="auto", commit=False):
            n += 1
    if due:
        db.commit()
    return n


# ── lifecycle transitions ─────────────────────────────────────────────────────

def file_claim(db, tenant: Tenant, claim: WarrantyClaim, *, via: str,
               direct: Optional[bool] = None, commit: bool = True) -> bool:
    """File the claim — the real outward-facing send. Safe by default: emails the
    owner a forward-ready packet unless `direct`/WARRANTY_SEND_DIRECT routes it
    straight to the manufacturer. Advances to `sent` only if the email went out.
    """
    draft = claim.draft or build_draft(claim, tenant)
    go_direct = _send_direct_default() if direct is None else bool(direct)
    owner_email = tenant.contact_email

    if go_direct and draft.get("to"):
        recipient = draft["to"]
    elif owner_email:
        recipient = owner_email
    else:
        # No owner email and not going direct — nowhere safe to send. Flag it.
        notify.send_internal_alert(
            "Warranty claim has no recipient",
            f"Tenant {tenant.id} claim {claim.id} ({claim.site_name}/{claim.inv_name}) "
            f"could not be filed: no contact_email and direct-send off.",
        )
        return False

    subject = draft.get("subject") or "Warranty claim"
    if not go_direct:
        subject = f"[Ready to forward] {subject}"
    body = draft.get("body") or ""
    if not go_direct:
        body = (
            "Your Array Operator agent drafted the warranty claim below. Review it, "
            f"then forward it to the manufacturer at: {draft.get('to', '(see above)')}\n"
            "(Reply-To is set to you, so the manufacturer's reply reaches you directly.)\n\n"
            "— — — — — — — — — — — — — — — — — — — — — — — — — — — — — — —\n\n"
        ) + body

    ok = notify.send_warranty_claim_email(
        to=recipient, subject=subject, body_text=body,
        reply_to=owner_email,
        from_name=tenant.company_name or "Array Operator",
    )
    if not ok:
        log.warning("warranty claim %s send failed (tenant %s)", claim.id, tenant.id)
        return False

    claim.stage = "sent"
    claim.sent_at = now()
    claim.sent_via = via
    claim.sent_to = recipient
    claim.sent_direct = go_direct
    claim.send_at = None
    if commit:
        db.commit()
    return True


def resolve_claim(db, claim: WarrantyClaim, recovered_usd: Optional[float] = None) -> None:
    claim.stage = "resolved"
    claim.resolved_at = now()
    claim.auto_resolved = False
    if recovered_usd is not None:
        claim.recovered_usd = round(max(0.0, float(recovered_usd)))
    else:
        claim.recovered_usd = round((claim.evidence or {}).get("lostYr", 0) or 0)
    db.commit()


def dismiss_claim(db, claim: WarrantyClaim) -> None:
    claim.stage = "dismissed"
    claim.dismissed_at = now()
    db.commit()


def reopen_claim(db, tenant: Tenant, claim: WarrantyClaim) -> None:
    claim.recovered_usd = 0.0
    claim.resolved_at = claim.sent_at = claim.dismissed_at = claim.cleared_at = None
    claim.sent_via = claim.sent_to = None
    claim.sent_direct = False
    claim.auto_resolved = False
    _apply_policy(db, tenant, claim)
    db.commit()


def set_claim_mode(db, tenant: Tenant, claim: WarrantyClaim, mode: str) -> None:
    if mode not in VALID_MODES:
        raise ValueError(f"bad mode: {mode}")
    claim.send_mode = mode
    if claim.stage in ("ready", "queued"):
        _apply_policy(db, tenant, claim)
    db.commit()


def cancel_auto_send(db, claim: WarrantyClaim) -> None:
    """Hold a queued claim back for manual approval."""
    claim.send_mode = "manual"
    claim.stage = "ready"
    claim.send_at = None
    db.commit()


def update_draft(db, claim: WarrantyClaim, to=None, subject=None, body=None) -> None:
    d = dict(claim.draft or {})
    if to is not None:
        d["to"] = to
    if subject is not None:
        d["subject"] = subject
    if body is not None:
        d["body"] = body
    claim.draft = d
    db.commit()


# ── serialization ─────────────────────────────────────────────────────────────

def serialize(claim: WarrantyClaim) -> dict:
    def iso(dt):
        return dt.replace(microsecond=0).isoformat() + "Z" if dt else None
    return {
        "id": claim.id,
        "inverter_id": claim.inverter_id,
        "array_id": claim.array_id,
        "site": claim.site_name,
        "inv": claim.inv_name,
        "serial": claim.serial,
        "model": claim.model,
        "vendor": claim.vendor,
        "nameplate": claim.nameplate_kw,
        "failType": claim.fail_type,
        "stage": claim.stage,
        "mode": claim.send_mode or "",
        "evidence": claim.evidence or {},
        "draft": claim.draft or {},
        "recoveredUsd": round(claim.recovered_usd or 0),
        "autoResolved": bool(claim.auto_resolved),
        "sentVia": claim.sent_via,
        "sentTo": claim.sent_to,
        "sentDirect": bool(claim.sent_direct),
        "sendAt": iso(claim.send_at),
        "sentAt": iso(claim.sent_at),
        "resolvedAt": iso(claim.resolved_at),
        "createdAt": iso(claim.created_at),
    }


def summarize(claims: list[WarrantyClaim]) -> dict:
    active = [c for c in claims if c.stage in ACTIVE_STAGES]
    resolved = [c for c in claims if c.stage == "resolved"]
    return {
        "open": len(active),
        "awaiting": sum(1 for c in claims if c.stage == "ready"),
        "queued": sum(1 for c in claims if c.stage == "queued"),
        "sent": sum(1 for c in claims if c.stage == "sent"),
        "atStakeYr": round(sum((c.evidence or {}).get("lostYr", 0) or 0 for c in active)),
        "recovered": round(sum(c.recovered_usd or 0 for c in resolved)),
        "resolved": len(resolved),
    }


# ── endpoints ─────────────────────────────────────────────────────────────────

def _tenant(authorization: str | None) -> Tenant:
    from .array_owners import _tenant_from_bearer
    return _tenant_from_bearer(authorization)


def _get_claim(db, tenant: Tenant, claim_id: int) -> WarrantyClaim:
    claim = db.get(WarrantyClaim, claim_id)
    if claim is None or claim.tenant_id != tenant.id:
        raise HTTPException(404, "Claim not found")
    return claim


def _payload(db, tenant: Tenant) -> dict:
    claims = db.execute(
        select(WarrantyClaim)
        .where(WarrantyClaim.tenant_id == tenant.id)
        .order_by(WarrantyClaim.created_at.desc())
    ).scalars().all()
    visible = [c for c in claims if c.stage != "cleared"]
    return {
        "claims": [serialize(c) for c in visible],
        "settings": {
            "sendMode": tenant.claim_send_mode if tenant.claim_send_mode in VALID_MODES else "manual",
            "graceHours": int(tenant.claim_grace_hours or 24),
            "sendDirect": _send_direct_default(),
        },
        "summary": summarize(visible),
    }


@router.get("/v1/array-owners/claims")
def list_claims(reconcile_first: int = 1,
                authorization: str | None = Header(default=None)) -> dict:
    """The Claims tab's data source. Reconciles against the live fleet (open new
    failures, close recovered ones), fires any due grace-timer sends, then returns
    the full ledger + the owner's send policy + a summary roll-up."""
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        if reconcile_first:
            try:
                reconcile(db, tenant)
                process_due(db, tenant)
            except Exception as exc:  # a live-fetch hiccup shouldn't blank the ledger
                log.warning("claims reconcile failed for %s: %s", tenant.id, exc)
        return _payload(db, tenant)


class SettingsBody(BaseModel):
    sendMode: Optional[str] = None
    graceHours: Optional[int] = None


@router.post("/v1/array-owners/claims/settings")
def update_settings(body: SettingsBody,
                    authorization: str | None = Header(default=None)) -> dict:
    """Set the owner's global send policy. Re-places every still-pending claim
    (those without a per-claim override) under the new rule."""
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        if body.sendMode is not None:
            if body.sendMode not in VALID_MODES:
                raise HTTPException(400, "sendMode must be manual|auto|delay")
            t.claim_send_mode = body.sendMode
        if body.graceHours is not None:
            t.claim_grace_hours = max(1, min(168, int(body.graceHours)))
        db.flush()
        pending = db.execute(
            select(WarrantyClaim).where(
                WarrantyClaim.tenant_id == t.id,
                WarrantyClaim.stage.in_(("ready", "queued")),
                WarrantyClaim.send_mode.is_(None),
            )
        ).scalars().all()
        for c in pending:
            _apply_policy(db, t, c)
        db.commit()
        return _payload(db, t)


class SendBody(BaseModel):
    direct: Optional[bool] = None


@router.post("/v1/array-owners/claims/{claim_id}/send")
def send_claim_ep(claim_id: int, body: SendBody = SendBody(),
                  authorization: str | None = Header(default=None)) -> dict:
    """Approve & file a claim now (owner-initiated)."""
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        claim = _get_claim(db, t, claim_id)
        ok = file_claim(db, t, claim, via="owner", direct=body.direct)
        if not ok:
            raise HTTPException(502, "Could not send the claim email — check the recipient and try again.")
        return {"ok": True, "claim": serialize(claim)}


class ResolveBody(BaseModel):
    recoveredUsd: Optional[float] = None


@router.post("/v1/array-owners/claims/{claim_id}/resolve")
def resolve_ep(claim_id: int, body: ResolveBody = ResolveBody(),
               authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        claim = _get_claim(db, tenant, claim_id)
        resolve_claim(db, claim, body.recoveredUsd)
        return {"ok": True, "claim": serialize(claim)}


@router.post("/v1/array-owners/claims/{claim_id}/dismiss")
def dismiss_ep(claim_id: int, authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        claim = _get_claim(db, tenant, claim_id)
        dismiss_claim(db, claim)
        return {"ok": True, "claim": serialize(claim)}


@router.post("/v1/array-owners/claims/{claim_id}/reopen")
def reopen_ep(claim_id: int, authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        claim = _get_claim(db, t, claim_id)
        reopen_claim(db, t, claim)
        return {"ok": True, "claim": serialize(claim)}


@router.post("/v1/array-owners/claims/{claim_id}/cancel-auto")
def cancel_auto_ep(claim_id: int, authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        claim = _get_claim(db, tenant, claim_id)
        cancel_auto_send(db, claim)
        return {"ok": True, "claim": serialize(claim)}


class ModeBody(BaseModel):
    mode: str


@router.post("/v1/array-owners/claims/{claim_id}/mode")
def mode_ep(claim_id: int, body: ModeBody,
            authorization: str | None = Header(default=None)) -> dict:
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        t = db.get(Tenant, tenant.id)
        claim = _get_claim(db, t, claim_id)
        try:
            set_claim_mode(db, t, claim, body.mode)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "claim": serialize(claim)}


class DraftBody(BaseModel):
    to: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None


@router.patch("/v1/array-owners/claims/{claim_id}/draft")
def draft_ep(claim_id: int, body: DraftBody,
             authorization: str | None = Header(default=None)) -> dict:
    """Persist owner edits to a claim's drafted email."""
    tenant = _tenant(authorization)
    with SessionLocal() as db:
        claim = _get_claim(db, tenant, claim_id)
        update_draft(db, claim, to=body.to, subject=body.subject, body=body.body)
        return {"ok": True, "claim": serialize(claim)}
