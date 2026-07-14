"""CLI for the Array Operator redesign + Energy Agent announcement.

Logic lives in api/jobs/energy_agent_announce.py (shared with the scheduler).

  Dry-run (default):  PYTHONPATH=. python scripts/announce_energy_agent.py
  Preview one email:  PYTHONPATH=. python scripts/announce_energy_agent.py --preview
  Send now:           PYTHONPATH=. python scripts/announce_energy_agent.py --send

Scheduled path: set ENERGY_AGENT_ANNOUNCE_AT (ISO) on Railway; cron fires once.
"""
import argparse
import sys

from api.jobs.energy_agent_announce import send_announcement, email_html, recipients


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually send (default: dry-run)")
    ap.add_argument("--preview", action="store_true", help="print one rendered email and exit")
    args = ap.parse_args(argv)

    if args.preview:
        print(email_html("Alex"))
        return

    if not args.send:
        tos = recipients()
        print(f"Array Operator Energy Agent announcement — {len(tos)} recipient(s):")
        for t in tos:
            print(f"  • {t.contact_email}  ({t.name or 'unnamed'} · {t.id})")
        print("\nDRY RUN — nothing sent. Re-run with --send to send to the list above.")
        return

    result = send_announcement(send=True)
    print(f"Sent {result.get('ok')}/{result.get('count')}.")


if __name__ == "__main__":
    main(sys.argv[1:])
