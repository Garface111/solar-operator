"""Establish (or reverse) the cross-product tenant LINK for one email.

"One extension install feeds BOTH products": when a user owns a NEPOOL tenant
AND an Array Operator tenant on the same email, linking them lets a single
extension capture fan out into both (fan-out itself is separately gated behind
the FAN_OUT_TO_SIBLING env flag — see api/capture_fanout.py).

This script is the GUARDED, opt-in, one-email-at-a-time path. It resolves the
CANONICAL (active, non-duplicate) tenant per product (api.tenant_link) so it
never links to a stale dup, and is DRY-RUN by default.

Usage (local or `railway ssh "cd /app && ..."`):

    # Preview (writes nothing) — see exactly which two rows would link:
    python -m scripts.link_tenants_by_email bruce.genereaux@gmail.com

    # Apply the bidirectional link:
    python -m scripts.link_tenants_by_email bruce.genereaux@gmail.com --apply

    # Reverse a link (null both sides) for a tenant id:
    python -m scripts.link_tenants_by_email --unlink ten_6522da7ac2e1d01d --apply
"""
from __future__ import annotations

import argparse
import json
import sys

from api import tenant_link


def main() -> int:
    ap = argparse.ArgumentParser(description="Link/unlink a user's two product tenants.")
    ap.add_argument("email", nargs="?", help="contact email to link (canonical per product)")
    ap.add_argument("--unlink", metavar="TENANT_ID",
                    help="reverse: null linked_tenant_id on this tenant AND its sibling")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry-run preview)")
    args = ap.parse_args()

    if args.unlink:
        result = tenant_link.unlink_tenant(args.unlink, apply=args.apply)
    elif args.email:
        result = tenant_link.link_by_email(args.email, apply=args.apply)
    else:
        ap.error("provide an email to link, or --unlink <tenant_id>")
        return 2

    print(json.dumps(result, indent=2, default=str))
    if not args.apply:
        print("\n(DRY RUN — pass --apply to commit. Reverse anytime with "
              "--unlink <tenant_id> --apply.)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
