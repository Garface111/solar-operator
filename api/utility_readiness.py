"""Offline portfolio triage. A collector/catalog entry never certifies invoicing.

No network, credential access, database writes, discovery or email side effects.
Runtime billing gates remain in billing.delivery; this reports integration work.
"""
from __future__ import annotations

from collections import Counter
import re
from urllib.parse import urlsplit

from .providers import PROVIDERS, get_provider

BASES = {"bill_credit", "production_kwh", "allocation_statement", "unknown"}
ROLES = {"array_generation", "offtaker_credit", "both", "unknown"}
FAMILIES = {
    "gmp": {
        "collector": "api.harvester.vendors.gmp / api.adapters.gmp",
        "outputs": ["accounts", "bill_json", "bill_pdf", "meter_generation"],
        "billing_paths": ["bill_credit"],
        "limitation": "Confirm the actual account, generation meter, credit lines and contract rate.",
    },
    "smarthub": {
        "collector": "api.harvester.vendors.smarthub",
        "outputs": ["accounts", "bill_history", "bill_pdf", "meter_readings"],
        "billing_paths": ["production_kwh", "bill_credit"],
        "limitation": "Qualify each utility and meter. Bill-credit mode requires actual statement credit/excess semantics and a verified parser. Usage, export and prorated bill totals are not measured generation. Monthly only until quarterly support is implemented.",
    },
    "eversource": {
        "collector": "api.harvester.vendors.eversource",
        "outputs": ["accounts", "meter_candidates"],
        "billing_paths": [],
        "limitation": "The current collector emits no bill records. Login or meter capture does not establish bill-credit extraction.",
    },
    "cmp": {
        "collector": "api.harvester.vendors.cmp",
        "outputs": ["accounts", "meter_candidates"],
        "billing_paths": [],
        "limitation": "The current collector emits no bill records. Community allocation statements require a separate verified source and mapping.",
    },
    "unknown": {
        "collector": None, "outputs": [], "billing_paths": [],
        "limitation": "Discover the official portal and source contract before building or enabling a connector.",
    },
}


class IntakeError(ValueError):
    pass


def _portal_host(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port not in (None, 443)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                or "." not in host or ".." in host
                or re.fullmatch(r"[0-9.]+", host)):
            raise ValueError()
        return host
    except ValueError as exc:
        raise IntakeError("Use a public HTTPS portal address without credentials, query parameters or fragments") from exc


def _is_smarthub(host: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.smarthub\.coop", host))


def assess_utility(provider_code="", utility_name="", state="", portal_url="", billing_basis="unknown", cadence="monthly"):
    """Resolve only exact catalog identity. Similar names never authorize a match."""
    code = str(provider_code or "").strip().lower()
    label = str(utility_name or "").strip()
    region = str(state or "").strip().upper()
    basis = str(billing_basis or "unknown").strip().lower()
    cadence = str(cadence or "monthly").strip().lower()
    if basis not in BASES:
        raise IntakeError("billing_basis must be bill_credit, production_kwh, allocation_statement or unknown")
    if cadence not in {"monthly", "quarterly", "annual", "unknown"}:
        raise IntakeError("cadence must be monthly, quarterly, annual or unknown")
    host = _portal_host(str(portal_url or "").strip())
    provider = get_provider(code) if code else None
    issues = []
    label_matches = [p for p in PROVIDERS if label and p["label"].casefold() == label.casefold()]
    if code and label_matches and code not in {p["code"] for p in label_matches}:
        issues.append("utility_name_identity_mismatch")
    if not code and label:
        matches = [p for p in PROVIDERS if p["label"].casefold() == label.casefold()
                   and (not region or p["state"] == region)]
        if len(matches) == 1:
            provider = matches[0]
            code = provider["code"]
    # Official catalog host can identify a utility, but conflicting supplied
    # identity must be reviewed rather than silently replaced by that host.
    by_host = [p for p in PROVIDERS if p.get("smarthub_host") == host] if host else []
    if not provider and not code and len(by_host) == 1:
        provider = by_host[0]
        code = provider["code"]
    if provider and region and provider["state"] and provider["state"] != region:
        issues.append("state_identity_mismatch")
    if by_host and code and by_host[0]["code"] != code:
        issues.append("portal_identity_mismatch")
    if provider and provider.get("smarthub_host"):
        if host and host != provider["smarthub_host"]:
            issues.append("portal_identity_mismatch")
        host = host or provider["smarthub_host"]
    elif provider and host and provider.get("portal_url"):
        official = _portal_host(provider["portal_url"])
        # Bespoke login redirects can legitimately use different hosts; record
        # the difference for verification rather than infer authorization.
        if host.removeprefix("www.") != official.removeprefix("www."):
            issues.append("portal_identity_unverified")
    if code == "gmp":
        family = "gmp"
    elif code in {"eversource", "eversource_ma", "eversource_ct"}:
        family = "eversource"
    elif code == "cmp":
        family = "cmp"
    elif _is_smarthub(host):
        family = "smarthub"
    else:
        family = "unknown"
    profile = FAMILIES[family]
    if issues:
        route = "verify_identity"
    elif cadence == "unknown":
        route = "confirm_cadence"
    elif basis == "unknown":
        route = "confirm_billing_source"
    elif family == "unknown":
        route = "new_connector"
    elif cadence == "annual" or basis not in profile["billing_paths"] or (family == "smarthub" and cadence != "monthly"):
        route = "add_billing_evidence_path"
    else:
        route = "qualify_existing_family"
    return {
        "provider_code": code or None, "utility_name": label or (provider or {}).get("label") or "Unknown utility",
        "state": region or (provider or {}).get("state"), "portal_host": host or None,
        "family": family, "catalog_status": (provider or {}).get("scrape_status", "not_cataloged"),
        "collector": profile["collector"], "capture_outputs": list(profile["outputs"]),
        "billing_basis": basis, "cadence": cadence, "integration_route": route,
        "identity_issues": sorted(set(issues)), "limitation": profile["limitation"],
        "invoice_ready": False, "qualification": "not_assessed_on_real_accounts",
        "required_evidence": [
            "official_utility_and_portal_identity", "authorized_access_and_mfa_recovery",
            "account_meter_and_offtaker_mapping", "two_closed_cycles_with_original_source_files",
            "independently_checked_amounts_units_and_contract", "relogin_and_repeat_capture",
            "incomplete_wrong_account_and_duplicate_rejection", "reviewed_live_invoice_canary",
        ],
    }


def analyze_roster(rows):
    """One row per utility/source/billing group; counts are supplied, not guessed."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise IntakeError("Supply between 1 and 5000 utility/source groups")
    groups, seen = [], set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise IntakeError(f"Row {index}: expected an object")
        group = str(row.get("group_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", group) or group in seen:
            raise IntakeError(f"Row {index}: group_id must be unique and contain only letters, digits, underscore or dash")
        seen.add(group)
        count = str(row.get("offtaker_count") or "").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,5}", count):
            raise IntakeError(f"Row {index}: offtaker_count must be a positive whole number")
        role = str(row.get("source_role") or "unknown").strip().lower()
        if role not in ROLES:
            raise IntakeError(f"Row {index}: invalid source_role")
        try:
            item = assess_utility(**{key: row.get(key) for key in (
                "provider_code", "utility_name", "state", "portal_url", "billing_basis", "cadence")})
        except IntakeError as exc:
            raise IntakeError(f"Row {index}: {exc}") from exc
        item.update(group_id=group, offtaker_count=int(count), source_role=role)
        if role == "unknown":
            item["identity_issues"].append("source_role_unconfirmed")
            item["integration_route"] = "confirm_billing_source"
        groups.append(item)
    groups.sort(key=lambda x: (-x["offtaker_count"], x["group_id"]))
    return {
        "schema_version": 1, "purpose": "planning_only",
        "group_count": len(groups), "offtaker_count": sum(g["offtaker_count"] for g in groups),
        "invoice_ready_count": 0,
        "count_note": "Counts sum the supplied groups. Keep groups disjoint; this tool cannot establish unique customers without Norwich's roster.",
        "family_counts": dict(Counter(g["family"] for g in groups)),
        "integration_routes": dict(Counter(g["integration_route"] for g in groups)),
        "groups": groups,
    }
