"""
Synthetic GMP health monitor.

Nightly cron that authenticates as a known GMP test account, calls the
canonical bill endpoint, validates the response shape, logs the result to
storage/synthetic_runs.jsonl, and emails Ford on failure or schema drift.

Usage:
  python -m scripts.synthetic_gmp_monitor           # standard run (called by scheduler)
  python -m scripts.synthetic_gmp_monitor --once    # one-shot manual trigger
  python -m scripts.synthetic_gmp_monitor --dry-run # no real HTTP calls, prints plan

Required env vars:
  SYNTHETIC_GMP_REFRESH_TOKEN  — GMP refresh token for the test account
  SYNTHETIC_GMP_ACCOUNT_NUMBER — GMP account number for the test account

Set on Railway:
  railway variables --set "SYNTHETIC_GMP_REFRESH_TOKEN=<32-char-token>"
  railway variables --set "SYNTHETIC_GMP_ACCOUNT_NUMBER=<11-digit-number>"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
RUNS_LOG = STORAGE_DIR / "synthetic_runs.jsonl"

# Minimum fields a well-formed GMP bill object must carry
REQUIRED_BILL_FIELDS = {"billDate", "billSegments"}
REQUIRED_SEGMENT_FIELDS = {"segmentLineItems"}


def _load_last_run() -> dict | None:
    """Return the most recent run record from the JSONL log, or None."""
    if not RUNS_LOG.exists():
        return None
    last = None
    with RUNS_LOG.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    pass
    return last


def _compute_schema_hash(bills: list[dict]) -> str:
    """
    Hash the structural shape of the response: sorted union of all keys seen
    at bill level, segment level, and line-item level across all returned bills.
    Detects field additions and removals without triggering on value changes.
    """
    bill_keys: set[str] = set()
    seg_keys: set[str] = set()
    li_keys: set[str] = set()

    for bill in bills:
        bill_keys.update(bill.keys())
        for seg in bill.get("billSegments", []):
            seg_keys.update(seg.keys())
            for li in seg.get("segmentLineItems", []):
                li_keys.update(li.keys())

    schema_repr = json.dumps(
        {
            "bill": sorted(bill_keys),
            "segment": sorted(seg_keys),
            "line_item": sorted(li_keys),
        },
        sort_keys=True,
    )
    return hashlib.sha256(schema_repr.encode()).hexdigest()[:16]


def _validate_response(bills: list[dict]) -> list[str]:
    """Return a list of validation error strings; empty list means OK."""
    errors: list[str] = []
    if not bills:
        errors.append("empty bill list returned")
        return errors

    sample = bills[0]
    missing_bill = REQUIRED_BILL_FIELDS - sample.keys()
    if missing_bill:
        errors.append(f"missing required bill fields: {sorted(missing_bill)}")

    segs = sample.get("billSegments", [])
    if not segs:
        errors.append("first bill has no billSegments")
    else:
        missing_seg = REQUIRED_SEGMENT_FIELDS - segs[0].keys()
        if missing_seg:
            errors.append(f"missing required segment fields: {sorted(missing_seg)}")

    return errors


def run(*, dry_run: bool = False) -> dict[str, Any]:
    """
    Execute one synthetic check cycle.

    Returns a result dict with: success, latency_ms, response_hash,
    previous_hash, schema_changed, error, timestamp_utc.
    """
    from api.gmp_refresh import refresh_gmp_token, GmpRefreshError
    from api.adapters.gmp import fetch_bills_json
    from api.notify import send_internal_alert

    refresh_token = os.environ.get("SYNTHETIC_GMP_REFRESH_TOKEN", "")
    account_number = os.environ.get("SYNTHETIC_GMP_ACCOUNT_NUMBER", "")

    if not refresh_token or not account_number:
        # The synthetic GMP canary is OPTIONAL and only runs where its test-account
        # creds are configured. When absent, skip cleanly (return, do NOT raise) so
        # the scheduler doesn't page a daily "unhandled exception". Enable it by
        # setting SYNTHETIC_GMP_REFRESH_TOKEN + SYNTHETIC_GMP_ACCOUNT_NUMBER.
        missing = ("SYNTHETIC_GMP_REFRESH_TOKEN" if not refresh_token
                   else "SYNTHETIC_GMP_ACCOUNT_NUMBER")
        return {"success": None, "skipped": True,
                "error": f"{missing} not set \u2014 synthetic GMP monitor disabled"}

    last_run = _load_last_run()
    previous_hash = (last_run or {}).get("response_hash")

    if dry_run:
        print("[dry-run] would call: refresh_gmp_token(SYNTHETIC_GMP_REFRESH_TOKEN)")
        print(
            f"[dry-run] would call: fetch_bills_json("
            f"account={account_number!r}, jwt=<from-refresh>)"
        )
        print("[dry-run] would validate: non-empty bills, required fields present")
        print(
            f"[dry-run] would compute schema hash and compare with "
            f"previous hash: {previous_hash!r}"
        )
        print(f"[dry-run] would write result to: {RUNS_LOG}")
        return {"success": True, "dry_run": True}

    now_utc = datetime.now(timezone.utc)
    t0 = time.monotonic()
    result: dict[str, Any] = {
        "timestamp_utc": now_utc.isoformat(),
        "success": False,
        "latency_ms": None,
        "response_hash": None,
        "previous_hash": previous_hash,
        "schema_changed": False,
        "error": None,
    }

    try:
        logger.info("Refreshing GMP token (prefix: %s...)", refresh_token[:6])
        jwt, expires_at = refresh_gmp_token(refresh_token)
        logger.info("Token refreshed, expires_at=%s", expires_at.isoformat())

        logger.info("Fetching bills for account %s", account_number)
        bills = fetch_bills_json(account_number, jwt)
        result["latency_ms"] = round((time.monotonic() - t0) * 1000)

        errors = _validate_response(bills)
        if errors:
            raise ValueError("; ".join(errors))

        h = _compute_schema_hash(bills)
        result["response_hash"] = h
        schema_changed = previous_hash is not None and h != previous_hash
        result["schema_changed"] = schema_changed
        result["success"] = True

        logger.info(
            "Check PASSED: %d bills, hash=%s, latency=%dms, schema_changed=%s",
            len(bills),
            h,
            result["latency_ms"],
            schema_changed,
        )

        if schema_changed:
            send_internal_alert(
                "Synthetic GMP check: SCHEMA DRIFT DETECTED",
                f"GMP response schema has changed since the last run.\n\n"
                f"Previous hash : {previous_hash}\n"
                f"New hash      : {h}\n\n"
                f"Account       : {account_number}\n"
                f"Bills returned: {len(bills)}\n"
                f"Latency       : {result['latency_ms']} ms\n"
                f"Timestamp     : {result['timestamp_utc']}\n\n"
                f"ACTION NEEDED: verify that bill_json_to_metrics() and "
                f"_extract_kwh_generated() still parse the new shape correctly. "
                f"Check api/adapters/gmp.py.",
            )

    except GmpRefreshError as exc:
        result["latency_ms"] = round((time.monotonic() - t0) * 1000)
        result["error"] = f"token refresh failed: {exc}"
        logger.error("Synthetic check FAILED (token refresh): %s", exc)
    except Exception as exc:
        result["latency_ms"] = round((time.monotonic() - t0) * 1000)
        result["error"] = str(exc)
        logger.error("Synthetic check FAILED: %s", exc)

    if not result["success"]:
        send_internal_alert(
            f"Synthetic GMP check FAILED: {(result['error'] or 'unknown')[:100]}",
            f"Error         : {result['error']}\n"
            f"Account       : {account_number}\n"
            f"Latency       : {result['latency_ms']} ms\n"
            f"Previous hash : {previous_hash}\n"
            f"Timestamp     : {result['timestamp_utc']}\n\n"
            f"Check Railway logs for the full traceback.\n\n"
            f"To disable temporarily: set SYNTHETIC_GMP_REFRESH_TOKEN= (empty) "
            f"in Railway environment variables.",
        )

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with RUNS_LOG.open("a") as f:
        f.write(json.dumps(result) + "\n")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic GMP health monitor")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without making real API calls",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once immediately and exit (explicit alias for manual trigger via railway ssh)",
    )
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    if not args.dry_run:
        status = "PASSED" if result.get("success") else "FAILED"
        print(f"Synthetic GMP check {status}: {result}")
        if not result.get("success"):
            sys.exit(1)


if __name__ == "__main__":
    main()
