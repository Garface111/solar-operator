"""Remap ten_anna_800 offtaker (and host) utility accounts from all-GMP onto a
mix of every LIVE offtaker-supported provider: GMP + every live SmartHub coop.

Offtaker invoices list utilities where provider is `gmp` OR in
ALL_SMARTHUB_PROVIDERS — those are the codes this script uses. Bills, shares,
and subscription bindings stay put; only `UtilityAccount.provider` (and a
matching service_address state when known) changes. `auto_attach_gmp` on
subscriptions is set true only when the bound account is GMP.

Idempotent: re-running reassigns the same deterministic rotation.

  railway run .venv/bin/python scripts/anna800_mix_utilities.py
  # or with public DB:
  DATABASE_URL=... .venv/bin/python scripts/anna800_mix_utilities.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text

from api.db import SessionLocal, init_db
from api.models import BillingReportSubscription, UtilityAccount
from api.providers import PROVIDERS
from api.adapters.smarthub import ALL_SMARTHUB_PROVIDERS

TENANT_ID = "ten_anna_800"


def offtaker_supported_live_providers() -> list[dict]:
    """Sorted list of {code, label, state} for every live provider the offtaker
    utility-account list will actually surface."""
    out = []
    for p in PROVIDERS:
        code = p["code"]
        if p.get("scrape_status") != "live":
            continue
        if code != "gmp" and code not in ALL_SMARTHUB_PROVIDERS:
            continue
        out.append({
            "code": code,
            "label": p.get("label") or code,
            "state": (p.get("state") or "").upper() or None,
        })
    out.sort(key=lambda x: (x["state"] or "ZZ", x["code"]))
    return out


def _patch_address(addr, state: str | None):
    if not state:
        return addr
    if not isinstance(addr, dict):
        return {"state": state}
    new = dict(addr)
    new["state"] = state
    # If the free-text line ends in ", VT", retarget to the new state so the UI
    # doesn't contradict the provider (demo data only).
    line1 = new.get("line1")
    if isinstance(line1, str) and line1.rstrip().endswith(", VT"):
        new["line1"] = line1[: -len("VT")] + state
    return new


def run(*, dry_run: bool = False) -> dict:
    init_db()
    providers = offtaker_supported_live_providers()
    assert providers, "no live offtaker-supported providers loaded"
    n_p = len(providers)
    codes = [p["code"] for p in providers]
    by_code = {p["code"]: p for p in providers}

    with SessionLocal() as db:
        uas = db.execute(
            select(UtilityAccount).where(
                UtilityAccount.tenant_id == TENANT_ID,
                UtilityAccount.deleted_at.is_(None),
            ).order_by(UtilityAccount.id)
        ).scalars().all()
        if not uas:
            raise SystemExit(f"no utility accounts for {TENANT_ID}")

        # Stable assignment by sorted id so re-runs are identical.
        before = Counter((ua.provider or "").lower() for ua in uas)
        changed = 0
        for i, ua in enumerate(uas):
            p = providers[i % n_p]
            new_code = p["code"]
            if (ua.provider or "").lower() == new_code and (
                not isinstance(ua.service_address, dict)
                or (ua.service_address or {}).get("state") == p["state"]
            ):
                continue
            ua.provider = new_code
            ua.service_address = _patch_address(ua.service_address, p["state"])
            changed += 1

        # auto_attach_gmp follows the bound offtaker account's provider.
        subs = db.execute(
            select(BillingReportSubscription).where(
                BillingReportSubscription.tenant_id == TENANT_ID,
                BillingReportSubscription.deleted_at.is_(None),
            )
        ).scalars().all()
        ua_by_id = {ua.id: ua for ua in uas}
        sub_flips = 0
        for s in subs:
            ua = ua_by_id.get(s.utility_account_id) if s.utility_account_id else None
            want = bool(ua and (ua.provider or "").lower() == "gmp")
            if bool(getattr(s, "auto_attach_gmp", False)) != want:
                s.auto_attach_gmp = want
                sub_flips += 1

        after_preview = Counter()
        for i, ua in enumerate(uas):
            after_preview[providers[i % n_p]["code"]] += 1

        if dry_run:
            db.rollback()
        else:
            db.commit()

        return {
            "tenant_id": TENANT_ID,
            "dry_run": dry_run,
            "provider_catalog_size": n_p,
            "utility_accounts": len(uas),
            "accounts_updated": changed,
            "subs_auto_attach_flipped": sub_flips,
            "before_top": before.most_common(8),
            "after_distinct_providers": len(after_preview),
            "after_per_provider_minmax": (
                min(after_preview.values()), max(after_preview.values())
            ),
            "sample_after": after_preview.most_common(12),
            "ne_codes_included": sorted(
                c for c in codes
                if by_code[c]["state"] in ("VT", "NH", "ME", "MA", "CT", "NY", "RI")
            ),
        }


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    stats = run(dry_run=dry)
    for k, v in stats.items():
        print(f"{k}: {v}")
