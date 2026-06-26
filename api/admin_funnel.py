"""Read-only operator funnel dashboard. Admin-key gated. No DB writes, no migration.

GET /admin/funnel-stats  -> JSON metrics
GET /admin/funnel        -> server-rendered HTML dashboard (auto-refresh)

Added by CC 2026-06-21 to watch signups -> card -> trial -> paid as outreach ramps.
"""
import hmac
import os
from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .db import SessionLocal
from .models import Tenant

router = APIRouter()
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")


def _check(key_header: str | None, key_query: str | None) -> None:
    key = key_header or key_query
    if not ADMIN_API_KEY:
        raise HTTPException(503, "Admin API not configured (set ADMIN_API_KEY)")
    if not hmac.compare_digest(key or "", ADMIN_API_KEY):
        raise HTTPException(403, "Invalid or missing admin key")


def _compute() -> dict:
    now = datetime.utcnow()
    with SessionLocal() as db:
        ts = db.query(Tenant).all()

    def dsince(d):
        return (now - d).days if d else None

    def split(rows):
        return dict(Counter((t.product or "unknown") for t in rows))

    card = [t for t in ts if t.stripe_payment_method_id]
    subbed = [t for t in ts if t.stripe_subscription_id]
    comped = [t for t in ts if (t.subscription_status or "") == "comped"]
    trial_set = [t for t in ts if t.trial_ends_at and not t.stripe_subscription_id
                 and (t.subscription_status or "") != "comped"]
    trial_active = [t for t in trial_set if t.trial_ends_at and t.trial_ends_at > now]
    trial_soon = [t for t in trial_active if (t.trial_ends_at - now).days <= 7]

    return {
        "as_of": now.isoformat() + "Z",
        "total_tenants": len(ts),
        "by_product": dict(Counter((t.product or "unknown") for t in ts)),
        "subscription_status": dict(Counter((t.subscription_status or "none") for t in ts)),
        "card_on_file": {"total": len(card), "by_product": split(card)},
        "with_live_subscription": {"total": len(subbed), "by_product": split(subbed)},
        "comped": len(comped),
        "trial_set_no_sub": {"total": len(trial_set), "by_product": split(trial_set)},
        "trial_active_now": len(trial_active),
        "trial_ending_7d": len(trial_soon),
        "new_signups_7d": sum(1 for t in ts if (dsince(t.created_at) or 999) <= 7),
        "new_signups_30d": sum(1 for t in ts if (dsince(t.created_at) or 999) <= 30),
    }


@router.get("/admin/funnel-stats")
def funnel_stats(x_admin_key: str | None = Header(default=None),
                 key: str | None = Query(default=None)):
    _check(x_admin_key, key)
    return JSONResponse(_compute())


def _pct(num: int, den: int) -> str:
    return f"{(100 * num / den):.0f}%" if den else "—"


def _card(label: str, value, sub: str = "") -> str:
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    return (f'<div class="card"><div class="lbl">{label}</div>'
            f'<div class="val">{value}</div>{sub_html}</div>')


@router.get("/admin/funnel", response_class=HTMLResponse)
def funnel_page(key: str | None = Query(default=None),
                x_admin_key: str | None = Header(default=None)):
    _check(x_admin_key, key)
    m = _compute()
    signups = m["total_tenants"]
    card = m["card_on_file"]["total"]
    trials = m["trial_active_now"]
    paid = m["with_live_subscription"]["total"]
    bp = m["by_product"]
    prod_rows = "".join(
        f"<tr><td>{p}</td><td>{c}</td></tr>" for p, c in sorted(bp.items())
    )
    status_rows = "".join(
        f"<tr><td>{s}</td><td>{c}</td></tr>" for s, c in sorted(m["subscription_status"].items())
    )
    cards = "".join([
        _card("Signups (total)", signups, f"+{m['new_signups_7d']} last 7d · +{m['new_signups_30d']} last 30d"),
        _card("Card on file", card, _pct(card, signups) + " of signups"),
        _card("Active trials", trials, f"{m['trial_ending_7d']} ending ≤7d"),
        _card("Paying subscriptions", paid, _pct(paid, signups) + " of signups"),
        _card("Comped", m["comped"], "non-billed (e.g. pilot)"),
    ])
    funnel = "".join([
        _card("1 · Signed up", signups),
        _card("2 · Added card", card, _pct(card, signups)),
        _card("3 · In trial", trials, _pct(trials, signups)),
        _card("4 · Paying", paid, _pct(paid, signups)),
    ])
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Solar Operator — Funnel</title>
<style>
 body{{margin:0;background:#0c1311;color:#e8f1ec;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
 .wrap{{max-width:920px;margin:0 auto;padding:28px 20px}}
 h1{{font-size:20px;font-weight:600;margin:0 0 2px}}
 .meta{{color:#8aa89b;font-size:13px;margin-bottom:22px}}
 h2{{font-size:13px;font-weight:600;color:#8aa89b;text-transform:uppercase;letter-spacing:.04em;margin:26px 0 10px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
 .card{{background:#15201c;border:1px solid #243530;border-radius:12px;padding:14px 16px}}
 .lbl{{color:#8aa89b;font-size:12px;margin-bottom:6px}}
 .val{{font-size:28px;font-weight:600;color:#34d896}}
 .sub{{color:#8aa89b;font-size:12px;margin-top:4px}}
 table{{width:100%;border-collapse:collapse;font-size:14px}}
 td{{padding:7px 10px;border-bottom:1px solid #1c2925}}
 td:last-child{{text-align:right;color:#8aa89b}}
 .note{{color:#5f7468;font-size:12px;margin-top:24px}}
</style></head><body><div class="wrap">
<h1>Solar Operator — Funnel</h1>
<div class="meta">Live · as of {m['as_of']} · auto-refreshes every 60s</div>
<h2>Conversion funnel</h2><div class="grid">{funnel}</div>
<h2>Key metrics</h2><div class="grid">{cards}</div>
<h2>Signups by product</h2><table>{prod_rows}</table>
<h2>Subscription status</h2><table>{status_rows}</table>
<div class="note">Read-only. Paying = tenants with a live Stripe subscription. Billing is deferred:
a card is stored at signup and the metered subscription mints at trial end, so "Active trials" with a
card are the near-term revenue pipeline. MRR shows once subscriptions exist.</div>
</div></body></html>"""
    return HTMLResponse(content=html)
