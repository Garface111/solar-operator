"""HARD RESET: one client per login, named after the login, holding that
login's arrays.

Ford, 2026-07-24: "every single login which has some amount of arrays under it
creates a new client, and the name of that client is the login" — utility and
vendor alike. This is the new DEFAULT shape; operators re-organize from there.

Dry-run by default. Nothing is deleted: a client left holding no arrays is
retired (active=False), so the whole reset is reversible by hand.

    python -m scripts.reset_clients_by_login --tenant ten_x            # plan
    python -m scripts.reset_clients_by_login --tenant ten_x --execute  # apply
    python -m scripts.reset_clients_by_login --all-reports-world       # plan all

HOW A LOGIN'S ARRAYS ARE DETERMINED
  vendor   — authoritative. InverterConnection rows carry both the credential
             (config.username, or a masked key handle) and the array_id.
  utility  — inferred. NOTHING in the schema records which credential captured
             a given UtilityAccount (login_origin_client_id is NULL fleet-wide),
             so: if a provider has exactly ONE saved credential, every account
             of that provider belongs to it. If a provider has 2+ credentials
             we do NOT guess — those arrays are reported as AMBIGUOUS and left
             exactly where they are.

PRECEDENCE when an array is reachable by several logins: a utility login wins
over a monitoring login, because generation reports settle on metered utility
data and the utility login is the one that owns the billing relationship.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from sqlalchemy import select, text

from api import events
from api.db import SessionLocal
from api.models import (
    Array,
    Client,
    InverterConnection,
    Tenant,
    UtilityAccount,
)
from api.report_eligibility import tenant_in_reports_world

VENDOR_KINDS = ("locus", "alsoenergy", "solaredge", "fronius", "sma", "chint",
                "enphase", "solis", "tigo")


def _key_handle(vendor: str, config: dict) -> str | None:
    """Non-secret identifier for a vendor credential."""
    cfg = config or {}
    user = str(cfg.get("username") or "").strip()
    if user:
        return user
    key = str(cfg.get("api_key") or "").strip()
    return f"{vendor} key ••••{key[-4:]}" if len(key) >= 4 else None


def _portal_credentials(db, tenant_id: str) -> list[tuple[str, str]]:
    """(provider, username) for every saved utility credential. Raw SQL and
    only non-secret columns — the password column refuses to decrypt outside
    the harvester role, and we never need it here."""
    rows = db.execute(text(
        "select provider, username from portal_credential where tenant_id=:t"
    ), {"t": tenant_id}).mappings().all()
    return [(r["provider"], r["username"]) for r in rows if r["username"]]


def plan_tenant(db, tenant_id: str) -> dict:
    """Work out the target shape. Pure read — returns a plan, changes nothing."""
    arrays = db.execute(select(Array).where(
        Array.tenant_id == tenant_id, Array.deleted_at.is_(None)
    )).scalars().all()
    arrays_by_id = {a.id: a for a in arrays}
    clients = {c.id: c for c in db.execute(select(Client).where(
        Client.tenant_id == tenant_id)).scalars().all()}

    # ── which login owns which array ──────────────────────────────────────
    # login key -> {"label", "kind", "provider", "arrays": set[int]}
    logins: dict[str, dict] = {}

    def _login(key: str, label: str, kind: str, provider: str) -> dict:
        return logins.setdefault(key, {
            "label": label, "kind": kind, "provider": provider, "arrays": set(),
        })

    # Vendor: authoritative (the connection names both credential and array).
    for conn, arr in db.execute(
        select(InverterConnection, Array)
        .join(Array, Array.id == InverterConnection.array_id)
        .where(Array.tenant_id == tenant_id, Array.deleted_at.is_(None))
    ).all():
        handle = _key_handle(conn.vendor, conn.config)
        if not handle:
            continue
        _login(f"{conn.vendor}:{handle.lower()}", handle, "vendor",
               conn.vendor)["arrays"].add(arr.id)

    # Utility: inferred, and only when unambiguous.
    creds = _portal_credentials(db, tenant_id)
    by_provider: dict[str, list[str]] = defaultdict(list)
    for provider, username in creds:
        if provider in VENDOR_KINDS:
            continue  # a monitoring portal saved in the vault — vendor side owns it
        by_provider[provider].append(username)

    accounts = db.execute(select(UtilityAccount).where(
        UtilityAccount.tenant_id == tenant_id)).scalars().all()
    accts_by_provider: dict[str, list[UtilityAccount]] = defaultdict(list)
    for acct in accounts:
        if acct.array_id and acct.array_id in arrays_by_id:
            accts_by_provider[acct.provider].append(acct)

    ambiguous: list[dict] = []
    for provider, accts in accts_by_provider.items():
        usernames = by_provider.get(provider) or []
        if len(usernames) == 1:
            key = f"{provider}:{usernames[0].lower()}"
            entry = _login(key, usernames[0], "utility", provider)
            for a in accts:
                entry["arrays"].add(a.array_id)
        elif len(usernames) > 1:
            ambiguous.append({
                "provider": provider,
                "logins": usernames,
                "arrays": sorted({a.array_id for a in accts}),
            })
        # 0 saved credentials: captured by the extension with nothing in the
        # vault — leave those arrays alone rather than invent an owner.

    # ── precedence: a utility login outranks a monitoring login ───────────
    owner: dict[int, str] = {}
    for key, entry in logins.items():
        for aid in entry["arrays"]:
            cur = owner.get(aid)
            if cur is None:
                owner[aid] = key
            elif logins[cur]["kind"] == "vendor" and entry["kind"] == "utility":
                owner[aid] = key

    # Ambiguous arrays keep their current client no matter what.
    ambiguous_ids = {aid for a in ambiguous for aid in a["arrays"]}
    for aid in ambiguous_ids:
        owner.pop(aid, None)

    # ── name each client, de-duping when one username spans providers ─────
    used_names: dict[str, str] = {}   # lowercased name -> login key
    names: dict[str, str] = {}
    for key in sorted(logins):
        if not any(owner.get(a) == key for a in logins[key]["arrays"]):
            continue  # this login ends up owning nothing
        label = logins[key]["label"]
        name = label
        if name.lower() in used_names:
            name = f"{label} ({logins[key]['provider'].upper()})"
        used_names[name.lower()] = key
        names[key] = name[:200]

    moves: list[dict] = []
    for aid, key in sorted(owner.items()):
        arr = arrays_by_id[aid]
        target_name = names.get(key)
        if not target_name:
            continue
        cur = clients.get(arr.client_id) if arr.client_id else None
        if cur is not None and cur.name == target_name:
            continue  # already right
        moves.append({
            "array_id": aid, "array": arr.name,
            "from": cur.name if cur else None,
            "from_id": arr.client_id,
            "to": target_name, "login_key": key,
        })

    kept = {names[k] for k in names}
    retire = [
        {"id": c.id, "name": c.name}
        for c in clients.values()
        if c.active and c.deleted_at is None and c.name not in kept
        and not any(
            a.client_id == c.id and a.id not in owner for a in arrays
        )
    ]

    return {
        "tenant_id": tenant_id,
        "arrays": len(arrays),
        "logins": [
            {"key": k, "name": names[k], "kind": logins[k]["kind"],
             "arrays": sum(1 for a in owner.values() if a == k)}
            for k in sorted(names)
        ],
        "moves": moves,
        "ambiguous": ambiguous,
        "unowned": sorted(set(arrays_by_id) - set(owner) - ambiguous_ids),
        "retire": retire,
    }


def apply_plan(db, tenant_id: str, plan: dict) -> None:
    """Apply in ONE transaction: create/find clients, move arrays, retire empties."""
    clients = {c.name: c for c in db.execute(select(Client).where(
        Client.tenant_id == tenant_id, Client.deleted_at.is_(None))).scalars().all()}

    for entry in plan["logins"]:
        name = entry["name"]
        c = clients.get(name)
        if c is None:
            c = Client(tenant_id=tenant_id, name=name, active=True)
            db.add(c)
            db.flush()
            clients[name] = c
        elif not c.active:
            c.active = True   # a login's client must be visible

    for mv in plan["moves"]:
        arr = db.get(Array, mv["array_id"])
        target = clients[mv["to"]]
        # Carry report settings forward the first time a client gains an array
        # from an older client, so cadence/contact aren't silently lost.
        if mv["from_id"]:
            old = db.get(Client, mv["from_id"])
            if old is not None:
                if not target.contact_email and old.contact_email:
                    target.contact_email = old.contact_email
                if getattr(old, "report_frequency", None) and not getattr(
                        target, "report_frequency", None):
                    target.report_frequency = old.report_frequency
        arr.client_id = target.id

    for r in plan["retire"]:
        c = db.get(Client, r["id"])
        if c is not None:
            c.active = False


def print_plan(plan: dict) -> None:
    print(f"\n=== {plan['tenant_id']} — {plan['arrays']} live arrays ===")
    print(f"  clients after reset: {len(plan['logins'])}")
    for e in plan["logins"]:
        print(f"    [{e['kind']:7}] {e['name']!r} → {e['arrays']} arrays")
    print(f"  array moves: {len(plan['moves'])}")
    for m in plan["moves"][:12]:
        print(f"    {m['array']!r}: {m['from']!r} → {m['to']!r}")
    if len(plan["moves"]) > 12:
        print(f"    … {len(plan['moves']) - 12} more")
    if plan["ambiguous"]:
        print("  ⚠ AMBIGUOUS (2+ logins for one provider — LEFT ALONE):")
        for a in plan["ambiguous"]:
            print(f"    {a['provider']}: logins={a['logins']} "
                  f"{len(a['arrays'])} arrays untouched")
    if plan["unowned"]:
        print(f"  ⓘ {len(plan['unowned'])} arrays reach no saved login "
              f"(manual/CSV/extension-only) — left alone")
    if plan["retire"]:
        print(f"  clients retired (0 arrays, reversible): "
              f"{[r['name'] for r in plan['retire']]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", action="append", default=[])
    ap.add_argument("--all-reports-world", action="store_true")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    with SessionLocal() as db:
        if args.all_reports_world:
            tenants = [
                t.id for t in db.execute(
                    select(Tenant).where(Tenant.active.is_(True))
                ).scalars().all() if tenant_in_reports_world(t)
            ]
        else:
            tenants = args.tenant
    if not tenants:
        print("nothing to do — pass --tenant or --all-reports-world")
        return 1

    for tid in tenants:
        with SessionLocal() as db:
            plan = plan_tenant(db, tid)
            print_plan(plan)
            if args.execute:
                apply_plan(db, tid, plan)
                db.commit()
                # Post-commit: the reset reshapes the tenant's whole client/
                # array graph — tell any open dashboard to refetch.
                events.publish(tid, "clients.changed")
                events.publish(tid, "arrays.changed")
                print("  ✅ APPLIED")
            else:
                print("  (dry run — pass --execute to apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
