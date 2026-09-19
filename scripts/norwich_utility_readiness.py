"""Prepare a utility/source inventory without network calls or credentials.

python -m scripts.norwich_utility_readiness --template intake.csv
python -m scripts.norwich_utility_readiness --roster intake.csv --output ./utility-review
python -m scripts.norwich_utility_readiness --scaffold acme_power --output ./utility-review
"""
import argparse
import csv
import html
import json
from pathlib import Path
import re

from api.utility_readiness import analyze_roster, IntakeError

COLUMNS = ["group_id", "utility_name", "provider_code", "state", "portal_url",
           "source_role", "billing_basis", "cadence", "offtaker_count"]


def report_markdown(report):
    def safe(value):
        return html.escape(str(value or "")).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")
    lines = ["# Utility integration triage", "",
             f"{report['group_count']} source groups; {report['offtaker_count']} supplied offtaker assignments.",
             "**Planning only. No group has been qualified for automatic invoicing by this report.**", "",
             report["count_note"], "",
             "| Group | Utility | Offtakers | Family | Billing source | Work required |",
             "|---|---|---:|---|---|---|"]
    for g in report["groups"]:
        lines.append("| " + " | ".join(safe(g[k]) for k in (
            "group_id", "utility_name", "offtaker_count", "family", "billing_basis", "integration_route")) + " |")
    for g in report["groups"]:
        lines += ["", "## " + safe(g["group_id"]), "", safe(g["limitation"])]
        if g["identity_issues"]:
            lines += ["", "Resolve: " + ", ".join(safe(x) for x in g["identity_issues"])]
        lines += ["", "Required evidence:", ""] + ["- " + safe(x) for x in g["required_evidence"]]
    return "\n".join(lines) + "\n"


def scaffold(code, output):
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", code):
        raise IntakeError("Scaffold code must be lowercase letters, digits and underscores")
    target = output / "integration-packs" / code
    target.mkdir(parents=True, exist_ok=False)
    manifest = {"provider_code": code, "status": "candidate", "portal_url": None,
                "billing_basis": "unknown", "source_role": "unknown",
                "authorized_access_reference": None, "mfa_recovery_owner": None,
                "fixture_sources": [], "independent_reviewer": None,
                "live_account_verification": None, "invoice_canary": None}
    (target / "qualification.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (target / "normalize.py").write_text(
        '"""Candidate parser. Keep raw evidence and expected results separate."""\n'
        'def normalize(raw_bytes):\n'
        '    raise NotImplementedError("Add verified utility-specific parsing; do not infer bill credit from a usage total")\n')
    (target / "README.md").write_text(f"""# {code}: candidate integration

No catalog edits, production writes, logins or sending are enabled by this pack.

1. Verify the utility's official identity and exact login host. Prefer an authorized API or original export where available. Record the permitted access method and MFA/recovery contact; store credentials only in the existing vault.
2. Get two closed billing cycles with original PDF/CSV/JSON artifacts, a generation-meter sample and any allocation statement. Keep secret-bearing HAR files out of this pack and source control; provide a redacted structural sample separately.
3. Have an independent reviewer transcribe expected identity, period, units and invoice inputs from the original source. Preserve its SHA-256. Implement normalize.py against those records.
4. Validate normalized records with scripts.verify_utility_fixture. Do not generate expected values with the parser being tested. A fixture pass is an offline result, not production approval.
5. Add tests for wrong account, swapped generation/consumption/export, zero generation, incomplete dates, duplicate capture, revised bills, expired session, timeout/429, MFA and partial multi-account failure.
6. Reuse api.harvester.vendors.base and the existing ingest bridge. Register only the correct provider/host. A daily generation collector does not imply a bill-credit or allocation-statement adapter.
7. Prove two separate captures, including fresh login/session renewal, on an authorized real account. Compare all expected accounts/cycles against what landed, then obtain invoice-canary approval before enabling automatic sending.

See docs/handoffs/NORWICH_UTILITY_EXPANSION_PLAYBOOK_2026-09-19.md for the release and incident checklist.
""")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--template", type=Path)
    mode.add_argument("--roster", type=Path)
    mode.add_argument("--scaffold")
    parser.add_argument("--output", type=Path, default=Path("utility-review"))
    args = parser.parse_args()
    try:
        if args.template:
            with args.template.open("x", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(COLUMNS)
            print("Created blank intake template:", args.template)
        elif args.scaffold:
            print("Created candidate integration pack:", scaffold(args.scaffold, args.output))
        else:
            with args.roster.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                if len(reader.fieldnames or []) != len(COLUMNS) or set(reader.fieldnames or []) != set(COLUMNS):
                    raise IntakeError("CSV headers must match the intake template; do not include credentials")
                rows = list(reader)
                if any(None in row or any(value is None for value in row.values()) for row in rows):
                    raise IntakeError("CSV rows must contain exactly the template columns")
            report = analyze_roster(rows)
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "utility-readiness.json").write_text(json.dumps(report, indent=2) + "\n")
            (args.output / "utility-readiness.md").write_text(report_markdown(report))
            print(f"Triaged {report['group_count']} groups; {report['offtaker_count']} supplied assignments. No production qualification implied.")
    except (IntakeError, FileExistsError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
