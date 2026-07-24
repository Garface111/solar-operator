"""The Discover pool — everything we can SEE through a saved login, staged for
the operator to curate before it becomes a real Client/Array.

WHY THIS EXISTS (Ford, 2026-07-23). Connecting a monitoring login used to import
every site it could see. But a Locus **partner** login spans multiple operators'
sites, so "connect and import" put "Benson Site"/"Tinker Hall Site" under three
different tenants at once. Discovery is now a READ that lands in this staging
pool; nothing enters the operator's system until they pick it.

The pool is also the answer to "no more messing around with logins": a login is
entered once, its secrets stay in the vault (InverterConnection.config), and the
nightly refresh re-reads it — so new sites show up on their own and the operator
never re-types a password to look around.

Two source kinds:
  * vendor  — monitoring portals with a real discovery API (Locus, AlsoEnergy,
              SolarEdge). Refresh calls the vendor over the network.
  * utility — bill portals (GMP, VEC, co-ops). Their accounts are already
              captured into UtilityAccount, so refresh is a pure local read;
              an account with no array_id is a candidate.

Nothing in this module stores a secret: `source_login` is a username (or, for
SolarEdge's key-only auth, a masked handle) and is only ever used to re-find the
credential that already lives in the vault.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from . import inverters
from .inverters.base import InverterAuthError, InverterError
from .models import (
    Array,
    Client,
    DiscoveredCandidate,
    DiscoveryLoginState,
    InverterConnection,
    UtilityAccount,
    now,
)

log = logging.getLogger(__name__)

# Vendors whose adapter implements discover_sites(config) -> [{site_id, name, …}].
VENDOR_DISCOVERABLE = ("locus", "alsoenergy", "solaredge")

PROVIDER_LABELS = {
    "locus": "Locus (SolarNOC)",
    "alsoenergy": "AlsoEnergy PowerTrack",
    "solaredge": "SolarEdge",
    "gmp": "Green Mountain Power",
    "vec": "Vermont Electric Coop",
}


def provider_label(provider: str) -> str:
    """Human name for a provider code, falling back to a title-cased slug."""
    p = (provider or "").strip()
    return PROVIDER_LABELS.get(p, p.replace("_", " ").title() or "Utility")


def login_key(provider: str, source_login: str) -> str:
    """Stable id for one (provider, login) group — what the UI round-trips."""
    return f"{provider}:{source_login}"


def credential_handle(vendor: str, config: dict) -> str | None:
    """A stable, NON-SECRET identifier for a saved vendor credential.

    Username for the login-based vendors; for SolarEdge (API-key auth, no
    username) a masked tail so the operator can tell two keys apart without the
    key itself ever being stored in, or served from, the pool.
    """
    cfg = config or {}
    if vendor in ("locus", "alsoenergy"):
        user = str(cfg.get("username") or "").strip()
        return user or None
    if vendor == "solaredge":
        key = str(cfg.get("api_key") or "").strip()
        return f"key ••••{key[-4:]}" if len(key) >= 4 else None
    return None


# ─── reading the saved logins ──────────────────────────────────────────────


def _vendor_logins(db, tenant_id: str) -> dict[tuple[str, str], dict]:
    """Every distinct vendor credential saved under this tenant.

    Returns {(vendor, handle): config}. Arrays share a credential (one partner
    login covers many sites), so we de-duplicate down to the credential — one
    discovery call per LOGIN, not per array.
    """
    rows = db.execute(
        select(InverterConnection, Array)
        .join(Array, Array.id == InverterConnection.array_id)
        .where(
            Array.tenant_id == tenant_id,
            Array.deleted_at.is_(None),
            InverterConnection.vendor.in_(VENDOR_DISCOVERABLE),
        )
    ).all()

    out: dict[tuple[str, str], dict] = {}
    for conn, _arr in rows:
        handle = credential_handle(conn.vendor, conn.config)
        if not handle:
            continue
        # First one wins; they're the same credential by construction.
        out.setdefault((conn.vendor, handle), dict(conn.config or {}))
    return out


def _utility_logins(db, tenant_id: str) -> dict[tuple[str, str], list[UtilityAccount]]:
    """Utility accounts already captured, grouped by (provider, login handle).

    The handle is the customer number when the provider gives one (it IS the
    login identity — every account captured under one sign-in shares it), else
    the provider itself so single-login utilities still group sanely.
    """
    accounts = db.execute(
        select(UtilityAccount).where(UtilityAccount.tenant_id == tenant_id)
    ).scalars().all()

    out: dict[tuple[str, str], list[UtilityAccount]] = {}
    for acct in accounts:
        if getattr(acct, "deleted_at", None) is not None:
            continue
        handle = (acct.customer_number or "").strip() or provider_label(acct.provider)
        out.setdefault((acct.provider, handle), []).append(acct)
    return out


# ─── upsert + reconcile ────────────────────────────────────────────────────


def _login_state(db, tenant_id: str, provider: str, kind: str, handle: str) -> DiscoveryLoginState:
    st = db.execute(
        select(DiscoveryLoginState).where(
            DiscoveryLoginState.tenant_id == tenant_id,
            DiscoveryLoginState.provider == provider,
            DiscoveryLoginState.source_login == handle,
        )
    ).scalar_one_or_none()
    if st is None:
        st = DiscoveryLoginState(
            tenant_id=tenant_id, provider=provider,
            source_kind=kind, source_login=handle,
        )
        db.add(st)
        db.flush()
    st.source_kind = kind
    return st


def _upsert_candidate(
    db,
    *,
    tenant_id: str,
    provider: str,
    kind: str,
    handle: str,
    external_id: str,
    name: str,
    peak_power_kw: float | None,
    seen_at: datetime,
) -> DiscoveredCandidate:
    """Insert or refresh one candidate.

    The operator's verdict (`status`) is never clobbered here — a refresh only
    updates what the PROVIDER owns (name, size, last-seen).
    """
    row = db.execute(
        select(DiscoveredCandidate).where(
            DiscoveredCandidate.tenant_id == tenant_id,
            DiscoveredCandidate.provider == provider,
            DiscoveredCandidate.source_login == handle,
            DiscoveredCandidate.external_id == str(external_id),
        )
    ).scalar_one_or_none()
    if row is None:
        row = DiscoveredCandidate(
            tenant_id=tenant_id, provider=provider, source_kind=kind,
            source_login=handle, external_id=str(external_id), name=name,
            peak_power_kw=peak_power_kw, status="new",
            first_seen_at=seen_at, last_seen_at=seen_at,
        )
        db.add(row)
        db.flush()
        return row
    row.name = name or row.name
    if peak_power_kw is not None:
        row.peak_power_kw = peak_power_kw
    row.source_kind = kind
    row.last_seen_at = seen_at
    return row


def _reconcile_vendor_status(db, tenant_id: str, row: DiscoveredCandidate) -> None:
    """Mark a candidate `imported` when a live array already carries its site.

    Keeps the pool honest about arrays that arrived by any other route (the
    Add-client flow, an older connect-account run, a migration) so the operator
    never sees a duplicate offered for import. `ignored` is the operator's word
    and always wins.
    """
    if row.status == "ignored":
        return
    hit = None
    for conn, arr in db.execute(
        select(InverterConnection, Array)
        .join(Array, Array.id == InverterConnection.array_id)
        .where(
            Array.tenant_id == tenant_id,
            Array.deleted_at.is_(None),
            InverterConnection.vendor == row.provider,
        )
    ).all():
        if str((conn.config or {}).get("site_id") or "") == row.external_id:
            hit = arr
            break
    if hit is not None:
        row.status = "imported"
        row.imported_array_id = hit.id
        row.imported_client_id = hit.client_id
    elif row.status == "imported":
        # The array it pointed at is gone — offer it again rather than
        # stranding a row that claims to be in a system it isn't in.
        row.status = "new"
        row.imported_array_id = None
        row.imported_client_id = None


def refresh_tenant(db, tenant_id: str, only_login_key: str | None = None) -> dict:
    """Re-read the tenant's saved logins into the pool.

    One failing login (expired password, vendor 5xx) records its error and the
    sweep continues — a stale Locus password must not hide the GMP accounts.
    Returns a summary the UI can narrate.
    """
    seen_at = now()
    refreshed = 0
    found = 0
    errors: list[dict] = []

    for (vendor, handle), config in _vendor_logins(db, tenant_id).items():
        key = login_key(vendor, handle)
        if only_login_key and key != only_login_key:
            continue
        state = _login_state(db, tenant_id, vendor, "vendor", handle)
        state.last_attempt_at = seen_at
        try:
            module = inverters.get_vendor(vendor)
            if not hasattr(module, "discover_sites"):
                continue  # vendor gained a connection but not account discovery
            sites = module.discover_sites(config)
        except (InverterAuthError, InverterError) as exc:
            state.last_error = str(exc)[:500]
            errors.append({"login_key": key, "error": str(exc)})
            log.info("discovery: %s login %s failed: %s", vendor, handle, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — one login must not kill the sweep
            state.last_error = f"Unexpected error: {exc}"[:500]
            errors.append({"login_key": key, "error": str(exc)})
            log.exception("discovery: unexpected failure for %s/%s", vendor, handle)
            continue

        for site in sites or []:
            sid = site.get("site_id")
            if sid is None:
                continue
            row = _upsert_candidate(
                db, tenant_id=tenant_id, provider=vendor, kind="vendor",
                handle=handle, external_id=str(sid),
                name=str(site.get("name") or f"Site {sid}"),
                peak_power_kw=site.get("peak_power_kw"), seen_at=seen_at,
            )
            _reconcile_vendor_status(db, tenant_id, row)
            found += 1
        state.last_ok_at = seen_at
        state.last_error = None
        refreshed += 1

    # Utility accounts: already captured locally, so this is a read, not a call.
    for (provider, handle), accounts in _utility_logins(db, tenant_id).items():
        key = login_key(provider, handle)
        if only_login_key and key != only_login_key:
            continue
        state = _login_state(db, tenant_id, provider, "utility", handle)
        state.last_attempt_at = seen_at
        for acct in accounts:
            row = _upsert_candidate(
                db, tenant_id=tenant_id, provider=provider, kind="utility",
                handle=handle, external_id=str(acct.account_number),
                name=str(acct.nickname or acct.account_number),
                peak_power_kw=None, seen_at=seen_at,
            )
            if row.status != "ignored":
                arr = (
                    db.get(Array, acct.array_id) if acct.array_id else None
                )
                if arr is not None and arr.deleted_at is None:
                    row.status = "imported"
                    row.imported_array_id = arr.id
                    row.imported_client_id = arr.client_id
                elif row.status == "imported":
                    row.status = "new"
                    row.imported_array_id = None
                    row.imported_client_id = None
            found += 1
        state.last_ok_at = seen_at
        state.last_error = None
        refreshed += 1

    db.commit()
    return {"ok": True, "refreshed": refreshed, "found": found, "errors": errors}


# ─── reading the pool for the UI ───────────────────────────────────────────


def list_pool(db, tenant_id: str) -> dict:
    """The whole pool, grouped by login, newest-refreshed first."""
    rows = db.execute(
        select(DiscoveredCandidate)
        .where(DiscoveredCandidate.tenant_id == tenant_id)
        .order_by(DiscoveredCandidate.name)
    ).scalars().all()
    states = {
        (s.provider, s.source_login): s
        for s in db.execute(
            select(DiscoveryLoginState).where(
                DiscoveryLoginState.tenant_id == tenant_id
            )
        ).scalars().all()
    }
    # Retired clients keep their name here (the array really IS in the system)
    # but are flagged, so the UI can say "Bruce Genereaux (retired)" instead of
    # pointing at a client the roster doesn't show — see rosterFilter.ts.
    clients_by_id = {
        c.id: c
        for c in db.execute(
            select(Client).where(Client.tenant_id == tenant_id)
        ).scalars().all()
    }

    groups: dict[str, dict] = {}
    for row in rows:
        key = login_key(row.provider, row.source_login)
        g = groups.get(key)
        if g is None:
            st = states.get((row.provider, row.source_login))
            g = groups[key] = {
                "key": key,
                "provider": row.provider,
                "provider_label": provider_label(row.provider),
                "source_kind": row.source_kind,
                "login": row.source_login,
                "last_seen_at": st.last_ok_at.isoformat() if st and st.last_ok_at else None,
                "last_error": st.last_error if st else None,
                "counts": {"new": 0, "imported": 0, "ignored": 0},
                "candidates": [],
            }
        g["counts"][row.status] = g["counts"].get(row.status, 0) + 1
        g["candidates"].append({
            "id": row.id,
            "name": row.name,
            "external_id": row.external_id,
            "peak_power_kw": row.peak_power_kw,
            "status": row.status,
            "imported_array_id": row.imported_array_id,
            "imported_client_id": row.imported_client_id,
            "imported_client_name": (
                clients_by_id[row.imported_client_id].name
                if row.imported_client_id in clients_by_id else None
            ),
            "imported_client_active": (
                bool(clients_by_id[row.imported_client_id].active)
                if row.imported_client_id in clients_by_id else None
            ),
            "suggested_client": _suggested_client_name(row.source_login),
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        })

    # Logins we know about that returned nothing yet (or only errors) still
    # deserve a row — otherwise a broken password looks like "no logins".
    for (provider, handle), st in states.items():
        key = login_key(provider, handle)
        if key in groups:
            continue
        groups[key] = {
            "key": key,
            "provider": provider,
            "provider_label": provider_label(provider),
            "source_kind": st.source_kind,
            "login": handle,
            "last_seen_at": st.last_ok_at.isoformat() if st.last_ok_at else None,
            "last_error": st.last_error,
            "counts": {"new": 0, "imported": 0, "ignored": 0},
            "candidates": [],
        }

    ok_times = [s.last_ok_at for s in states.values() if s.last_ok_at]
    return {
        "ok": True,
        "refreshed_at": max(ok_times).isoformat() if ok_times else None,
        "logins": sorted(groups.values(), key=lambda g: (g["provider_label"], g["login"])),
    }


class DiscoveryImportError(Exception):
    """The import can't proceed as asked (bad client, nothing importable)."""


def set_ignored(db, tenant_id: str, candidate_ids: list[int], ignored: bool) -> dict:
    """Hide or un-hide candidates. Never touches an already-imported row —
    a real array must be deleted through the normal path, not hidden here.
    """
    rows = db.execute(
        select(DiscoveredCandidate).where(
            DiscoveredCandidate.tenant_id == tenant_id,
            DiscoveredCandidate.id.in_(candidate_ids or [0]),
        )
    ).scalars().all()
    updated = 0
    for row in rows:
        if row.status == "imported":
            continue
        new_status = "ignored" if ignored else "new"
        if row.status != new_status:
            row.status = new_status
            updated += 1
    db.commit()
    return {"ok": True, "updated": updated}


def import_candidates(
    db,
    tenant_id: str,
    candidate_ids: list[int],
    *,
    client_id: int | None = None,
    client_name: str | None = None,
) -> dict:
    """Promote picked candidates into real arrays under ONE client.

    Vendor candidates get a fresh Array + an InverterConnection carrying the
    same saved credential (so the nightly pull just works, and the deep-history
    backfill fires). Utility candidates re-point the already-captured
    UtilityAccount at a new Array. Already-imported rows are skipped, not
    duplicated, so a double-click is harmless.
    """
    from .array_owners import _trigger_history_backfill  # noqa: PLC0415

    rows = db.execute(
        select(DiscoveredCandidate).where(
            DiscoveredCandidate.tenant_id == tenant_id,
            DiscoveredCandidate.id.in_(candidate_ids or [0]),
        )
    ).scalars().all()
    if not rows:
        raise DiscoveryImportError("Those sites are no longer in the pool.")

    # Resolve the destination client: an existing one (verified to be this
    # tenant's) or a new one named by the operator / suggested from the login.
    client: Client | None = None
    if client_id is not None:
        client = db.execute(
            select(Client).where(
                Client.id == client_id,
                Client.tenant_id == tenant_id,
                Client.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if client is None:
            raise DiscoveryImportError("That client no longer exists.")
    else:
        name = (client_name or "").strip() or _suggested_client_name(rows[0].source_login)
        client = db.execute(
            select(Client).where(
                Client.tenant_id == tenant_id,
                Client.name == name,
                Client.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if client is None:
            client = Client(tenant_id=tenant_id, name=name, active=True)
            db.add(client)
            db.flush()

    # Array names are unique per tenant INCLUDING soft-deleted rows, so collect
    # every taken name up front and disambiguate rather than blow up mid-import.
    taken = {
        (n or "").strip().lower()
        for (n,) in db.execute(select(Array.name).where(Array.tenant_id == tenant_id)).all()
    }

    imported: list[dict] = []
    skipped: list[dict] = []
    backfill_ids: list[int] = []
    vendor_configs = _vendor_logins(db, tenant_id)

    for row in rows:
        if row.status == "imported" and row.imported_array_id:
            skipped.append({"candidate_id": row.id, "reason": "already in your system"})
            continue

        name = row.name
        if name.strip().lower() in taken:
            name = f"{row.name} ({row.external_id})"
        arr = Array(
            tenant_id=tenant_id, name=name, client_id=client.id, fuel_type="solar",
        )
        db.add(arr)
        db.flush()
        taken.add(name.strip().lower())

        if row.source_kind == "vendor":
            config = vendor_configs.get((row.provider, row.source_login))
            if config is None:
                db.delete(arr)
                skipped.append({
                    "candidate_id": row.id,
                    "reason": "that login is no longer saved — reconnect it",
                })
                continue
            db.add(InverterConnection(
                array_id=arr.id, vendor=row.provider,
                config={**config, "site_id": _coerce_site_id(row.external_id)},
                status="ok",
            ))
            backfill_ids.append(arr.id)
        else:
            acct = db.execute(
                select(UtilityAccount).where(
                    UtilityAccount.tenant_id == tenant_id,
                    UtilityAccount.provider == row.provider,
                    UtilityAccount.account_number == row.external_id,
                )
            ).scalars().first()
            if acct is None:
                db.delete(arr)
                skipped.append({
                    "candidate_id": row.id,
                    "reason": "that utility account is no longer captured",
                })
                continue
            acct.array_id = arr.id

        row.status = "imported"
        row.imported_array_id = arr.id
        row.imported_client_id = client.id
        imported.append({"candidate_id": row.id, "array_id": arr.id, "name": name})

    db.commit()

    # Pull each new vendor array's history AFTER the commit so a slow backfill
    # can't hold the import transaction open.
    for array_id in backfill_ids:
        try:
            _trigger_history_backfill(db, array_id)
        except Exception:  # noqa: BLE001 — best-effort; the nightly pull retries
            log.exception("discovery: history backfill failed for array %s", array_id)

    n = len(imported)
    message = (
        f"Added {n} array{'' if n == 1 else 's'} to {client.name}."
        if n else "Nothing new to add — those are already in your system."
    )
    if skipped:
        message += f" Skipped {len(skipped)}."
    return {
        "ok": True,
        "client": {"id": client.id, "name": client.name},
        "imported": imported,
        "skipped": skipped,
        "message": message,
    }


def _coerce_site_id(external_id: str):
    """Vendor site ids are ints in every adapter's config; keep the string form
    only if a provider ever hands us a non-numeric id."""
    try:
        return int(external_id)
    except (TypeError, ValueError):
        return external_id


def _suggested_client_name(source_login: str) -> str:
    """Default client name for a login — mirrors cloud_capture._name_from_login."""
    s = (source_login or "").strip()
    local = s.split("@")[0] if "@" in s else s
    cleaned = local.replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return (cleaned.title()[:200] or s[:200] or "New client")
