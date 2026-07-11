"""CLI for the Cloud Capture announcement email (logic lives in
api/jobs/cloud_capture_announce.py so the scheduler + CLI share one source).

  Dry-run (default):  python scripts/announce_cloud_capture.py
  Preview one email:  python scripts/announce_cloud_capture.py --preview > /tmp/ann.html
  Send now:           python scripts/announce_cloud_capture.py --send

The scheduled path (fire-once at CLOUD_CAPTURE_ANNOUNCE_AT) runs from the backend
cron via maybe_send_scheduled(); this CLI is for manual/dry-run use.
"""
import argparse
import sys

from api.jobs.cloud_capture_announce import send_announcement, email_html, recipients


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
        print(f"Array Operator announcement — {len(tos)} recipient(s) (testers included):")
        for t in tos:
            print(f"  • {t.contact_email}  ({t.name or 'unnamed'} · {t.id})")
        print("\nDRY RUN — nothing sent. Re-run with --send to send to the list above.")
        return

    result = send_announcement(send=True)
    print(f"Sent {result.get('ok')}/{result.get('count')}.")


if __name__ == "__main__":
    main(sys.argv[1:])
