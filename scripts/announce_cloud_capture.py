"""Announce Cloud Capture (opt-in) to real Array Operator account holders.

SAFE BY DEFAULT: dry-run. It prints the exact recipient list and the rendered
email, and sends NOTHING unless you pass --send. Mass-emailing real customers is
irreversible, so review the list + copy first.

  Dry-run (default):  python scripts/announce_cloud_capture.py
  Preview one email:  python scripts/announce_cloud_capture.py --preview > /tmp/ann.html
  Actually send:      python scripts/announce_cloud_capture.py --send

Recipients = active, non-demo Array Operator tenants (product='array_operator')
with a contact email, MINUS obvious scratch/test signups and the comped design
partner. Tenant.is_demo is unreliable (see memory), so we also apply an email
denylist. Review anything that looks off before --send.
"""
import argparse
import re
import sys
import time

from sqlalchemy import select

from api.db import SessionLocal
from api.models import Tenant
from api.email_skin import render_email_skin, render_email_skin_text
from api.notify import _send_via_resend

ACCOUNT_URL = "https://arrayoperator.com/#account"
REPLY_TO = "admin@solaroperator.org"

# Scratch/test signups to exclude even when is_demo is not set (memory:
# ford.genereaux*@gmail.com variants, test@, fasdfas@, demo tenants). Plus the
# comped design partner (Paul) — he already knows; don't mass-mail him.
_DENY_EXACT = {"pbozuwa@gmail.com"}
_DENY_RE = re.compile(
    r"(ford\.genereaux|^test@|^fasdfas@|@energyagent-demo\.com|@example\.com|\+demo@|\+test@)",
    re.I,
)


def _is_real(t: Tenant) -> bool:
    email = (t.contact_email or "").strip().lower()
    if not email or "@" not in email:
        return False
    if t.is_demo or not t.active:
        return False
    if email in _DENY_EXACT or _DENY_RE.search(email):
        return False
    return True


def recipients():
    with SessionLocal() as db:
        rows = db.execute(
            select(Tenant).where(Tenant.product == "array_operator")
        ).scalars().all()
    seen, out = set(), []
    for t in rows:
        if not _is_real(t):
            continue
        email = t.contact_email.strip().lower()
        if email in seen:
            continue
        seen.add(email)
        out.append(t)
    return out


def _email_html(name: str) -> str:
    greeting = f"Hi {name}," if name else "Hi,"
    body = f"""
    <p>{greeting}</p>
    <p>We built a new, optional way to keep your Array Operator data fresh:
    <strong>Cloud Capture</strong>.</p>
    <p>Until now, keeping your live production and utility bills up to date meant running our
    browser extension on your own computer. That still works — and your passwords stay on your
    device — but it only refreshes while a browser tab is open.</p>
    <p><strong>With Cloud Capture, you give us your portal logins once and we do the rest, on our
    servers, around the clock.</strong> Your live inverter data stays under five minutes old and
    your utility bills refresh on their own. No tab to keep open, no computer to leave running.</p>
    <p>It is entirely <strong>opt-in</strong>. If you would rather keep your passwords on your own
    device, just keep using the extension — nothing changes for you.</p>
    <p>If you do turn it on:</p>
    <ul>
      <li>Your passwords are <strong>encrypted</strong> and used only to sign in on your behalf to
      the portals you connect.</li>
      <li>You can turn any login off or delete it at any time.</li>
      <li>Automated sign-in stops itself if a password looks wrong, so we never lock you out.</li>
    </ul>
    <p>To try it, open <strong>Master Account &rarr; Auto-refresh</strong> and choose
    &ldquo;Store it with us.&rdquo;</p>
    <p>&mdash; Ford, Array Operator</p>
    """
    return render_email_skin(
        preheader="A new, hands-off way to keep your solar data fresh — opt-in.",
        headline="Introducing Cloud Capture",
        intro_line="Live data, no browser tab required.",
        body_html=body,
        cta={"label": "Open Master Account", "url": ACCOUNT_URL},
        product="array_operator",
    )


def _email_text(name: str) -> str:
    greeting = f"Hi {name}," if name else "Hi,"
    return render_email_skin_text(
        headline="Introducing Cloud Capture",
        intro_line="Live data, no browser tab required.",
        body_text=(
            f"{greeting}\n\n"
            "We built a new, optional way to keep your Array Operator data fresh: Cloud Capture.\n\n"
            "Until now, keeping your data up to date meant running our browser extension on your "
            "own computer (your passwords stay on your device), and it only refreshes while a tab "
            "is open.\n\n"
            "With Cloud Capture, you give us your portal logins once and we do the rest, on our "
            "servers, around the clock — live inverter data under five minutes old, utility bills "
            "refreshed on their own. No tab, no computer to leave running.\n\n"
            "It is entirely opt-in. Prefer to keep your passwords on your own device? Keep using "
            "the extension — nothing changes.\n\n"
            "If you turn it on: your passwords are encrypted and used only to sign in on your "
            "behalf; you can remove any login anytime; and automated sign-in stops itself if a "
            "password looks wrong, so we never lock you out.\n\n"
            "Turn it on in Master Account -> Auto-refresh -> \"Store it with us\": " + ACCOUNT_URL +
            "\n\n-- Ford, Array Operator"
        ),
        cta={"label": "Open Master Account", "url": ACCOUNT_URL},
        product="array_operator",
    )


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually send (default: dry-run)")
    ap.add_argument("--preview", action="store_true", help="print one rendered email HTML and exit")
    args = ap.parse_args(argv)

    if args.preview:
        print(_email_html("Alex"))
        return

    tos = recipients()
    print(f"Array Operator announcement — {len(tos)} real recipient(s):")
    for t in tos:
        print(f"  • {t.contact_email}  ({t.name or 'unnamed'} · {t.id})")
    if not args.send:
        print("\nDRY RUN — nothing sent. Re-run with --send to send to the list above.")
        return

    print("\nSending…")
    ok = 0
    for t in tos:
        name = (t.operator_name or t.name or "").split(" ")[0].strip()
        sent = _send_via_resend(
            to=t.contact_email,
            subject="A new way to keep your solar data fresh — Cloud Capture (opt-in)",
            html=_email_html(name),
            text=_email_text(name),
            reply_to=REPLY_TO,
            product="array_operator",
        )
        print(f"  {'✓' if sent else '✗'} {t.contact_email}")
        ok += 1 if sent else 0
        time.sleep(0.6)          # gentle on Resend
    print(f"\nDone: {ok}/{len(tos)} sent.")


if __name__ == "__main__":
    main(sys.argv[1:])
