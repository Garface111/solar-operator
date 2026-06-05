"""
Stripe webhook self-test — signs a synthetic checkout.session.completed event
and fires it at the production endpoint to diagnose 400 Bad Request errors.

Usage:
    STRIPE_WEBHOOK_SECRET=whsec_... python scripts/test_webhook_signature.py

If STRIPE_WEBHOOK_SECRET is not set in the environment, the script reads it
from a .env file in the repo root (if present) via python-dotenv, or falls
back to a clearly-wrong placeholder so the invalid-signature test still runs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from typing import Optional

TARGET = "https://web-production-49c83.up.railway.app/v1/stripe/webhook"
LOG_TAIL_SECONDS = 8  # how long to tail railway logs after sending


# ── signature helpers ─────────────────────────────────────────────────────────

def _sign(payload: bytes, secret: str, timestamp: int) -> str:
    """Compute Stripe-Signature header value for the given payload and secret."""
    signed_payload = f"{timestamp}.".encode() + payload
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def _post(payload: bytes, sig_header: str) -> tuple[int, str]:
    """POST payload to TARGET with Stripe-Signature header. Returns (status, body)."""
    req = urllib.request.Request(
        TARGET,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": sig_header,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ── synthetic event ────────────────────────────────────────────────────────────

def _make_payload() -> bytes:
    event = {
        "id": "evt_selftest_0000000000000001",
        "object": "event",
        "type": "checkout.session.completed",
        "livemode": False,
        "created": int(time.time()),
        "data": {
            "object": {
                "id": "cs_selftest_0000000000000001",
                "object": "checkout.session",
                "metadata": {},
                "customer": None,
                "subscription": None,
                "customer_email": "selftest@solaroperator.org",
                "payment_status": "paid",
                "status": "complete",
            }
        },
    }
    return json.dumps(event, separators=(",", ":")).encode()


# ── env resolution ─────────────────────────────────────────────────────────────

def _resolve_secret() -> Optional[str]:
    val = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if val:
        return val
    # Try .env in repo root
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("STRIPE_WEBHOOK_SECRET="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ── railway log tail ──────────────────────────────────────────────────────────

def _tail_railway_logs(seconds: int) -> None:
    print(f"\n→ Tailing railway logs for ~{seconds}s (Ctrl-C to skip) ...")
    try:
        result = subprocess.run(
            ["railway", "logs", "--tail", "50"],
            capture_output=True, text=True, timeout=seconds
        )
        output = result.stdout + result.stderr
    except FileNotFoundError:
        print("  [railway CLI not found — skipping log tail]")
        return
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")

    if not output.strip():
        print("  [no log output captured]")
        return

    for line in output.splitlines():
        low = line.lower()
        if "webhook" in low or "signature" in low or "stripe" in low or "warning" in low:
            print(f"  {line}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=== Stripe Webhook Self-Test ===")

    secret = _resolve_secret()
    if not secret:
        print("WARNING: STRIPE_WEBHOOK_SECRET not set — valid-signature test will likely 400.")
        secret = "whsec_MISSING_SET_ENV_VAR"
    else:
        print(f"Using STRIPE_WEBHOOK_SECRET prefix: {secret[:12]}...")

    print(f"Target: {TARGET}\n")

    payload = _make_payload()
    ts = int(time.time())

    # ── Test 1: valid signature ────────────────────────────────────────────────
    print("→ POST with valid signature ...")
    sig_valid = _sign(payload, secret, ts)
    status, body = _post(payload, sig_valid)
    status_text = "OK" if status < 300 else "Bad Request" if status == 400 else f"HTTP {status}"
    print(f"  Response: {status} {status_text}")
    print(f"  Body: {body[:300]}")

    # ── Test 2: intentionally-wrong signature ─────────────────────────────────
    print("\n→ POST with INTENTIONALLY-WRONG signature ...")
    sig_bad = _sign(payload, "whsec_WRONG_SECRET_FOR_TESTING", ts)
    status2, body2 = _post(payload, sig_bad)
    status2_text = "OK" if status2 < 300 else "Bad Request" if status2 == 400 else f"HTTP {status2}"
    print(f"  Response: {status2} {status2_text}")
    print(f"  Body: {body2[:300]}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print()
    if status == 200 and status2 == 400:
        print("Verdict: SIGNATURE OK — endpoint accepts our secret and rejects wrong one.")
        print("         The production 400s are likely from Stripe using a DIFFERENT secret.")
        print("         Action: verify STRIPE_WEBHOOK_SECRET in Railway matches the Stripe")
        print("         Dashboard endpoint's signing secret exactly.")
    elif status == 400 and status2 == 400:
        print("Verdict: SECRET MISMATCH (90%+ confidence). Both valid and invalid signatures")
        print("         were rejected — the Railway env var does NOT match what Stripe signs with.")
        print()
        print("Next action: roll the signing secret in Stripe Dashboard, then")
        print("  railway variables --set STRIPE_WEBHOOK_SECRET=<new value>")
    elif status == 200 and status2 == 200:
        print("Verdict: WEBHOOK SECRET IS EMPTY ON SERVER — both requests accepted.")
        print("         The server is running without signature verification.")
        print("         Set STRIPE_WEBHOOK_SECRET on Railway immediately.")
    else:
        print(f"Verdict: UNEXPECTED — valid={status}, invalid={status2}. Investigate manually.")

    # ── Log tail ──────────────────────────────────────────────────────────────
    _tail_railway_logs(LOG_TAIL_SECONDS)


if __name__ == "__main__":
    main()
