"""Admin-only vault health — replacement for ad-hoc public-Postgres probes.

GET /admin/vault-stats   — fleet counts (no secrets, no usernames)
GET /admin/vault-health  — encryption + split-key posture + residual churn check
GET /admin/harvest-runs  — one portal login's stored copies, lockout roster and
                           newest harvest runs (no secrets; ops debugging)

Auth: X-Admin-Key header ONLY (query ?key= rejected — lands in access logs).
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .db import SessionLocal

router = APIRouter()
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")


def _check(key_header: str | None, key_query: str | None = None) -> None:
    if key_query:
        raise HTTPException(
            400,
            "Pass the admin key via X-Admin-Key header only (query ?key= is disabled)",
        )
    if not ADMIN_API_KEY:
        raise HTTPException(503, "Admin API not configured (set ADMIN_API_KEY)")
    if not hmac.compare_digest(key_header or "", ADMIN_API_KEY):
        raise HTTPException(403, "Invalid or missing admin key")


def _compute_stats() -> dict:
    """Read-only vault posture from SQL — never selects ciphertext into Python."""
    with SessionLocal() as db:
        total = db.execute(text("SELECT count(*) FROM portal_credential")).scalar() or 0
        enabled = db.execute(text(
            "SELECT count(*) FROM portal_credential WHERE cloud_capture_enabled"
        )).scalar() or 0
        with_secret = db.execute(text(
            "SELECT count(*) FROM portal_credential WHERE secret_enc IS NOT NULL"
        )).scalar() or 0
        with_session = db.execute(text(
            "SELECT count(*) FROM portal_credential WHERE session_state_enc IS NOT NULL"
        )).scalar() or 0
        inactive = db.execute(text("""
            SELECT count(*) FROM portal_credential pc
            JOIN tenants t ON t.id = pc.tenant_id
            WHERE t.active = false
        """)).scalar() or 0
        inactive_enabled = db.execute(text("""
            SELECT count(*) FROM portal_credential pc
            JOIN tenants t ON t.id = pc.tenant_id
            WHERE t.active = false AND pc.cloud_capture_enabled
        """)).scalar() or 0
        # Envelope presence without decrypting (SOENC1: prefix).
        encrypted_secret = db.execute(text(
            "SELECT count(*) FROM portal_credential "
            "WHERE secret_enc IS NOT NULL AND secret_enc LIKE 'SOENC1:%'"
        )).scalar() or 0
        plain_secret = db.execute(text(
            "SELECT count(*) FROM portal_credential "
            "WHERE secret_enc IS NOT NULL AND secret_enc NOT LIKE 'SOENC1:%'"
        )).scalar() or 0
        harvest_runs_7d = db.execute(text(
            "SELECT count(*) FROM harvest_run "
            "WHERE started_at > NOW() - INTERVAL '7 days'"
        )).scalar() or 0
        harvest_ok_7d = db.execute(text(
            "SELECT count(*) FROM harvest_run "
            "WHERE started_at > NOW() - INTERVAL '7 days' AND status = 'ok'"
        )).scalar() or 0
        by_provider = db.execute(text("""
            SELECT provider, count(*) AS n
            FROM portal_credential
            GROUP BY provider
            ORDER BY n DESC
        """)).all()

    from . import crypto
    return {
        "as_of": datetime.utcnow().isoformat() + "Z",
        "encryption_at_rest": bool(crypto.encryption_enabled()),
        "vault_decrypt_enabled": bool(crypto.vault_decrypt_enabled()),
        "process_role": (os.environ.get("PROCESS_ROLE") or "unknown")[:40],
        "portal_credential_total": int(total),
        "cloud_capture_enabled": int(enabled),
        "with_secret_enc": int(with_secret),
        "with_session_state": int(with_session),
        "on_inactive_tenant": int(inactive),
        "enabled_on_inactive_tenant": int(inactive_enabled),
        "secret_envelope_soenc1": int(encrypted_secret),
        "secret_plaintext_residual": int(plain_secret),
        "harvest_runs_7d": int(harvest_runs_7d),
        "harvest_ok_7d": int(harvest_ok_7d),
        "by_provider": {str(p): int(n) for p, n in by_provider},
        # Green when no inactive vault rows and no plaintext secrets.
        "churn_clean": int(inactive) == 0,
        "plaintext_clean": int(plain_secret) == 0,
    }


@router.get("/admin/vault-stats")
def vault_stats(
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    """Fleet vault counts for ops — no secrets, no usernames."""
    _check(x_admin_key, key)
    return JSONResponse(_compute_stats())


@router.get("/admin/vault-health")
def vault_health(
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    """Pass/fail posture for monitoring / agents after deploys."""
    _check(x_admin_key, key)
    s = _compute_stats()
    issues: list[str] = []
    if not s["encryption_at_rest"]:
        issues.append("encryption_at_rest_false")
    if s["vault_decrypt_enabled"] and (s["process_role"] or "") == "web":
        issues.append("web_can_decrypt_vault")
    if not s["churn_clean"]:
        issues.append(f"inactive_tenant_vault_rows={s['on_inactive_tenant']}")
    if not s["plaintext_clean"]:
        issues.append(f"plaintext_secrets={s['secret_plaintext_residual']}")
    return JSONResponse({
        "ok": len(issues) == 0,
        "issues": issues,
        "stats": s,
    })


@router.get("/admin/harvest-runs")
def harvest_runs(
    provider: str = Query(..., min_length=2, max_length=40),
    username: str | None = Query(default=None, max_length=200),
    tenant_id: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=40, ge=1, le=200),
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    """One portal login's Cloud Capture story, for ops debugging without a
    database shell: every stored copy of the login (one per tenant), its lockout
    roster row, and the newest harvest runs with their sanitized detail.

    Read-only. Never selects ciphertext (secret_enc / session_state_enc are only
    tested for NULL in SQL), so the web process never decrypts the vault.
    Narrow by `username` (the portal login, case-insensitive) and/or `tenant_id`.
    """
    _check(x_admin_key, key)
    prov = provider.strip().lower()
    ulc = (username or "").strip().lower() or None
    where = ["pc.provider = :prov"]
    params: dict = {"prov": prov, "lim": int(limit)}
    if ulc:
        where.append("pc.username_lc = :ulc")
        params["ulc"] = ulc
    if tenant_id:
        where.append("pc.tenant_id = :tid")
        params["tid"] = tenant_id.strip()
    with SessionLocal() as db:
        creds = db.execute(text(f"""
            SELECT pc.id, pc.tenant_id, t.contact_email, t.product, t.active,
                   pc.username_lc, pc.cloud_capture_enabled,
                   (pc.secret_enc IS NOT NULL) AS has_secret,
                   (pc.session_state_enc IS NOT NULL) AS has_session,
                   pc.session_state_at, pc.last_harvest_at, pc.last_harvest_ok,
                   pc.harvest_fails, pc.created_at, pc.updated_at,
                   pls.fails AS roster_fails, pls.paused AS roster_paused,
                   pls.last_ok_at AS roster_last_ok_at
            FROM portal_credential pc
            JOIN tenants t ON t.id = pc.tenant_id
            LEFT JOIN portal_login_status pls
              ON pls.tenant_id = pc.tenant_id AND pls.provider = pc.provider
             AND pls.username_lc = pc.username_lc
            WHERE {' AND '.join(where)}
            ORDER BY pc.updated_at DESC
        """), params).mappings().all()
        run_where = ["provider = :prov"]
        if ulc:
            run_where.append("username_lc = :ulc")
        if tenant_id:
            run_where.append("tenant_id = :tid")
        runs = db.execute(text(f"""
            SELECT started_at, ended_at, tenant_id, username_lc, status,
                   logged_in_fresh, rows_written, detail
            FROM harvest_run
            WHERE {' AND '.join(run_where)}
            ORDER BY started_at DESC
            LIMIT :lim
        """), params).mappings().all()

    def _row(m) -> dict:
        out = {}
        for k, v in dict(m).items():
            out[k] = v.isoformat() if isinstance(v, datetime) else v
        return out

    return JSONResponse({
        "as_of": datetime.utcnow().isoformat() + "Z",
        "provider": prov,
        "username": ulc,
        "tenant_id": tenant_id,
        "credentials": [_row(c) for c in creds],
        "runs": [_row(r) for r in runs],
    })
